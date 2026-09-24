"""Bounded, read-only discovery. Untrusted cards never grant access or run code."""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import math
import queue
import re
import socket
import ssl
import struct
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener

from ..society import directory

MCP_ENDPOINT = "https://registry.modelcontextprotocol.io/v0.1/servers"
PROVIDERS = (*directory.PROVIDERS, "mcp_registry")
MAX_CARD_BYTES = 128 * 1024
MAX_REGISTRY_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 10
TOTAL_SECONDS = 24
MAX_ITEMS = 8
_DNS_SLOTS = threading.BoundedSemaphore(8)
LIMITATIONS = [
    "Поиск ограничен выбранными публичными каталогами и явно заданными карточками; весь интернет и закрытые сети не индексируются.",
    "Идентичность, возможности и согласие в карточке заявлены оператором, криптографически не подтверждены.",
    "Карточки являются данными: их команды, ссылки на endpoint и код не исполняются; сообщения агентам не отправляются.",
    "Обнаружение не означает присоединение, работоспособность агента, лицензионное разрешение или научную валидацию.",
]


def _stamp():
    return directory._timestamp()


def _text(value, maximum=240):
    return directory._text(value, maximum)


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("Discovery deadline exceeded")
    return min(TIMEOUT_SECONDS, left)


def _json_load(raw):
    def reject_constant(_):
        raise ValueError("Non-finite JSON rejected")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key rejected")
            result[key] = value
        return result

    payload = json.loads(raw.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    count, stack = 0, [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        count += 1
        if depth > 32 or count > 12000:
            raise ValueError("JSON structure exceeds bounds")
        if isinstance(value, str):
            if len(value) > 12000 or any(not c.isprintable() and c not in "\n\r\t" for c in value):
                raise ValueError("JSON text exceeds bounds")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite JSON rejected")
        elif isinstance(value, dict):
            stack.extend((k, depth + 1) for k in value)
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)
    return payload


def _read_response(response, maximum, deadline, connection=None):
    if response.status != 200:
        raise ValueError("Non-success response or redirect rejected")
    content_type = response.headers.get_content_type()
    if content_type not in {"application/json", "text/json", "application/agent+json"}:
        raise ValueError("Non-JSON response rejected")
    if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
        raise ValueError("Compressed response rejected")
    length = response.headers.get("Content-Length")
    if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > maximum):
        raise ValueError("Invalid Content-Length")
    chunks, total = [], 0
    reader = getattr(response, "read1", response.read)
    while True:
        timeout = _remaining(deadline)
        if connection is not None:
            connection.settimeout(timeout)
        chunk = reader(min(16384, maximum + 1 - total))
        _remaining(deadline)
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise ValueError("Response exceeds byte limit")
        chunks.append(chunk)
    raw = b"".join(chunks)
    return _json_load(raw), hashlib.sha256(raw).hexdigest()


def _valid_onion(host):
    if not re.fullmatch(r"[a-z2-7]{56}\.onion", host):
        return False
    decoded = base64.b32decode(host[:-6].upper())
    return decoded[-1] == 3 and decoded[32:34] == hashlib.sha3_256(
        b".onion checksum" + decoded[:32] + decoded[-1:]
    ).digest()[:2]


def _validate_url(url):
    if not isinstance(url, str) or not 1 <= len(url) <= 2000 or re.search(r"[\x00-\x20\x7f\\]", url):
        raise ValueError("Invalid card URL")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if not host or parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError("Invalid card authority")
        host = host.encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError):
        raise ValueError("Invalid card URL") from None
    if host.endswith(".onion"):
        if not _valid_onion(host) or parts.scheme not in {"http", "https"}:
            raise ValueError("A valid v3 onion URL is required")
        expected = 443 if parts.scheme == "https" else 80
        layer = "tor_onion"
    else:
        if parts.scheme != "https" or host.endswith(".") or host == "localhost" or host.endswith(".localhost"):
            raise ValueError("Public HTTPS URL required")
        expected, layer = 443, "clearnet"
    if port is not None and port != expected:
        raise ValueError("Only the standard transport port is allowed")
    authority = "[" + host + "]" if ":" in host else host
    path = parts.path or "/"
    try:
        (path + parts.query).encode("ascii")
    except UnicodeError:
        raise ValueError("Card path must be URL encoded") from None
    normalized = urlunsplit((parts.scheme, authority, path, parts.query, ""))
    target = path + ("?" + parts.query if parts.query else "")
    return {"url": normalized, "host": host, "port": expected, "scheme": parts.scheme,
            "authority": authority, "target": target, "layer": layer}


def _public_ip(value):
    ip = ipaddress.ip_address(value)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        raise ValueError("Mapped IP addresses are unsupported")
    if ip.version == 6 and (ip.sixtofour is not None or ip.teredo is not None or
                           ip in ipaddress.ip_network("64:ff9b::/96") or
                           ip in ipaddress.ip_network("64:ff9b:1::/48")):
        raise ValueError("IPv6 transition addresses are unsupported")
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise ValueError("Non-public IP rejected")
    return ip


def _resolve_public(host, deadline):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        ip = _public_ip(host)
        return [(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, str(ip))]
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ValueError("Resolver capacity reached")
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put((True, socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        except OSError:
            result.put((False, None))
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, daemon=True, name="federation-card-dns").start()
    try:
        ok, rows = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise TimeoutError("DNS deadline exceeded") from None
    if not ok or not rows or len(rows) > 64:
        raise ValueError("Card DNS lookup failed")
    addresses = []
    for family, _kind, _proto, _canon, address in rows:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise ValueError("Unsupported address family")
        ip = _public_ip(address[0])
        pair = (family, str(ip))
        if pair not in addresses:
            addresses.append(pair)
    return addresses


def _connect_literal(family, address, port, deadline):
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(_remaining(deadline))
        sock.connect((address, port))
        return sock
    except BaseException:
        sock.close()
        raise


def _proxy_parts(proxy):
    if not isinstance(proxy, str) or len(proxy) > 180 or re.search(r"[\x00-\x20\x7f\\]", proxy):
        raise ValueError("A local SOCKS5 proxy URL is required")
    if "://" not in proxy:
        proxy = "socks5h://" + proxy
    try:
        parts = urlsplit(proxy)
        ip = ipaddress.ip_address(parts.hostname or "")
        if parts.scheme not in {"socks5", "socks5h"} or not ip.is_loopback or parts.username is not None or parts.password is not None:
            raise ValueError("Proxy must be a loopback literal without credentials")
        if parts.path not in {"", "/"} or parts.query or parts.fragment or parts.port is None:
            raise ValueError("Invalid proxy URL")
        if not 1 <= parts.port <= 65535:
            raise ValueError("Invalid proxy port")
        return socket.AF_INET6 if ip.version == 6 else socket.AF_INET, str(ip), parts.port
    except (ValueError, UnicodeError):
        raise ValueError("Proxy must be a local SOCKS5 URL with an explicit port") from None


def _recv_exact(sock, count, deadline):
    chunks = bytearray()
    while len(chunks) < count:
        sock.settimeout(_remaining(deadline))
        data = sock.recv(count - len(chunks))
        if not data:
            raise OSError("SOCKS connection closed")
        chunks.extend(data)
    return bytes(chunks)


def _tor_socket(info, proxy, deadline):
    family, address, port = _proxy_parts(proxy)
    sock = _connect_literal(family, address, port, deadline)
    try:
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2, deadline) != b"\x05\x00":
            raise ValueError("SOCKS5 no-auth handshake rejected")
        host = info["host"].encode("ascii")
        # ATYP=3 sends the onion name to Tor; no DNS lookup of the onion occurs.
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", info["port"]))
        version, status, reserved, address_type = _recv_exact(sock, 4, deadline)
        if (version, status, reserved) != (5, 0, 0):
            raise ValueError("SOCKS5 destination rejected")
        if address_type == 1:
            count = 4
        elif address_type == 4:
            count = 16
        elif address_type == 3:
            count = _recv_exact(sock, 1, deadline)[0]
            if not count:
                raise ValueError("Invalid SOCKS5 reply")
        else:
            raise ValueError("Invalid SOCKS5 address type")
        _recv_exact(sock, count + 2, deadline)
        return sock
    except BaseException:
        sock.close()
        raise


def _fetch_card(info, tor_proxy):
    deadline = time.monotonic() + TOTAL_SECONDS
    if info["layer"] == "tor_onion":
        if tor_proxy is None:
            raise ValueError("Onion inspection requires a configured local Tor SOCKS5 proxy")
        sock = _tor_socket(info, tor_proxy, deadline)
    else:
        if tor_proxy is not None:
            raise ValueError("Tor proxy is supported only for explicit onion seeds")
        addresses = _resolve_public(info["host"], deadline)
        family, address = addresses[0]
        # Connect to the vetted numeric address, never resolve the hostname again.
        sock = _connect_literal(family, address, info["port"], deadline)
    conn, watchdog = None, None
    try:
        if info["scheme"] == "https":
            sock.settimeout(_remaining(deadline))
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=info["host"])
        sock.settimeout(_remaining(deadline))
        # HTTP header parsing can make several reads. Interrupt the socket at
        # the absolute deadline as well as setting each operation's timeout.
        def expire():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        watchdog = threading.Timer(max(0, deadline - time.monotonic()), expire)
        watchdog.daemon = True
        watchdog.start()
        conn = http.client.HTTPConnection(info["host"], info["port"], timeout=_remaining(deadline))
        conn.sock = sock
        conn.request("GET", info["target"], headers={"Host": info["authority"], "Accept": "application/json",
                     "Accept-Encoding": "identity", "Connection": "close", "User-Agent": "Meta-Harness/0.7 card-inspection"})
        response = conn.getresponse()
        return _read_response(response, MAX_CARD_BYTES, deadline, sock)
    finally:
        if watchdog is not None:
            watchdog.cancel()
        if conn is not None:
            conn.close()
        sock.close()


def _optional_url(value):
    try:
        return _validate_url(value)["url"]
    except ValueError:
        return ""


def _normalize_card(payload, info, digest):
    if not isinstance(payload, dict) or not isinstance(payload.get("skills", []), list) or not isinstance(payload.get("capabilities", {}), dict):
        raise ValueError("Invalid Agent Card schema")
    name = _text(payload.get("name"), 240)
    if not name or not ("skills" in payload or "supportedInterfaces" in payload or "protocolVersion" in payload):
        raise ValueError("Missing Agent Card fields")
    version = _text(payload.get("protocolVersion"), 32)
    if not version:
        interfaces = payload.get("supportedInterfaces", [])
        if isinstance(interfaces, list):
            for interface in interfaces[:8]:
                if isinstance(interface, dict):
                    version = _text(interface.get("protocolVersion"), 32)
                    if version:
                        break
    version = version if re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", version) else "unspecified"
    capabilities = []
    for skill in payload.get("skills", [])[:32]:
        if not isinstance(skill, dict):
            continue
        capability = _text(skill.get("name"), 160) or _text(skill.get("id"), 160)
        if capability and capability not in capabilities:
            capabilities.append(capability)
        if len(capabilities) >= 16:
            break
    extension = payload.get("meta_harness")
    collaboration = extension.get("collaboration") if isinstance(extension, dict) else None
    explicit_opt_in = isinstance(collaboration, dict) and collaboration.get("opt_in") is True
    terms = _optional_url(collaboration.get("terms_url")) if isinstance(collaboration, dict) else ""
    now = _stamp()
    return {"id": "seed:" + hashlib.sha256(info["url"].encode()).hexdigest()[:32], "name": name,
            "url": info["url"], "entity_type": "agent", "organization": _text(payload.get("provider", {}).get("organization"), 240) if isinstance(payload.get("provider"), dict) else "",
            "description": _text(payload.get("description"), 700), "protocol": "A2A", "protocol_version": version,
            "capabilities": capabilities, "self_reported_capabilities": list(capabilities),
            "collaboration": {"opt_in": explicit_opt_in, "terms_url": terms, "verification": "self_reported_not_authenticated"},
            "license": "not_specified", "status": "card_inspected_not_connected", "readiness": "reviewed",
            "network_layer": info["layer"], "seen_at": now, "retrieved_at": now,
            "provenance": {"provider": "explicit_seed", "retrieved_at": now, "card_url": info["url"],
                "card_sha256": digest, "transport": "local_socks5_domain" if info["layer"] == "tor_onion" else "https_pinned_public_ip",
                "identity_verification": "self_reported", "card_contacted": True, "endpoint_contacted": False},
            "limitations": list(LIMITATIONS) + ["opt_in сообщает о намерении из карточки; вступление и обмен требуют отдельного подтверждения оператора и проверки условий."]}


def inspect_card(url, tor_proxy=None):
    """Read one explicit public HTTPS or v3 onion Agent Card; never follow links.

    Raises ValueError on validation or transport failure. Error strings never
    include server responses, credentials, proxy settings or private addresses.
    """
    info = _validate_url(url)
    if info["layer"] == "tor_onion":
        _proxy_parts(tor_proxy)
    elif tor_proxy is not None:
        raise ValueError("Tor proxy is supported only for explicit onion seeds")
    try:
        payload, digest = _fetch_card(info, tor_proxy)
        return _normalize_card(payload, info, digest)
    except (ValueError, TypeError, UnicodeError, RecursionError, OSError, http.client.HTTPException):
        raise ValueError("Agent Card could not be inspected: transport, address policy, format or limit check failed") from None


def _fetch_registry(query, limit):
    url = MCP_ENDPOINT + "?" + urlencode({"search": query, "limit": limit, "version": "latest"})
    request = Request(url, headers={"Accept": "application/json", "Accept-Encoding": "identity",
                                   "User-Agent": "Meta-Harness/0.7 registry-discovery"})
    # Only this fixed official API uses the system proxy, as other built-in
    # catalogs do. Arbitrary seed inspection above ignores all proxy variables.
    with build_opener(directory._NoRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
        if response.geturl() != url:
            raise ValueError("Registry redirect rejected")
        return _read_response(response, MAX_REGISTRY_BYTES, time.monotonic() + TOTAL_SECONDS)[0]


def _parse_registry(payload, limit):
    rows = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Invalid MCP registry response")
    result, now = {}, _stamp()
    for row in rows[:limit]:
        if not isinstance(row, dict) or not isinstance(row.get("server"), dict):
            continue
        server = row["server"]
        ident = server.get("name")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,240}", ident) or ".." in ident:
            continue
        metadata = row.get("_meta", {})
        official = metadata.get("io.modelcontextprotocol.registry/official", {}) if isinstance(metadata, dict) else {}
        status = official.get("status") if isinstance(official, dict) else None
        if status is not None and status != "active":
            continue
        description = _text(server.get("description"), 700)
        item = {"id": "mcp_registry:" + ident, "name": _text(server.get("title"), 240) or ident,
                "url": MCP_ENDPOINT, "entity_type": "mcp_server", "organization": "Publisher namespace: " + ident.split("/")[0],
                "description": description, "protocol": "MCP", "protocol_version": "not_inferred",
                "capabilities": [description] if description else [], "license": "not_specified", "revision": _text(server.get("version"), 80),
                "status": "directory_listed_not_connected", "readiness": "discovered", "network_layer": "clearnet",
                "seen_at": now, "retrieved_at": now, "collaboration": {"opt_in": False, "terms_url": ""},
                "provenance": {"provider": "mcp_registry", "retrieved_at": now, "endpoint": MCP_ENDPOINT,
                    "directory_name": ident, "identity_verification": "registry_namespace_not_operator_audit", "endpoint_contacted": False},
                "limitations": list(LIMITATIONS) + ["MCP-сервер является инструментом, не обязательно самостоятельным агентом; метаданные пакетов и секреты не исполняются."]}
        result.setdefault(item["id"], item)
    return list(result.values())


def discover(query, providers=None, limit=5, online=False, root=None, data_class="public"):
    """Search selected fixed catalogs, or the local curated registry offline.

    ``limit`` is per provider (1..8), with at most four providers; 32 candidates
    maximum. Public catalogs receive the query only when online=True and the
    caller classifies the query as public. This classifier is not a DLP engine.
    """
    query = directory._query(query)
    if type(online) is not bool or type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise ValueError("online must be boolean and limit must be an integer from 1 to 8")
    if not isinstance(data_class, str) or not 1 <= len(data_class) <= 40:
        raise ValueError("A data classification is required")
    if online and data_class != "public":
        raise ValueError("Only public queries can be sent to external catalogs")
    providers = list(PROVIDERS) if providers is None else providers
    if not isinstance(providers, (list, tuple)) or not 1 <= len(providers) <= len(PROVIDERS):
        raise ValueError("Select one to four supported providers")
    if any(not isinstance(p, str) or p not in PROVIDERS for p in providers) or len(set(providers)) != len(providers):
        raise ValueError("Unsupported or duplicate provider")
    items, reports = {}, []
    for provider in providers:
        errors, found = [], []
        requests = int(online)
        mode = "public_directory" if online else "offline_registry"
        if provider != "mcp_registry":
            response = directory.discover(query, provider=provider, limit=limit, offline=not online, root=root)
            found, errors, requests = response["items"], response["errors"], response["requests"]
        else:
            try:
                found = _parse_registry(_fetch_registry(query, limit), limit) if online else directory._offline(query, provider, limit, root)
            except (HTTPError, URLError, OSError, ValueError, TypeError, UnicodeError, RecursionError, http.client.HTTPException):
                errors = [{"provider": provider, "code": "registry_unavailable_or_invalid", "message": "Реестр недоступен либо ответ не прошёл проверку формата и лимитов."}]
        for item in found:
            item.setdefault("network_layer", "clearnet" if online else "local_registry")
            item.setdefault("readiness", "discovered")
            item.setdefault("retrieved_at", item["provenance"]["retrieved_at"])
            item.setdefault("seen_at", item["retrieved_at"])
            item.setdefault("self_reported_capabilities", list(item.get("capabilities", [])))
            item.setdefault("collaboration", {"opt_in": False, "terms_url": ""})
            items.setdefault(item["id"], item)
        reports.append({"provider": provider, "mode": mode, "requests": requests, "item_count": len(found), "errors": errors})
    return {"query": query, "mode": "online" if online else "offline", "data_class": data_class,
            "items": list(items.values()), "provider_reports": reports, "requests": sum(p["requests"] for p in reports),
            "limitations": list(LIMITATIONS)}

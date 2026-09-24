"""Small authenticated worker transport; no public job submission endpoint.

The shared bearer credential defines a trusted worker group, not separate tenants.
TLS is mandatory outside literal loopback addresses / localhost. Credentials never
enter SQLite, request logs or response bodies. No redirects are followed by Client.
"""
import hmac
import http.server
import ipaddress
import json
import math
import os
import socket
import ssl
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MAX_BODY = 1152 * 1024
SOCKET_TIMEOUT = 10


class TransportError(Exception):
    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_token(token):
    if not isinstance(token, str) or not 32 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("META_HUB_TOKEN must contain 32–4096 printable non-space ASCII characters")
    return token


def validate_url(url):
    if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url:
        raise ValueError("Invalid hub URL")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("Invalid hub URL")
        if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("Hub URL must have no path, query or fragment")
        parsed.port
        if parsed.scheme != "https" and not is_loopback(parsed.hostname):
            raise ValueError("Non-loopback hub connections require HTTPS")
    except (ValueError, UnicodeError):
        raise ValueError("Hub URL must be HTTPS, or HTTP on loopback, without credentials/path/query/fragment") from None
    return url.rstrip("/")


def _validate_json(value, depth=0):
    if depth > 24:
        raise ValueError("JSON nesting exceeds limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            _validate_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item, depth + 1)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError("Unsupported JSON value")


def _encode(value):
    _validate_json(value)
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_BODY:
        raise ValueError("JSON payload exceeds transport limit")
    return data


def _decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
        _validate_json(result)
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid finite JSON document") from None


class _BoundedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(SOCKET_TIMEOUT)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            if isinstance(request, ssl.SSLSocket):
                request.do_handshake()
            super().process_request_thread(request, client_address)
        except (ssl.SSLError, OSError):
            self.shutdown_request(request)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # Never print incoming request content or exception payloads.
        pass


def make_hub(queue, host="127.0.0.1", port=8766, token=None, certfile=None, keyfile=None):
    token = validate_token(token if token is not None else os.environ.get("META_HUB_TOKEN"))
    if not isinstance(host, str) or not host or any(c.isspace() for c in host):
        raise ValueError("Invalid bind address")
    if bool(certfile) != bool(keyfile):
        raise ValueError("Both TLS certificate and key are required")
    if not is_loopback(host) and not certfile:
        raise ValueError("Binding outside loopback requires TLS certificate and key")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("Invalid port")
    tls = None
    if certfile:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(certfile, keyfile)

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "MetaHarnessHub/0.5"
        sys_version = ""

        def log_message(self, *_):
            pass

        def _send(self, status, body):
            raw = _encode(body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(raw)

        def _authorize(self):
            headers = self.headers.get_all("Authorization", [])
            supplied = headers[0] if len(headers) == 1 else ""
            # bytes avoid non-ASCII compare_digest exceptions from untrusted headers.
            if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + token).encode("ascii")):
                self._send(401, {"error": "Authentication required"})
                return False
            if self.headers.get_all("Origin"):
                self._send(403, {"error": "Browser origin requests are not accepted"})
                return False
            return True

        def do_GET(self):
            if not self._authorize():
                return
            if self.path != "/v1/health":
                self._send(404, {"error": "Unknown endpoint"})
                return
            self._send(200, {"status": "ok", "protocol": "meta-harness-worker/1"})

        def do_POST(self):
            if not self._authorize():
                return
            routes = {
                "/v1/register": ({"worker_id", "capabilities"}, {"worker_id", "capabilities"}),
                "/v1/claim": ({"worker_id"}, {"worker_id"}),
                "/v1/heartbeat": ({"job_id", "worker_id", "lease_token"}, {"job_id", "worker_id", "lease_token"}),
                "/v1/finish": ({"job_id", "worker_id", "lease_token"}, {"job_id", "worker_id", "lease_token", "result", "error"}),
            }
            if self.path not in routes:
                self._send(404, {"error": "Unknown endpoint"})
                return
            sizes = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding") or len(sizes) != 1 or len(sizes[0]) > 7 or not sizes[0].isascii() or not sizes[0].isdigit():
                self._send(400, {"error": "One Content-Length header is required; transfer encoding is unsupported"})
                return
            size = int(sizes[0])
            if not 0 < size <= MAX_BODY:
                self._send(413, {"error": "Request body exceeds limit"})
                return
            if self.headers.get_content_type() != "application/json":
                self._send(415, {"error": "Content-Type must be application/json"})
                return
            try:
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("Incomplete body")
                body = _decode(raw)
                required, allowed = routes[self.path]
                if not isinstance(body, dict) or not required <= body.keys() or body.keys() - allowed:
                    raise ValueError("Invalid fields")
                if self.path == "/v1/register":
                    result = queue.register_worker(**body)
                elif self.path == "/v1/claim":
                    result = queue.claim(**body)
                elif self.path == "/v1/heartbeat":
                    result = queue.heartbeat(**body)
                else:
                    result = queue.finish(**body)
                self._send(200, {"data": result})
            except KeyError:
                self._send(404, {"error": "Resource not found"})
            except (ValueError, TypeError, RecursionError):
                self._send(400, {"error": "Invalid request or stale lease"})
            except (TimeoutError, socket.timeout):
                self._send(408, {"error": "Request timeout"})
            except Exception:
                self._send(500, {"error": "Coordinator request failed"})

    server_type = _BoundedServer
    if ":" in host:
        class IPv6Server(_BoundedServer):
            address_family = socket.AF_INET6
        server_type = IPv6Server
    server = server_type((host, port), Handler)
    if tls:
        # A peer that never completes TLS must not block the accept loop.
        server.socket = tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise TransportError("Hub redirects are forbidden", status=code)


class Client:
    def __init__(self, url, token, timeout=8, ca_file=None):
        self.url = validate_url(url)
        self._token = validate_token(token)
        self.timeout = timeout
        context = ssl.create_default_context(cafile=ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # Ignore environment HTTP proxies: a bearer token must only reach this hub.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            _NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def request(self, path, body=None):
        if path not in ("/v1/health", "/v1/register", "/v1/claim", "/v1/heartbeat", "/v1/finish"):
            raise ValueError("Unsupported worker endpoint")
        data = _encode(body) if body is not None else None
        req = urllib.request.Request(self.url + path, data=data,
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json", "Accept": "application/json"},
            method="GET" if body is None else "POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise TransportError("Hub response exceeds size limit")
                if response.headers.get_content_type() != "application/json":
                    raise TransportError("Hub returned an unsupported content type")
                try:
                    decoded = _decode(raw)
                except ValueError:
                    raise TransportError("Hub returned invalid JSON") from None
                if not isinstance(decoded, dict):
                    raise TransportError("Hub returned an invalid response envelope")
                if path == "/v1/health":
                    return decoded
                if "data" not in decoded:
                    raise TransportError("Hub returned an invalid response envelope")
                return decoded["data"]
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise TransportError(f"Hub rejected request (HTTP {status})", status=status, retryable=status in (408, 429, 500, 502, 503, 504)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransportError("Hub connection failed", retryable=True) from None

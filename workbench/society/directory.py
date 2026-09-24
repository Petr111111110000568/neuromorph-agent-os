"""Read-only discovery of public agent, model and dataset directory cards.

No card endpoint is contacted, README interpreted, code/weights downloaded, or
agent joined. Directory identity, license and capabilities are self-reported.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .evidence import _plain

PROVIDERS = {
    "agentverse": "https://agentverse.ai/v1/search/agents",
    "huggingface_models": "https://huggingface.co/api/models",
    "huggingface_datasets": "https://huggingface.co/api/datasets",
}
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUERY_CHARACTERS = 500
MAX_ITEMS = 8
TIMEOUT_SECONDS = 12
READ_BUDGET_SECONDS = 24
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_REGISTRY_FILES = 32
MAX_REGISTRY_RECORDS = 2000
_HF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,95})?\Z")
_AGENT_ADDRESS = re.compile(r"agent1[a-z0-9]{20,120}\Z")
_COMMON_LIMITS = [
    "Карточка каталога — сведения поставщика или автора; идентичность оператора и заявленные возможности независимо не подтверждены.",
    "Ресурс не подключён, код и веса не загружены; endpoint агента, README и инструкции карточки не исполнялись.",
    "Наличие в каталоге не подтверждает качество, научную применимость, лицензионные права или возможность изменения организма.",
]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Directory redirect rejected")


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _text(value, bound=200):
    if not isinstance(value, str):
        return ""
    return _plain(value[: min(len(value), 12000)], 12000)[:bound]


def _terms(value, maximum=12):
    if not isinstance(value, list):
        return []
    result = []
    for term in value[:100]:
        term = _text(term, 100)
        if term and term not in result:
            result.append(term)
        if len(result) >= maximum:
            break
    return result


def _safe_public_url(value):
    if not isinstance(value, str) or not value or len(value) > 2000 or re.search(r"[\x00-\x20\x7f\\]", value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        _ = parsed.port
    except ValueError:
        return ""
    return value


def _query(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_QUERY_CHARACTERS:
        raise ValueError("query must contain 1–500 text characters")
    if any(not c.isprintable() and c not in "\n\r\t" for c in value):
        raise ValueError("query contains unsupported control characters")
    return " ".join(value.split())


def _request(query, provider, limit):
    headers = {"User-Agent": "Meta-Harness/0.6 public-directory-discovery",
               "Accept": "application/json", "Accept-Encoding": "identity"}
    if provider == "agentverse":
        headers["Content-Type"] = "application/json"
        body = {"search_text": query, "sort": "relevancy", "direction": "asc",
                "filters": {"protocol_digest": []}, "offset": 0, "limit": limit}
        return Request(PROVIDERS[provider], data=json.dumps(body).encode(), headers=headers, method="POST")
    args = {"search": query, "limit": limit}
    args["cardData" if provider == "huggingface_models" else "full"] = "true"
    return Request(PROVIDERS[provider] + "?" + urlencode(args), headers=headers)


def _fetch(query, provider, limit):
    request = _request(query, provider, limit)
    started = time.monotonic()
    # Respect the system proxy, required by managed environments; no token is read.
    with build_opener(_NoRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
        if response.geturl() != request.full_url or response.status != 200:
            raise ValueError("Unexpected directory response")
        if response.headers.get_content_type() not in {"application/json", "text/json"}:
            raise ValueError("Directory response is not JSON")
        if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
            raise ValueError("Compressed directory response rejected")
        size_header = response.headers.get("Content-Length")
        if size_header is not None and (not size_header.isascii() or not size_header.isdecimal() or int(size_header) > MAX_RESPONSE_BYTES):
            raise ValueError("Invalid directory Content-Length")
        chunks, total = [], 0
        reader = getattr(response, "read1", response.read)
        while True:
            if time.monotonic() - started > READ_BUDGET_SECONDS:
                raise TimeoutError("Directory read budget exceeded")
            chunk = reader(min(16384, MAX_RESPONSE_BYTES + 1 - total))
            if time.monotonic() - started > READ_BUDGET_SECONDS:
                raise TimeoutError("Directory read budget exceeded")
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ValueError("Directory response exceeds size limit")
            chunks.append(chunk)
    def reject(value):
        raise ValueError("Non-finite directory JSON rejected")
    return json.loads(b"".join(chunks).decode("utf-8"), parse_constant=reject)


def _parse(payload, provider, limit):
    rows = payload.get("agents") if provider == "agentverse" and isinstance(payload, dict) else payload
    if not isinstance(rows, list) or len(rows) > MAX_ITEMS:
        raise ValueError("Invalid or excessive directory result list")
    stamp, items = _timestamp(), {}
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        if row.get("private") is not None and row.get("private") is not False:
            continue
        capabilities, license_text, revision = [], "not_specified", ""
        provenance = {"provider": provider, "retrieved_at": stamp, "endpoint": PROVIDERS[provider],
                      "identity_verification": "directory_self_reported", "endpoint_contacted": False}
        if provider == "agentverse":
            ident = row.get("address", "")
            if not isinstance(ident, str) or not _AGENT_ADDRESS.fullmatch(ident):
                continue
            name = _text(row.get("name"), 240) or ident
            # Provider homepage is intentionally used instead of guessing a profile
            # route or following endpoint URLs supplied by an untrusted agent card.
            url = "https://agentverse.ai/"
            entity_type = "agent"
            organization = "Оператор карточки не подтверждён"
            protocols = _terms(row.get("protocols"), 8)
            capabilities = ["Заявленный протокол: " + p for p in protocols]
            capabilities += _terms(row.get("system_wide_tags"), 4)
            provenance.update({"directory_address": ident, "listed_status": _text(row.get("status"), 40),
                               "listed_type": _text(row.get("type"), 40), "owner_account": _text(row.get("owner"), 160),
                               "profile_url_verified": False})
        else:
            ident = row.get("id", "")
            if not isinstance(ident, str) or not _HF_ID.fullmatch(ident) or any(p in {".", ".."} for p in ident.split("/")):
                continue
            name = ident
            url = "https://huggingface.co/" + ("datasets/" if provider == "huggingface_datasets" else "") + quote(ident, safe="/")
            entity_type = "model" if provider == "huggingface_models" else "dataset"
            owner = _text(row.get("author"), 120) or (ident.split("/")[0] if "/" in ident else "")
            organization = "Аккаунт каталога: " + owner if owner else "Оператор карточки не подтверждён"
            capabilities = _terms(row.get("tags"), 12)
            card = row.get("cardData") if isinstance(row.get("cardData"), dict) else {}
            license_value = card.get("license")
            if isinstance(license_value, str):
                license_text = _text(license_value, 120) or "not_specified"
            elif isinstance(license_value, list):
                license_text = ", ".join(_terms(license_value, 4))[:240] or "not_specified"
            else:
                license_text = next((tag[8:] for tag in capabilities if tag.startswith("license:")), "not_specified")
            raw_sha = row.get("sha", "")
            revision = raw_sha if isinstance(raw_sha, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", raw_sha) else ""
            provenance.update({"gated": row.get("gated") if isinstance(row.get("gated"), (str, bool)) else None,
                               "downloads": row.get("downloads") if type(row.get("downloads")) is int and 0 <= row["downloads"] <= 10**15 else None,
                               "license_verification": "card_label_only_not_license_audit"})
        item = {"id": provider + ":" + ident, "name": name, "organization": organization,
                "url": url, "entity_type": entity_type, "status": "directory_listed_not_connected",
                "capabilities": capabilities, "license": license_text, "revision": revision,
                "provenance": provenance, "limitations": list(_COMMON_LIMITS)}
        if provider == "agentverse":
            item["limitations"].append("Ссылка ведёт на каталог; точный адрес агента сохранён в provenance. Активность из карточки не проверялась вызовом агента.")
        if provider == "huggingface_datasets":
            item["limitations"].append("Публичная карточка не гарантирует свободный доступ к данным или право на выбранную исследовательскую цель.")
        items.setdefault(item["id"], item)
    return list(items.values())[:limit]


def _offline(query, provider, limit, root):
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    paths = sorted((base / "data").glob("agents_*.json"))
    if len(paths) > MAX_REGISTRY_FILES:
        raise ValueError("Curated registry exceeds file bound")
    terms = set(re.findall(r"[\w-]{2,}", query.casefold()))
    ranked, count, stamp = [], 0, _timestamp()
    for path in paths:
        if path.is_symlink() or path.stat().st_size > MAX_REGISTRY_BYTES:
            raise ValueError("Invalid or excessive curated registry file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Curated registry must be a list")
        count += len(data)
        if count > MAX_REGISTRY_RECORDS:
            raise ValueError("Curated registry exceeds record bound")
        for row in data:
            if not isinstance(row, dict):
                continue
            ident, name = _text(row.get("id"), 160), _text(row.get("name"), 240)
            url = _safe_public_url(row.get("source_url"))
            if not ident or not name or not url:
                continue
            content = " ".join([name, _text(row.get("organization"), 240), * _terms(row.get("capabilities"), 12)]).casefold()
            score = sum(term in content for term in terms)
            if not score:
                continue
            ranked.append((score, {"id": "registry:" + ident, "name": name,
                "organization": _text(row.get("organization"), 240), "url": url,
                "entity_type": _text(row.get("entity_type"), 40) or "tool",
                "status": "directory_listed_not_connected", "capabilities": _terms(row.get("capabilities"), 12),
                "license": _text(row.get("license"), 240) or "not_specified", "revision": "",
                "provenance": {"provider": provider, "retrieval_kind": "bundled_registry", "retrieved_at": stamp,
                               "identity_verification": "curated_documentation_only", "endpoint_contacted": False},
                "limitations": list(_COMMON_LIMITS) + ["Offline: поиск по общему локальному реестру; выбранный внешний каталог не опрашивался."]}))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    unique = {}
    for _, item in ranked:
        unique.setdefault(item["id"], item)
    return list(unique.values())[:limit]


def discover(query, provider="agentverse", limit=5, offline=False, root=None):
    query = _query(query)
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("Unsupported public directory provider")
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise ValueError("limit must be an integer between 1 and 8")
    if type(offline) is not bool:
        raise ValueError("offline must be boolean")
    mode = "offline_registry" if offline else "public_directory"
    items, errors = [], []
    try:
        items = _offline(query, provider, limit, root) if offline else _parse(_fetch(query, provider, limit), provider, limit)
    except HTTPError as exc:
        errors.append({"provider": provider, "code": "http_error", "http_status": exc.code,
                       "message": "Каталог отклонил запрос; автоматических повторов не было."})
    except (TimeoutError, socket.timeout):
        errors.append({"provider": provider, "code": "timeout", "message": "Истекло время запроса каталога."})
    except (URLError, OSError):
        errors.append({"provider": provider, "code": "read_error", "message": "Источник каталога недоступен."})
    except (ValueError, TypeError, UnicodeError, RecursionError):
        errors.append({"provider": provider, "code": "invalid_response", "message": "Каталог не прошёл проверку формата или лимитов."})
    return {"query": query, "provider": provider, "mode": mode, "items": items,
            "errors": errors, "requests": 0 if offline else 1}

"""Bounded public-abstract triage; source text is data, never an instruction.

This adapter does not assess biological validity or recommend interventions.
It returns only short excerpts, content hashes and explicit lexical indicators.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..network.discovery import catalog, rank_resources

ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
MAX_RESPONSE_BYTES = 1_000_000
MAX_ABSTRACT_CHARACTERS = 100_000
MAX_QUERY_CHARACTERS = 2_000
MAX_ITEMS = 8
TIMEOUT_SECONDS = 12
READ_BUDGET_SECONDS = 24
EXCERPT_WORDS = 12

# These labels mean only that the indicated words occur in the abstract.
# For example, "no human data" still matches human AND negation_present.
INDICATOR_TERMS = {
    "in_vitro": ("in vitro", "cell culture", "organoid", "organoids"),
    "animal": ("mouse", "mice", "murine", "rat", "rats", "zebrafish", "animal model", "animal models"),
    "human": ("human", "humans", "patient", "patients", "clinical trial", "clinical trials"),
    "computational": ("simulation", "simulations", "in silico", "machine learning", "computational model", "computational models"),
    "systematic_review": ("systematic review", "meta-analysis", "meta analysis"),
    "randomized": ("randomized", "randomised"),
    "negation_present": ("no", "not", "without", "lack", "lacking"),
}
_PATTERNS = {
    key: [(term, re.compile(r"(?<!\w)" + re.escape(term).replace(r"\ ", r"\s+") + r"(?!\w)", re.I))
          for term in terms]
    for key, terms in INDICATOR_TERMS.items()
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Provider redirect rejected")


class _PlainText(HTMLParser):
    """Drop markup and executable-style elements without interpreting them."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.hidden.append(tag)
        elif not self.hidden:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        else:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _plain(value, maximum):
    if not isinstance(value, str):
        return ""
    if len(value) > maximum:
        raise ValueError("Source text exceeds character limit")
    parser = _PlainText()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    return " ".join("".join(c for c in text if c.isprintable() or c.isspace()).split())


def _query(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_QUERY_CHARACTERS:
        raise ValueError("query must contain 1–2000 text characters")
    if any(not c.isprintable() and c not in "\n\r\t" for c in value):
        raise ValueError("query contains unsupported control characters")
    return " ".join(value.split())


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _reject_constant(value):
    raise ValueError("Non-finite JSON is unsupported")


def _fetch(query, limit):
    request = Request(ENDPOINT + "?" + urlencode({
        "query": query, "format": "json", "pageSize": limit, "resultType": "core",
    }), headers={
        "User-Agent": "Meta-Harness/0.6 public-abstract-triage",
        "Accept": "application/json", "Accept-Encoding": "identity",
    })
    started = time.monotonic()
    # The HTTPS target is fixed. Normal system proxy settings are respected:
    # managed workspaces may require their outbound proxy for internet access.
    with build_opener(_NoRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
        if response.geturl() != request.full_url:
            raise ValueError("Unexpected provider response URL")
        if response.headers.get_content_type() not in {"application/json", "text/json"}:
            raise ValueError("Provider response is not JSON")
        if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
            raise ValueError("Compressed payloads are unsupported")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
            raise ValueError("Invalid or excessive Content-Length")
        parts, total = [], 0
        reader = getattr(response, "read1", response.read)
        while True:
            if time.monotonic() - started > READ_BUDGET_SECONDS:
                raise TimeoutError("Response read budget exceeded")
            part = reader(min(16_384, MAX_RESPONSE_BYTES + 1 - total))
            if time.monotonic() - started > READ_BUDGET_SECONDS:
                raise TimeoutError("Response read budget exceeded")
            if not part:
                break
            total += len(part)
            if total > MAX_RESPONSE_BYTES:
                raise ValueError("Provider response exceeds byte limit")
            parts.append(part)
    return json.loads(b"".join(parts).decode("utf-8"), parse_constant=_reject_constant)


def _indicators(abstract):
    matches = {
        key: [term for term, pattern in patterns if pattern.search(abstract)]
        for key, patterns in _PATTERNS.items()
    }
    return {key: terms for key, terms in matches.items() if terms}


def _parse(payload, limit):
    if not isinstance(payload, dict) or not isinstance(payload.get("resultList"), dict):
        raise ValueError("Invalid provider result object")
    rows = payload["resultList"].get("result")
    if not isinstance(rows, list):
        raise ValueError("Invalid provider result list")
    if len(rows) > MAX_ITEMS:
        raise ValueError("Provider exceeded requested result bound")
    now, result = _timestamp(), {}
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        title = _plain(row.get("title"), 4_000)[:1_000]
        if not title:
            continue
        doi = row.get("doi", "")
        doi = doi.strip() if isinstance(doi, str) else ""
        if not re.fullmatch(r"10\.\d{4,9}/[^\s\\\x00-\x1f\x7f]{1,400}", doi):
            doi = ""
        source, source_id = row.get("source", ""), row.get("id", "")
        source = source if isinstance(source, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,20}", source) else ""
        source_id = source_id if isinstance(source_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", source_id) else ""
        url = ("https://doi.org/" + quote(doi, safe="/")) if doi else (
            "https://europepmc.org/article/" + quote(source, safe="") + "/" + quote(source_id, safe="")
            if source and source_id else "")
        identity = "doi:" + doi.casefold() if doi else "europepmc:" + hashlib.sha256((source + ":" + source_id + ":" + title).encode()).hexdigest()[:24]
        abstract = _plain(row.get("abstractText"), MAX_ABSTRACT_CHARACTERS)
        matches = _indicators(abstract)
        limitations = [
            "Индикаторы — совпадения слов в аннотации; дизайн исследования и истинность выводов не проверены.",
            "Словарь английский; отсутствие совпадения не доказывает отсутствия метода, эффекта или группы исследования.",
            "Аннотация не заменяет методы, полный текст, проверку независимых репликаций и области применимости.",
            "Хеш фиксирует очищенный текст на момент получения; это не доказательство достоверности или неизменности публикации.",
            "Результат не является рекомендацией вмешательства или прогнозом изменений организма.",
        ]
        if not abstract:
            limitations.append("Провайдер не вернул читаемую аннотацию; получены только метаданные.")
        if "negation_present" in matches:
            limitations.append("В тексте встречаются отрицания; ключевое слово может описывать отсутствие эффекта или данных.")
        item = {
            "id": identity, "title": title, "url": url, "doi": doi,
            "abstract_available": bool(abstract),
            "abstract_sha256": hashlib.sha256(abstract.encode("utf-8")).hexdigest() if abstract else None,
            "indicators": list(matches), "indicator_matches": matches,
            "provenance": {"provider": "europepmc", "retrieved_at": now,
                           "endpoint": ENDPOINT, "result_type": "core",
                           "source": source, "source_id": source_id,
                           "hash_basis": "UTF-8 HTML-stripped whitespace-normalized abstract" if abstract else None},
            "limitations": limitations,
        }
        if abstract:
            words = abstract.split()
            item["excerpt"] = " ".join(words[:EXCERPT_WORDS]) + ("…" if len(words) > EXCERPT_WORDS else "")
        if identity not in result or (item["abstract_available"] and not result[identity]["abstract_available"]):
            result[identity] = item
    return list(result.values())[:limit]


def _offline(query, limit, root):
    ranked = rank_resources(query, catalog(root))
    now = _timestamp()
    items = []
    for record in ranked:
        if record["score"] <= 0:
            continue
        provenance = record.get("provenance", {})
        items.append({
            "id": record["id"], "title": record["title"], "url": record["url"],
            "doi": provenance.get("doi", ""), "abstract_available": False,
            "abstract_sha256": None, "indicators": [], "indicator_matches": {},
            "provenance": {"provider": "bundled_catalog", "retrieved_at": now,
                           "retrieval_kind": "local_index_only", "origin": provenance.get("origin", "bundled")},
            "limitations": ["Offline: совпадение с локальным каталогом; веб-источник и аннотация не загружались.",
                            "Запись может описывать инструмент или платформу, а не научную публикацию.",
                            "Лексическая релевантность не подтверждает научную применимость."],
        })
        if len(items) == limit:
            break
    return items


def collect(query, limit=5, offline=False, root=None):
    """Collect at most eight metadata/abstract triage records.

    Invalid caller inputs raise ValueError. Provider failures are structured,
    contain no remote body/exception detail, and are never retried implicitly.
    """
    query = _query(query)
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise ValueError("limit must be an integer between 1 and 8")
    if type(offline) is not bool:
        raise ValueError("offline must be boolean")
    if offline:
        return {"query": query, "mode": "offline_index", "items": _offline(query, limit, root), "errors": [], "requests": 0}
    items, errors = [], []
    try:
        items = _parse(_fetch(query, limit), limit)
    except HTTPError as exc:
        errors.append({"provider": "europepmc", "code": "http_error", "http_status": exc.code,
                       "message": "Europe PMC отклонил запрос; автоматических повторов не было."})
    except (TimeoutError, socket.timeout):
        errors.append({"provider": "europepmc", "code": "timeout", "message": "Истекло время ожидания Europe PMC."})
    except (URLError, OSError):
        errors.append({"provider": "europepmc", "code": "network_error", "message": "Сетевой запрос к Europe PMC не удался."})
    except (ValueError, TypeError, UnicodeError, RecursionError):
        errors.append({"provider": "europepmc", "code": "invalid_response", "message": "Ответ Europe PMC не прошёл проверку формата или лимитов."})
    return {"query": query, "mode": "public_abstract_triage", "items": items, "errors": errors, "requests": 1}

"""Bounded public metadata discovery. No downloaded code is loaded or executed."""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROVIDERS = {
    "europepmc": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
    "crossref": "https://api.crossref.org/works",
}
MAX_RESPONSE_BYTES = 1_000_000
TIMEOUT_SECONDS = 12
MAX_RESOURCES = 2000
BUILTINS = {"builtin-coupled-dynamics": "coupled_dynamics", "builtin-regression": "regression_benchmark",
            "builtin-quantum": "quantum_circuit", "builtin-topology": "legacy_topology",
            "builtin-structural-plasticity": "structural_plasticity",
            "builtin-kan": "kan_benchmark", "builtin-cortical": "cortical_sequence"}
STOP_WORDS = {"the", "and", "for", "with", "from", "into", "что", "как", "для", "или", "это", "при", "его", "она", "они", "все", "чем"}
CONCEPTS = {
    "genomics": ("геном", "генет", "genom", "genetic", "alphagenome", "dna"),
    "epigenomics": ("эпиген", "epigen", "methyl", "метил", "chromatin", "хроматин", "atac"),
    "single_cell": ("клеточ", "single cell", "single-cell", "scrna", "scvi", "multiomic"),
    "tissue_simulation": ("ткан", "орган", "tissue", "organism", "physicell", "organ " ),
    "ml_benchmark": ("машин", "регресс", "benchmark", "regression", "machine learning", "holdout", " ml "),
    "quantum_simulation": ("квант", "quantum", "qubit", "qiskit"),
    "workflow": ("оркестр", "workflow", "nextflow", "pipeline", "конвейер", "воспроизвод"),
    "literature": ("источник", "литератур", "публикац", "evidence", "literature", "publication"),
    "kan_benchmark": ("колмогор", "калмагор", "kolmogorov", " kan ", "fastkan", "wav-kan"),
    "cortical_sequence": ("кортик", "неокорт", "cortical", "corticomorph"),
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Provider redirect rejected")


def _clean(value, limit=2000):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c for c in value if ord(c) >= 32).split())[:limit]


def _query(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000 or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("query must contain 1–2000 text characters")
    return " ".join(value.split())


def _url(value):
    value = _clean(value, 2048)
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return ""
        if any(c.isspace() for c in value) or "\\" in value:
            return ""
        return value
    except ValueError:
        return ""


def _list_text(value, limit=30):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [text for text in (_clean(item, 1000) for item in value[:limit]) if text]


def _concepts(text):
    text = " " + text.casefold().replace("ё", "е") + " "
    concepts = {key for key, patterns in CONCEPTS.items() if any(pattern in text for pattern in patterns)}
    if _structural_topic(text):
        concepts.add("structural_plasticity")
    return concepts


def _structural_topic(text):
    """Topic keywords select a synthetic control, not a biological mechanism."""
    return (any(term in text for term in ("морфоген", "morphogenesis", "савельев"))
            or bool(re.search(r"structural[\s-]+plasticity|структурн[\w-]*[^.!?\n]{0,80}пластич", text)))


def select_builtin(question):
    """Choose one allowlisted synthetic control by topic; never interpret code.

    Explicit KAN/cortical topics precede structural and quantum topics. A
    person's name is only a lexical route, not attribution of an algorithm.
    The original question is not returned or passed as simulation parameters.
    """
    text = _query(question).casefold().replace("ё", "е")
    if re.search(r'\bkan\b|колмогор|калмагор|kolmogorov|fastkan|wav[- ]?kan', text):
        return "kan_benchmark"
    if any(term in text for term in ("кортик", "неокорт", "cortical", "corticomorph")):
        return "cortical_sequence"
    if _structural_topic(text):
        return "structural_plasticity"
    if any(term in text for term in ("quantum", "квант")):
        return "quantum_circuit"
    return "regression_benchmark"


def _normalize(raw, origin="catalog", trusted=False):
    if not isinstance(raw, dict) or not _clean(raw.get("title")):
        return None
    title = _clean(raw["title"], 500)
    url = _url(raw.get("url", ""))
    description = _clean(raw.get("description") or raw.get("summary") or raw.get("integration") or raw.get("claim") or raw.get("evidence"))
    identity = _clean(raw.get("id"), 180) or "resource-" + hashlib.sha256((url + title).encode()).hexdigest()[:20]
    caps = _list_text(raw.get("capabilities"))
    if not caps:
        caps = sorted(_concepts(" ".join((title, description, _clean(raw.get("category"))))))
    result = {
        "id": identity, "title": title, "url": url, "description": description,
        "capabilities": caps, "kind": _clean(raw.get("kind") or raw.get("category"), 100) or "reference",
        "access": _clean(raw.get("access"), 150) or "not_verified",
        "status": _clean(raw.get("status"), 100) or "metadata_unverified",
        "provenance": {"origin": origin, "source_url": url},
        "limitations": _list_text(raw.get("limitations")) or ["Метаданные не подтверждают применимость или научную достоверность."],
        "adapter": {"available": False, "plugin_id": None},
    }
    if trusted and identity in BUILTINS:
        result["adapter"] = {"available": True, "plugin_id": BUILTINS[identity]}
    # Preserve only safe provenance identifiers; never propagate arbitrary provider fields.
    if isinstance(raw.get("provenance"), dict):
        for key in ("provider", "retrieved_at", "doi", "source_id", "filename", "bytes", "modified_ns", "job_id", "imported_at"):
            val = raw["provenance"].get(key)
            if isinstance(val, str):
                result["provenance"][key] = _clean(val, 500)
            elif type(val) is int and val >= 0:
                result["provenance"][key] = val
    return result


def _records(value):
    if isinstance(value, dict):
        value = value.get("resources", value.get("sources", value.get("items", [])))
    return value[:MAX_RESOURCES] if isinstance(value, list) else []


def catalog(root, extra=None):
    """Combine bundled reviewed references and optional untrusted metadata.

    Invalid bundled files fail explicitly rather than silently hiding catalog errors.
    Resources cannot become executable adapters through the extra metadata argument.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    data = root / "data"
    items = {}
    paths = [data / "resource_catalog.json", *sorted(data.glob("*_sources.json"))]
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        if path.stat().st_size > 2_000_000:
            raise ValueError("Bundled catalog exceeds size limit")
        records = _records(json.loads(path.read_text(encoding="utf-8")))
        for raw in records:
            entry = _normalize(raw, origin="bundled:" + path.name, trusted=path.name == "resource_catalog.json")
            if entry and entry["id"] not in items:
                items[entry["id"]] = entry
    for raw in _records(extra or []):
        entry = _normalize(raw, origin="discovered_metadata")
        if entry and entry["id"] not in items:
            items[entry["id"]] = entry
    return list(items.values())[:MAX_RESOURCES]


def rank_resources(question, resources):
    """Explainable lexical/concept ranking; scores are relevance, not probabilities."""
    question = _query(question)
    tokens = set(re.findall(r"[\w-]{3,}", question.casefold())) - STOP_WORDS
    desired = _concepts(question)
    ranked = []
    for raw in _records(resources):
        item = _normalize(raw)
        if item is None:
            continue
        # Only callers with the trusted catalog record can expose an existing adapter.
        original_adapter = raw.get("adapter", {})
        expected = BUILTINS.get(item["id"])
        available = bool(expected and isinstance(original_adapter, dict) and original_adapter.get("available") is True and original_adapter.get("plugin_id") == expected)
        item["adapter"] = {"available": available, "plugin_id": expected if available else None}
        text = " ".join((item["title"], item["description"], " ".join(item["capabilities"]))).casefold()
        terms = sorted(token for token in tokens if token in text)
        matched_concepts = sorted(desired & (set(item["capabilities"]) | _concepts(text)))
        score = 3 * len(terms) + 5 * len(matched_concepts)
        item["score"] = score
        item["match_explanation"] = {"terms": terms, "capabilities": matched_concepts,
            "method": "3 × совпавшие слова + 5 × совпавшие тематические группы; без оценки научной истинности"}
        item["computability"] = {"adapter_available": available,
            "mode": "builtin_synthetic_control" if available else "metadata_only",
            "reason": "Доступен встроенный абстрактный расчёт; биологическая валидация отсутствует." if available else "Исполняемый адаптер в этой сборке отсутствует; документация не является подключением."}
        ranked.append(item)
    return sorted(ranked, key=lambda item: (-item["score"], item["title"].casefold(), item["id"]))


def _fetch_json(provider, query, limit):
    endpoint = PROVIDERS[provider]
    params = {"query": query, "format": "json", "pageSize": limit, "resultType": "lite"} if provider == "europepmc" else {"query.bibliographic": query, "rows": limit, "select": "DOI,title,URL,type,published"}
    req = Request(endpoint + "?" + urlencode(params), headers={"User-Agent": "Meta-Harness/0.5 metadata-discovery", "Accept": "application/json", "Accept-Encoding": "identity"})
    with build_opener(_NoRedirect()).open(req, timeout=TIMEOUT_SECONDS) as response:
        if response.geturl() != req.full_url:
            raise ValueError("Unexpected provider response URL")
        if response.headers.get_content_type() not in ("application/json", "text/json"):
            raise ValueError("Provider did not return JSON")
        encoding = response.headers.get("Content-Encoding", "identity").lower()
        if encoding not in ("", "identity"):
            raise ValueError("Compressed provider payload is unsupported")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_RESPONSE_BYTES:
            raise ValueError("Provider response exceeds byte limit")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Provider response exceeds byte limit")
    return json.loads(body.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON")))


def _parse(provider, payload, limit):
    if not isinstance(payload, dict):
        raise ValueError("Invalid provider object")
    if provider == "europepmc":
        group = payload.get("resultList")
        values = group.get("result") if isinstance(group, dict) else None
    else:
        group = payload.get("message")
        values = group.get("items") if isinstance(group, dict) else None
        if payload.get("status") != "ok":
            raise ValueError("Crossref returned non-ok status")
    if not isinstance(values, list):
        raise ValueError("Invalid provider result list")
    now = datetime.now(timezone.utc).isoformat()
    items = []
    for row in values[:limit]:
        if not isinstance(row, dict):
            continue
        title = row.get("title")
        if provider == "crossref":
            title = title[0] if isinstance(title, list) and title else ""
        title = _clean(title, 500)
        if not title:
            continue
        doi = _clean(row.get("doi") if provider == "europepmc" else row.get("DOI"), 200)
        source_id = _clean(row.get("id"), 150)
        source = _clean(row.get("source"), 20)
        url = "https://doi.org/" + quote(doi, safe="/") if doi.startswith("10.") else _url(row.get("URL", ""))
        if not url and source_id and source:
            url = "https://europepmc.org/article/" + quote(source, safe="") + "/" + quote(source_id, safe="")
        identity = "doi:" + doi.lower() if doi.startswith("10.") else provider + ":" + hashlib.sha256((url + title).encode()).hexdigest()[:20]
        raw = {"id": identity, "title": title, "url": url, "kind": "publication_metadata",
            "description": "Библиографические метаданные из " + provider + "; полный текст не загружен.",
            "capabilities": sorted(_concepts(title)) or ["literature"], "status": "metadata_unverified", "access": "public_metadata",
            "provenance": {"provider": provider, "retrieved_at": now, "doi": doi, "source_id": source_id},
            "limitations": ["Попадание в индекс и название не подтверждают качество, воспроизводимость или применимость работы.", "Полный текст, лицензия и доступность кода требуют отдельной проверки."]}
        items.append(_normalize(raw, origin="public_metadata_api"))
    return items


def discover(query, providers=None, limit=8, offline=False, root=None):
    query = _query(query)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be an integer between 1 and 20")
    if type(offline) is not bool:
        raise ValueError("offline must be boolean")
    providers = list(PROVIDERS) if providers is None else providers
    if not isinstance(providers, list) or len(providers) > 2 or any(not isinstance(x, str) or x not in PROVIDERS for x in providers):
        raise ValueError("providers must contain only europepmc and crossref")
    providers = list(dict.fromkeys(providers))
    if offline:
        matched = [x for x in rank_resources(query, catalog(root)) if x["score"] > 0]
        return {"query": query, "items": matched[:limit], "errors": [], "requests": 0, "mode": "offline_catalog"}
    if not providers:
        raise ValueError("At least one online provider is required")
    items, errors, batches = {}, [], []
    for provider in providers:
        try:
            batches.append(_parse(provider, _fetch_json(provider, query, limit), limit))
        except HTTPError as exc:
            errors.append({"provider": provider, "code": "http_error", "http_status": exc.code, "message": "Провайдер отклонил запрос; повтор автоматически не выполнялся."})
        except (TimeoutError, socket.timeout):
            errors.append({"provider": provider, "code": "timeout", "message": "Истекло время ожидания метаданных."})
        except (URLError, OSError):
            errors.append({"provider": provider, "code": "network_error", "message": "Сетевой доступ к провайдеру не удался."})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            errors.append({"provider": provider, "code": "invalid_response", "message": "Ответ провайдера не прошёл проверку формата или лимитов."})
    # Interleave provider results so one full response does not hide another provider.
    for index in range(limit):
        for batch in batches:
            if index < len(batch):
                item = batch[index]
                items.setdefault(item["id"], item)
    return {"query": query, "items": list(items.values())[:limit], "errors": errors, "requests": len(providers), "mode": "public_metadata"}


def plan(question, resources):
    question = _query(question)
    ranked = rank_resources(question, resources)
    relevant = [x for x in ranked if x["score"] > 0][:12]
    groups = _concepts(question)
    biological = bool(groups & {"genomics", "epigenomics", "single_cell", "tissue_simulation", "structural_plasticity"})
    gaps = ["Нет валидированной модели, переводящей желаемые признаки взрослого организма в безопасные генетические или эпигенетические изменения."] if biological else []
    if not relevant:
        gaps.append("Локальный каталог не содержит совпадений; требуется поиск и проверка первичных источников.")
    unavailable = [x["title"] for x in relevant if not x["computability"]["adapter_available"] and x["kind"] != "publication_metadata"]
    if unavailable:
        gaps.append("Нет исполняемых адаптеров для найденных внешних ресурсов: " + "; ".join(unavailable[:6]))
    return {"question": question, "mode": "rule_based", "ranked_resources": relevant, "gaps": gaps,
        "steps": [
            {"id": "scope", "kind": "research_design", "title": "Уточнить измеримые исходы, область применимости и критерии опровержения", "status": "planned", "resource_ids": []},
            {"id": "evidence", "kind": "metadata_discovery", "title": "Собрать первичные источники и проверить методы, ограничения и независимые репликации", "status": "planned", "resource_ids": [x["id"] for x in relevant]},
            {"id": "model_fit", "kind": "model_validation", "title": "Сопоставить модели с типом данных, тканью, масштабом и допустимой областью экстраполяции", "status": "planned", "resource_ids": [x["id"] for x in relevant if x["kind"] != "publication_metadata"]},
            {"id": "controls", "kind": "synthetic_control", "title": "Проверить вычислительный конвейер на синтетических контрольных задачах", "status": "planned", "resource_ids": [x["id"] for x in ranked if x["computability"]["adapter_available"]]},
            {"id": "validation", "kind": "model_validation", "title": "Оценить ошибки на отложенных данных, неопределённость и альтернативные объяснения", "status": "requires_data_and_adapter", "resource_ids": []},
            {"id": "report", "kind": "report", "title": "Сохранить происхождение результатов, нерешённые вопросы и следующий ограниченный поиск", "status": "planned", "resource_ids": []}],
        "limits": ["Планировщик использует явные правила и совпадения терминов; продуктивность предложенного порядка эмпирически не доказана.",
            "Метаданные ресурсов не являются научной валидацией; исполнение произвольных найденных программ отсутствует.",
            "Встроенные расчёты являются абстрактными контролями и не прогнозируют изменения человека.",
            "План не содержит последовательностей, генетических вмешательств, назначения процедур или инструкций изменения организма."]}


def inspect_local(directory):
    """Inspect names, sizes and mtimes only, with bounded depth and no symlinks."""
    if not isinstance(directory, (str, Path)) or not str(directory).strip():
        raise ValueError("An explicit local directory is required")
    base = Path(directory).expanduser().absolute()
    if any(p.is_symlink() for p in (base, *base.parents)):
        raise ValueError("Symlink directories are not accepted")
    if not base.is_dir():
        raise ValueError("Local resource directory does not exist")
    allowed = {".json", ".toml", ".yaml", ".yml", ".md", ".py", ".ipynb", ".c", ".cpp", ".h", ".jl", ".r"}
    sensitive = ("secret", "credential", "password", "token", "private", "id_rsa", "id_ed25519")
    stack, items, scanned, skipped = [(base, 0)], [], 0, 0
    while stack and scanned < 512 and len(items) < 128:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > 512 or len(items) >= 128:
                        break
                    name = entry.name
                    lower = name.casefold()
                    if entry.is_symlink() or name.startswith(".") or any(s in lower for s in sensitive) or lower in ("state", "node_modules", "__pycache__", "venv"):
                        skipped += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < 2:
                            stack.append((Path(entry.path), depth + 1))
                        else:
                            skipped += 1
                        continue
                    if not entry.is_file(follow_symlinks=False) or Path(name).suffix.casefold() not in allowed:
                        skipped += 1
                        continue
                    meta = entry.stat(follow_symlinks=False)
                    relative = str(Path(entry.path).relative_to(base))
                    identity = "local:" + hashlib.sha256((str(base) + "/" + relative).encode()).hexdigest()[:24]
                    raw = {"id": identity, "title": relative, "url": "", "kind": "local_file_metadata", "status": "metadata_only", "access": "local",
                        "description": "Локальный файл; содержание не прочитано, код не запускался.", "capabilities": ["local_inventory"],
                        "provenance": {"filename": relative, "bytes": meta.st_size, "modified_ns": meta.st_mtime_ns},
                        "limitations": ["Имя и расширение не подтверждают функциональность или пригодность файла."]}
                    items.append(_normalize(raw, origin="explicit_local_directory"))
        except OSError:
            skipped += 1
    return {"items": sorted(items, key=lambda x: x["title"].casefold()), "inspected_files": len(items), "scanned_entries": min(scanned, 512), "skipped": skipped,
        "limits": {"max_entries": 512, "max_files": 128, "max_depth": 2, "contents_read": False, "symlinks_followed": False},
        "truncated": bool(stack or scanned >= 512 or len(items) >= 128)}

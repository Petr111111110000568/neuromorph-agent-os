"""Persistent, bounded research society. Roles are workflows, not LLM identities."""
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ..store import canonical, now
from ..network.control import text, integer, boolean, fields
from ..network.discovery import select_builtin

TERMINAL = {"completed", "partial", "failed", "cancelled"}
KINDS = ("discovery", "evidence", "simulation")
LIMITS = [
    "Семь ролей выполняются программными правилами; внешние LLM и научные платформы не подключены автоматически.",
    "Аннотации и метаданные требуют проверки полного исследования; согласие ролей не является независимой репликацией.",
    "Контрольный расчёт использует синтетические данные и не предсказывает изменения взрослого организма.",
    "Сетевое выполнение поддерживает одну доверенную группу работников; A2A, Tor и федерация независимых организаций не реализованы.",
]
ROLES = [
    {"id": key, "name": name, "function": purpose, "implementation": "deterministic_workflow"}
    for key, name, purpose in [
        ("scout", "Разведчик источников", "Поиск общедоступных метаданных в фиксированных научных API"),
        ("curator", "Куратор", "Реестр организаций, лицензий, источников и происхождения результатов"),
        ("methodologist", "Методолог", "Отделение вопроса, области применимости и методического контроля"),
        ("implementer", "Инженер", "Постановка разрешённых заданий и инспекция закреплённых исходников"),
        ("reproducer", "Воспроизводитель", "Контрольный расчёт с seed, хешами входа, кода и результата"),
        ("critic", "Критик", "Проверка структуры результатов и выявление неполных данных"),
        ("coordinator", "Координатор", "Очередь, восстановление, лимиты, отмена и журнал утверждений"),
    ]
]


def _safe_url(value, online=False):
    if value == "" and not online:
        return True
    if not isinstance(value, str) or len(value) > 3000 or any(ord(c) < 33 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and (not online or parsed.scheme == "https" and parsed.hostname in {"doi.org", "europepmc.org"})
                and parsed.port in {None, 80, 443})
    except ValueError:
        return False


def _timestamp_valid(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and 2000 <= parsed.year and (parsed - datetime.now(timezone.utc)).total_seconds() <= 300
    except (ValueError, TypeError, AttributeError):
        return False


class Society:
    def __init__(self, root, state_dir, network_control):
        self.root, self.state_dir, self.network = Path(root), Path(state_dir), network_control
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.state_dir / "society.sqlite3"), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS missions(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS claims(id TEXT PRIMARY KEY,mission_id TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,created_at TEXT,payload TEXT NOT NULL);
        """)
        from .snapshots import SnapshotStore
        self.snapshots = SnapshotStore(self.root, self.state_dir)

    def close(self):
        with self.lock:
            self.snapshots.close()
            self.db.close()

    def agents(self, q=""):
        if not isinstance(q, str) or len(q) > 500:
            raise ValueError("q must contain at most 500 characters")
        records = []
        paths = list((self.root / "data").glob("agents_*.json"))
        if (self.root / "data/agent_societies.json").is_file():
            paths.append(self.root / "data/agent_societies.json")
        for path in sorted(paths):
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("Agent registry must contain an array")
            for entry in data:
                if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("id", "name", "organization")):
                    raise ValueError("Invalid agent registry record")
                entry = dict(entry, integration_status="documented_not_connected",
                             identity_verification="public_documentation_only")
                # One resource reviewed by two specialists must not become two agents.
                if entry["id"] == "osint-dbgap":
                    continue
                if entry["id"] == "proto-dbgap":
                    entry["also_reviewed_as"] = ["osint-dbgap"]
                if not q.strip() or q.strip().casefold() in canonical(entry).casefold():
                    records.append(entry)
        return {"items": records}

    def directory(self, body):
        fields(body, {"query", "provider", "limit", "offline", "data_class"})
        query = " ".join(text(body.get("query"), "query", 500).split())
        provider = body.get("provider", "agentverse")
        if provider not in {"agentverse", "huggingface_models", "huggingface_datasets"}:
            raise ValueError("Unsupported public directory provider")
        limit = integer(body.get("limit", 5), "limit", 1, 8)
        offline = boolean(body.get("offline", True), "offline")
        data_class = body.get("data_class", "public")
        if data_class not in {"public", "internal", "sensitive_genomic"}:
            raise ValueError("Unsupported data_class")
        if not offline and data_class != "public":
            raise ValueError("Public directory queries must not contain nonpublic data")
        return self.network.queue.submit("directory", {"query": query, "provider": provider, "limit": limit, "offline": offline},
                                         capabilities=["directory"], max_attempts=2)

    def directory_jobs(self):
        jobs = [j for j in self.network.queue.jobs() if j["kind"] == "directory"]
        for job in jobs:
            if job["status"] != "completed":
                continue
            result, payload = job.get("result"), job["payload"]
            valid = (isinstance(result, dict) and result.get("query") == payload["query"]
                     and result.get("provider") == payload["provider"]
                     and result.get("mode") == ("offline_registry" if payload["offline"] else "public_directory")
                     and isinstance(result.get("items"), list) and len(result["items"]) <= payload["limit"]
                     and isinstance(result.get("errors"), list) and type(result.get("requests")) is int
                     and result["requests"] == (0 if payload["offline"] else 1))
            if valid:
                for item in result["items"]:
                    if (not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("id", "name", "url"))
                            or not _safe_url(item["url"]) or item.get("status") != "directory_listed_not_connected"
                            or not isinstance(item.get("provenance"), dict)
                            or item["provenance"].get("provider") != payload["provider"]
                            or not _timestamp_valid(item["provenance"].get("retrieved_at"))):
                        valid = False
                        break
                    if not payload["offline"] and urlsplit(item["url"]).hostname != ("agentverse.ai" if payload["provider"] == "agentverse" else "huggingface.co"):
                        valid = False
                        break
            job["result_validation"] = "envelope_checked_not_external_identity_verified" if valid else "invalid_result"
            if not valid:
                job["status"], job["result"] = "invalid_result", None
                job["error"] = {"code": "invalid_directory_envelope", "message": "Untrusted result fields or provenance rejected"}
        return jobs

    def route(self, body):
        fields(body, {"question", "data_class", "network_layer"})
        question = text(body.get("question"), "question")
        data_class = body.get("data_class", "public")
        layer = body.get("network_layer", "clear_web")
        if data_class not in {"public", "internal", "sensitive_genomic"}:
            raise ValueError("Unsupported data_class")
        if layer not in {"clear_web", "authenticated", "private", "onion"}:
            raise ValueError("Unsupported network_layer")
        decision = "allowed"
        reasons = ["Разрешены публичные API метаданных и аннотаций; остальные сервисы требуют отдельного адаптера."]
        if layer == "onion":
            decision, reasons = "unsupported_transport", ["Tor-коннектор отсутствует; доступен только каталог документированных возможностей."]
        elif data_class != "public":
            decision, reasons = "local_only", ["В этой версии непубличные запросы обрабатываются только локально; внешняя передача отключена."]
        elif layer != "clear_web":
            decision, reasons = "requires_connection", ["Для закрытого ресурса нужны проверенные права конкретного пользователя и отдельный настроенный коннектор."]
        expanded = question.casefold()
        for ru, en in (("ген", "genomics biology"), ("эпиген", "epigenetics"), ("агент", "agent orchestration"),
                       ("квант", "quantum"), ("угроз", "threat intelligence"), ("стать", "literature paper")):
            if ru in expanded:
                expanded += " " + en
        terms = set(re.findall(r"[\w-]{3,}", expanded))
        ranked = []
        for entry in self.agents()["items"]:
            target = " ".join(str(entry.get(k, "")) for k in ("name", "capabilities", "evidence_summary", "strengths")).casefold()
            matched = sorted(t for t in terms if t in target)
            if matched:
                ranked.append(dict(entry, relevance_score=len(matched), matched_terms=matched,
                                   selection_method="lexical_overlap_not_scientific_ranking"))
        ranked.sort(key=lambda a: (-a["relevance_score"], a["id"]))
        result = {"id": str(uuid.uuid4()), "question": question, "data_class": data_class, "network_layer": layer,
                  "decision": decision, "reasons": reasons, "ranked_agents": ranked[:12],
                  "steps": ["Уточнить измеримый вопрос и область применимости", "Найти источники и сохранить происхождение",
                            "Проверить аннотации и ограничения", "Выполнить методический контроль",
                            "Сопоставить утверждения с источниками; отдельно запланировать независимую проверку"],
                  "limits": list(LIMITS), "created_at": now()}
        with self.lock:
            self.db.execute("INSERT INTO decisions VALUES(?,?,?)", (result["id"], result["created_at"], canonical(result)))
        return result

    def _save(self, mission):
        mission["updated_at"] = now()
        self.db.execute("INSERT INTO missions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                        (mission["id"], canonical(mission)))

    def _load(self, ident):
        row = self.db.execute("SELECT payload FROM missions WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError("Mission not found")
        return json.loads(row[0])

    def _view(self, mission):
        result = dict(mission)
        result["claims"] = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM claims WHERE mission_id=? ORDER BY rowid", (mission["id"],))]
        result["job_details"] = [self.network.queue.get(j) for j in mission["jobs"]]
        return result

    def missions(self):
        with self.lock:
            return [self._view(json.loads(r[0])) for r in self.db.execute("SELECT payload FROM missions ORDER BY rowid DESC").fetchall()]

    def create_mission(self, body):
        fields(body, {"question", "online", "data_class", "max_sources"})
        question = text(body.get("question"), "question")
        online = boolean(body.get("online", False), "online")
        limit = integer(body.get("max_sources", 5), "max_sources", 1, 8)
        route = self.route({"question": question, "data_class": body.get("data_class", "public")})
        if online and route["decision"] != "allowed":
            raise ValueError("Online transmission is disabled for nonpublic data; use offline mode")
        with self.lock:
            active = sum(json.loads(r[0])["status"] not in TERMINAL for r in self.db.execute("SELECT payload FROM missions"))
            if active >= 20:
                raise ValueError("At most 20 active missions are supported")
            mission = {"id": str(uuid.uuid4()), "question": question, "online": online, "data_class": route["data_class"],
                       "max_sources": limit, "status": "created", "jobs": [], "events": [], "route": route,
                       "created_at": now(), "claims": [], "evidence_items": [], "method_results": [],
                       "discovery_items": [], "limitations": list(LIMITS), "budget": {"jobs": 3, "max_worker_claims": 6,
                         "max_provider_requests": 6, "note": "Сетевые повторы возможны после потери аренды; external платные API не используются."}}
            mission["events"].append({"at": now(), "role": "coordinator", "event": "mission_created"})
            self._save(mission)
        return self.tick({"id": mission["id"]})

    def _payloads(self, mission):
        query = self.network._query({"question": mission["question"], "iteration": 0})
        plugin = select_builtin(mission["question"])
        return {"discovery": {"query": query, "limit": mission["max_sources"], "offline": not mission["online"]},
                "evidence": {"query": query, "limit": mission["max_sources"], "offline": not mission["online"]},
                "simulation": {"plugin_id": plugin, "parameters": {"seed": 42}}}

    def _valid_result(self, job):
        if job["kind"] == "simulation":
            return self.network._valid_result(job)
        if job["kind"] == "discovery":
            if not self.network._valid_result(job):
                return False
            result, payload = job["result"], job["payload"]
            expected = "offline_catalog" if payload["offline"] else "public_metadata"
            return (result["query"] == payload["query"] and result["mode"] == expected
                    and len(result["items"]) <= payload["limit"]
                    and result["requests"] == (0 if payload["offline"] else 2)
                    and all(_safe_url(item.get("url", "")) for item in result["items"]))
        if job["kind"] != "evidence":
            return False
        result = job.get("result")
        expected_mode = "offline_index" if job["payload"]["offline"] else "public_abstract_triage"
        if (not isinstance(result, dict) or result.get("mode") != expected_mode
                or result.get("query") != job["payload"]["query"]
                or not isinstance(result.get("items"), list) or len(result["items"]) > job["payload"]["limit"]
                or not isinstance(result.get("errors"), list) or type(result.get("requests")) is not int
                or result["requests"] != (0 if job["payload"]["offline"] else 1)):
            return False
        seen = set()
        for item in result["items"]:
            if (not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("id", "title", "url"))
                    or item["id"] in seen or type(item.get("abstract_available")) is not bool
                    or not isinstance(item.get("indicators"), list) or not isinstance(item.get("provenance"), dict)
                    or not all(isinstance(item["provenance"].get(k), str) and item["provenance"][k] for k in ("provider", "retrieved_at"))):
                return False
            seen.add(item["id"])
            if item["provenance"]["provider"] != ("bundled_catalog" if job["payload"]["offline"] else "europepmc"):
                return False
            if not _safe_url(item["url"], online=not job["payload"]["offline"]) or not _timestamp_valid(item["provenance"]["retrieved_at"]):
                return False
            allowed_indicators = {"in_vitro", "animal", "human", "computational", "systematic_review", "randomized", "negation_present"}
            if any(not isinstance(x, str) or x not in allowed_indicators for x in item["indicators"]):
                return False
            if item["abstract_available"] and (job["payload"]["offline"] or not isinstance(item.get("abstract_sha256"), str)
                      or not re.fullmatch(r"[0-9a-f]{64}", item["abstract_sha256"])):
                return False
            if not isinstance(item.get("excerpt", ""), str) or len(item.get("excerpt", "").split()) > 12:
                return False
        return True

    def tick(self, body):
        fields(body, {"id"})
        ident = text(body.get("id"), "id", 100)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                mission = self._load(ident)
                if mission["status"] not in TERMINAL:
                    payloads = self._payloads(mission)
                    if not mission["jobs"]:
                        for kind in KINDS:
                            job = self.network.queue.submit(kind, payloads[kind], capabilities=[kind],
                                idempotency_key=f"society:{ident}:{kind}", max_attempts=2)
                            mission["jobs"].append(job["id"])
                        mission["events"].append({"at": now(), "role": "implementer", "event": "bounded_jobs_submitted", "query": payloads["evidence"]["query"]})
                    jobs = [self.network.queue.get(j) for j in mission["jobs"]]
                    if any(j["status"] in {"queued", "running"} for j in jobs):
                        mission["status"] = "running" if any(j["status"] == "running" for j in jobs) else "awaiting_workers"
                    else:
                        issues, valid = [], 0
                        for job in jobs:
                            if job["status"] != "completed" or not self._valid_result(job):
                                issues.append({"job_id": job["id"], "reason": "failed_or_invalid_result_provenance"})
                                continue
                            valid += 1
                            result = job["result"]
                            if result.get("errors"):
                                issues.append({"job_id": job["id"], "provider_errors": result["errors"]})
                            if job["kind"] == "evidence":
                                mission["evidence_items"] = result["items"]
                            elif job["kind"] == "discovery":
                                mission["discovery_items"] = result["items"]
                            else:
                                mission["method_results"] = [dict(result, interpretation="Методический контроль на искусственных данных; не прогноз организма.")]
                        mission["status"] = "completed" if valid == 3 and not issues else "partial" if valid else "failed"
                        mission["issues"] = issues
                        mission["events"].append({"at": now(), "role": "critic", "event": "results_checked", "valid_envelopes": valid,
                                                   "verdict": "workflow_execution_only_not_scientific_validation"})
                        mission["events"].append({"at": now(), "role": "coordinator", "event": "mission_stopped", "status": mission["status"]})
                    self._save(mission)
                self.db.commit()
                return self._view(mission)
            except Exception:
                self.db.rollback()
                raise

    def tick_all(self):
        with self.lock:
            ids = [m["id"] for m in (json.loads(r[0]) for r in self.db.execute("SELECT payload FROM missions")) if m["status"] not in TERMINAL]
        return {"items": [self.tick({"id": ident}) for ident in ids]}

    def cancel(self, body):
        fields(body, {"id"})
        ident = text(body.get("id"), "id", 100)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                mission = self._load(ident)
                if mission["status"] not in TERMINAL:
                    for kind in KINDS:
                        job = self.network.queue.by_idempotency_key(f"society:{ident}:{kind}")
                        if job and job["id"] not in mission["jobs"]:
                            mission["jobs"].append(job["id"])
                    for job_id in mission["jobs"]:
                        if self.network.queue.get(job_id)["status"] in {"queued", "running"}:
                            self.network.queue.cancel(job_id)
                    mission["status"] = "cancelled"
                    mission["events"].append({"at": now(), "role": "coordinator", "event": "mission_cancelled"})
                    self._save(mission)
                self.db.commit()
                return self._view(mission)
            except Exception:
                self.db.rollback()
                raise

    def add_claim(self, body):
        fields(body, {"mission_id", "text", "source_ids", "scope", "assessment", "rationale"})
        ident = text(body.get("mission_id"), "mission_id", 100)
        statement = text(body.get("text"), "text", 4000)
        scope = text(body.get("scope"), "scope", 2000)
        assessment = body.get("assessment", "unreviewed")
        if assessment not in {"unreviewed", "supported_within_scope", "contradicted", "inconclusive"}:
            raise ValueError("Unsupported assessment")
        sources = body.get("source_ids", [])
        if not isinstance(sources, list) or len(sources) > 20 or any(not isinstance(s, str) for s in sources) or len(set(sources)) != len(sources):
            raise ValueError("source_ids must be a unique bounded list")
        rationale = body.get("rationale", "")
        if not isinstance(rationale, str) or len(rationale) > 4000:
            raise ValueError("rationale must contain at most 4000 characters")
        if assessment != "unreviewed" and (not sources or not rationale.strip()):
            raise ValueError("Reviewed claims require source_ids and a rationale")
        with self.lock:
            mission = self._load(ident)
            available = {x["id"] for x in mission["evidence_items"] + mission["discovery_items"]}
            if set(sources) - available:
                raise ValueError("Unknown source IDs for this mission")
            claim = {"id": str(uuid.uuid4()), "mission_id": ident, "text": statement, "scope": scope,
                     "source_ids": sources, "assessment": assessment, "rationale": rationale,
                     "created_at": now(), "assessed_by": "local_user_self_reported",
                     "independently_validated": False}
            self.db.execute("INSERT INTO claims VALUES(?,?,?)", (claim["id"], ident, canonical(claim)))
        return claim

    def status(self):
        agents, missions, snapshots = self.agents()["items"], self.missions(), self.snapshots.items()
        return {"agents": agents, "missions": missions, "roles": ROLES, "snapshots": snapshots,
                "directory_jobs": self.directory_jobs(),
                "counts": {"agents_documented": len(agents), "external_agents_connected": 0, "missions": len(missions),
                           "snapshots": len(snapshots), "workflow_roles": len(ROLES)}, "limits": LIMITS}

    def export(self):
        result = self.status()
        result["exported_at"] = now()
        result["contains"] = "local_research_records_without_auth_or_lease_tokens"
        result["sharing_note"] = "Запросы и утверждения могут содержать введённые пользователем данные; проверьте их перед передачей."
        return result

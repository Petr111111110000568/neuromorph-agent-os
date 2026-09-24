"""Bounded, durable research campaigns; metadata discovery is not validation."""
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from ..store import canonical, now
from .queue import Queue
from .accounts import Accounts
from . import discovery

TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted"}


def text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name}: required nonempty text, maximum {maximum} characters")
    return value.strip()


def integer(value, name, low, high):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{name}: expected integer {low}..{high}")
    return value


def boolean(value, name):
    if not isinstance(value, bool):
        raise ValueError(f"{name}: expected Boolean")
    return value


def fields(body, allowed):
    if not isinstance(body, dict) or set(body) - set(allowed):
        raise ValueError("Unknown fields or non-object request")


class NetworkControl:
    def __init__(self, root, state_dir):
        self.root, self.state_dir = Path(root), Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.queue = Queue(self.state_dir / "network_queue.sqlite3")
        self.accounts = Accounts(self.state_dir / "network_accounts.sqlite3")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.state_dir / "network_control.sqlite3"), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS ingested(job_id TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, timestamp TEXT, kind TEXT, payload TEXT);
        """)

    def close(self):
        with self.lock:
            self.db.close()
        self.queue.close()
        self.accounts.close()

    def _event(self, kind, payload):
        self.db.execute("INSERT INTO events(timestamp,kind,payload) VALUES(?,?,?)", (now(), kind, canonical(payload)))

    def _load(self, ident):
        row = self.db.execute("SELECT payload FROM campaigns WHERE id=?", (ident,)).fetchone()
        if not row:
            raise KeyError("Campaign not found")
        return json.loads(row[0])

    def _save(self, campaign):
        campaign["updated_at"] = now()
        self.db.execute("INSERT INTO campaigns(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (campaign["id"], canonical(campaign)))

    def _resource(self, raw, job_id=None):
        if not isinstance(raw, dict) or not isinstance(raw.get("title"), str):
            return None
        title = raw["title"][:1000]
        url = raw.get("url", "")
        if not isinstance(url, str):
            url = ""
        url = url[:3000]
        # Imported worker metadata is never authoritative access/installation evidence.
        identity = str(raw.get("id") or hashlib.sha256((url + title).encode()).hexdigest())[:200]
        prior = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
        provenance = {"job_id": job_id, "provider": str(prior.get("provider", raw.get("provider", "worker_metadata")))[:80], "imported_at": now()}
        for key in ("doi", "retrieved_at", "source_id", "filename"):
            if isinstance(prior.get(key), str):
                provenance[key] = prior[key][:500]
        resource = {"id": identity, "title": title, "url": url,
          "description": str(raw.get("description", raw.get("summary", "")))[:5000],
          "kind": str(raw.get("kind", "publication"))[:80],
          "capabilities": [x[:100] for x in raw.get("capabilities", [])[:30] if isinstance(x, str)] if isinstance(raw.get("capabilities", []), list) else [],
          "access": "metadata_only", "status": "discovered_unverified",
          "provenance": provenance,
          "limitations": ["Найдены метаданные; содержание и научная применимость не подтверждены.", "Наличие ресурса не означает доступ или установленный адаптер."]}
        self.db.execute("INSERT INTO resources(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (identity, canonical(resource)))
        return identity

    def refresh_discoveries(self):
        jobs = self.queue.jobs()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for job in jobs:
                    if job["kind"] != "discovery" or job["status"] != "completed":
                        continue
                    if self.db.execute("SELECT 1 FROM ingested WHERE job_id=?", (job["id"],)).fetchone():
                        continue
                    result = job.get("result") or {}
                    if isinstance(result, dict) and isinstance(result.get("items"), list):
                        for raw in result["items"][:100]:
                            self._resource(raw, job["id"])
                    self.db.execute("INSERT INTO ingested VALUES(?)", (job["id"],))
                    self._event("discovery.ingested", {"job_id": job["id"]})
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def resources(self, q=""):
        if not isinstance(q, str) or len(q) > 500:
            raise ValueError("Resource query must be text up to 500 characters")
        with self.lock:
            extra = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM resources")]
        items = discovery.catalog(self.root, extra=extra)
        if q.strip():
            needle = q.strip().casefold()
            items = [item for item in items if needle in canonical(item).casefold()]
        return {"items": items}

    def discover(self, body):
        fields(body, {"query", "providers", "limit", "offline"})
        query = text(body.get("query"), "query", 500)
        limit = integer(body.get("limit", 8), "limit", 1, 20)
        offline = boolean(body.get("offline", False), "offline")
        providers = body.get("providers")
        if providers is not None and (not isinstance(providers, list) or not providers or len(providers) > 2 or any(p not in {"europepmc", "crossref"} for p in providers)):
            raise ValueError("providers: europepmc and/or crossref")
        payload = {"query": query, "limit": limit, "offline": offline}
        if providers is not None:
            payload["providers"] = list(dict.fromkeys(providers))
        return self.queue.submit("discovery", payload, capabilities=["discovery"], max_attempts=2)

    def plan(self, body):
        fields(body, {"question"})
        question = text(body.get("question"), "question")
        return discovery.plan(question, self.resources()["items"])

    def campaigns(self):
        with self.lock:
            return {"items": [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM campaigns ORDER BY rowid DESC")]}

    def create_campaign(self, body):
        fields(body, {"question", "max_iterations", "max_jobs", "online", "simulate"})
        question = text(body.get("question"), "question")
        iterations = integer(body.get("max_iterations", 2), "max_iterations", 1, 5)
        jobs = integer(body.get("max_jobs", 6), "max_jobs", 1, 20)
        online = boolean(body.get("online", False), "online")
        simulate = boolean(body.get("simulate", True), "simulate")
        campaign = {"id": str(uuid.uuid4()), "question": question, "status": "running", "iteration": 0,
          "max_iterations": iterations, "max_jobs": jobs, "online": online, "simulate": simulate,
          "jobs": [], "current_jobs": [], "plan": self.plan({"question": question}), "events": [],
          "created_at": now(), "mode": "bounded_rule_based", "resource_ids": [],
          "limitations": ["Кампания автоматизирует поиск метаданных и методические контрольные расчёты.",
            "Расчёт признаков взрослого организма и самостоятельная установка найденного кода не реализованы."]}
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._save(campaign)
                self._event("campaign.created", {"id": campaign["id"]})
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return self.tick({"id": campaign["id"]})

    @staticmethod
    def _query(campaign):
        question = campaign["question"]
        lowered = question.casefold()
        domains = []
        mapping = ((('генет', 'геном', 'genom'), 'genomics computational models'),
                   (('эпиген', 'epigen'), 'epigenetics cell state models'),
                   (('организм', 'признак', 'phenotyp'), 'adult phenotype model validation'),
                   (('квант', 'quantum'), 'quantum simulation classical benchmark'),
                   (('ml', 'обучен', 'нейро'), 'machine learning model validation'))
        for terms, phrase in mapping:
            if any(term in lowered for term in terms):
                domains.append(phrase)
        base = ' '.join(domains) or re.sub(r"\s+", " ", question)[:200]
        suffixes = ('', ' independent validation benchmark', ' systematic review limitations', ' reproducibility uncertainty', ' negative results replication')
        return (base[:350] + suffixes[min(campaign["iteration"], 4)])[:500]

    def _valid_result(self, job):
        result = job.get("result")
        if not isinstance(result, dict):
            return False
        if job["kind"] == "discovery":
            return (isinstance(result.get("query"), str) and isinstance(result.get("items"), list)
                and isinstance(result.get("errors"), list) and type(result.get("requests")) is int
                and 0 <= result["requests"] <= 2 and result.get("mode") in {"offline_catalog", "public_metadata"}
                and all(isinstance(item, dict) and isinstance(item.get("title"), str) and isinstance(item.get("id"), str) for item in result["items"]))
        if job["kind"] == "simulation":
            provenance = result.get("provenance")
            computation = result.get("result")
            parameters = result.get("parameters")
            if (result.get("status") != "completed" or result.get("plugin_id") != job["payload"].get("plugin_id")
                or not isinstance(computation, dict) or not isinstance(provenance, dict) or not isinstance(parameters, dict)):
                return False
            pins = json.loads((self.root / "data/builtin_pins.json").read_text())["files"]
            return (provenance.get("builtin_hashes") == pins
                and provenance.get("input_sha256") == hashlib.sha256(canonical(parameters).encode()).hexdigest()
                and provenance.get("output_sha256") == hashlib.sha256(canonical(computation).encode()).hexdigest()
                and all(parameters.get(key) == value for key, value in job["payload"].get("parameters", {}).items()))
        return False

    def tick(self, body):
        fields(body, {"id"})
        ident = text(body.get("id"), "id", 100)
        self.refresh_discoveries()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                campaign = self._load(ident)
                if campaign["status"] in TERMINAL:
                    self.db.commit()
                    return campaign
                current = [self.queue.get(j) for j in campaign["current_jobs"]]
                if current and any(j["status"] in {"queued", "running"} for j in current):
                    campaign["status"] = "running" if any(j["status"] == "running" for j in current) else "awaiting_workers"
                    self._save(campaign)
                    self.db.commit()
                    return campaign
                if current:
                    notes = []
                    discovered = []
                    usable_discovery = False
                    malformed_result = False
                    for job in current:
                        if job["status"] != "completed":
                            notes.append({"job_id": job["id"], "status": job["status"], "error": job.get("error")})
                            continue
                        result = job.get("result") or {}
                        if not self._valid_result(job):
                            malformed_result = True
                            notes.append({"job_id": job["id"], "error": "invalid_result_envelope_or_provenance"})
                            continue
                        if job["kind"] == "discovery" and isinstance(result, dict):
                            discoveries = result.get("items", [])
                            discovered.extend(str(x["id"]) for x in discoveries if isinstance(x, dict) and "id" in x)
                            usable_discovery = bool(discoveries) or not result.get("errors")
                            if result.get("errors"):
                                notes.append({"job_id": job["id"], "discovery_errors": result["errors"]})
                    campaign["resource_ids"] = sorted(set(campaign["resource_ids"] + discovered))[:500]
                    campaign["events"].append({"at": now(), "iteration": campaign["iteration"], "event": "iteration_reviewed", "new_resource_count": len(discovered), "notes": notes,
                        "interpretation": "Метаданные и методические тесты; биологическая валидация отсутствует."})
                    campaign["iteration"] += 1
                    campaign["current_jobs"] = []
                    campaign["plan"] = discovery.plan(campaign["question"], self.resources()["items"])
                    if malformed_result:
                        campaign["status"] = "failed"
                        campaign["stop_reason"] = "Результат не прошёл проверку структуры или происхождения вычисления."
                    elif any(job["status"] != "completed" for job in current):
                        campaign["status"] = "failed"
                        campaign["stop_reason"] = "Один из обязательных этапов итерации завершился ошибкой или отменой."
                    elif not usable_discovery:
                        campaign["status"] = "failed"
                        campaign["stop_reason"] = "Поиск не дал проверяемого ответа: провайдеры недоступны или задание завершилось ошибкой."
                    elif campaign["iteration"] >= campaign["max_iterations"]:
                        campaign["status"] = "completed"
                        campaign["stop_reason"] = "Завершён заданный объём обзора; научная цель автоматически не считается достигнутой."
                if campaign["status"] not in TERMINAL:
                    remaining = campaign["max_jobs"] - len(campaign["jobs"])
                    needed = 2 if campaign["simulate"] else 1
                    if remaining < needed:
                        campaign["status"] = "budget_exhausted"
                        campaign["stop_reason"] = "Недостаточно бюджета заданий для следующей полной итерации."
                    else:
                        index = campaign["iteration"]
                        payload = {"query": self._query(campaign), "offline": not campaign["online"], "limit": 8}
                        job = self.queue.submit("discovery", payload, capabilities=["discovery"], idempotency_key=f"campaign:{ident}:{index}:discovery", max_attempts=2)
                        new_ids = [job["id"]]
                        if campaign["simulate"]:
                            plugin = discovery.select_builtin(campaign["question"])
                            computation = {"plugin_id": plugin, "parameters": {"seed": 42 + index}}
                            job = self.queue.submit("simulation", computation, capabilities=["simulation"], idempotency_key=f"campaign:{ident}:{index}:simulation", max_attempts=2)
                            new_ids.append(job["id"])
                        campaign["jobs"].extend(new_ids)
                        campaign["current_jobs"] = new_ids
                        campaign["status"] = "awaiting_workers"
                        campaign["events"].append({"at": now(), "event": "iteration_submitted", "iteration": index, "query": payload["query"], "job_ids": new_ids})
                self._save(campaign)
                self._event("campaign.tick", {"id": ident, "status": campaign["status"], "iteration": campaign["iteration"]})
                self.db.commit()
                return campaign
            except Exception:
                self.db.rollback()
                raise

    def cancel_campaign(self, body):
        fields(body, {"id"})
        ident = text(body.get("id"), "id", 100)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                campaign = self._load(ident)
                if campaign["status"] not in TERMINAL:
                    # A crash between commits in queue/control databases may leave a
                    # submitted job absent from the campaign record. Its deterministic
                    # key still makes it discoverable and cancellable after restart.
                    recovered = list(campaign["jobs"])
                    for index in range(campaign["max_iterations"]):
                        for kind in ("discovery", "simulation"):
                            prior = self.queue.by_idempotency_key(f"campaign:{ident}:{index}:{kind}")
                            if prior and prior["id"] not in recovered:
                                recovered.append(prior["id"])
                    campaign["jobs"] = recovered
                    for job_id in campaign["jobs"]:
                        job = self.queue.get(job_id)
                        if job["status"] in {"queued", "running"}:
                            self.queue.cancel(job_id)
                    campaign["status"] = "cancelled"
                    campaign["stop_reason"] = "Остановлено пользователем; поздние результаты арендованных заданий отклоняются."
                    self._save(campaign)
                    self._event("campaign.cancelled", {"id": ident})
                self.db.commit()
                return campaign
            except Exception:
                self.db.rollback()
                raise

    def tick_all(self):
        outcomes = []
        for campaign in self.campaigns()["items"]:
            if campaign["status"] not in TERMINAL:
                try:
                    result = self.tick({"id": campaign["id"]})
                    outcomes.append({"id": result["id"], "status": result["status"]})
                except (ValueError, KeyError) as exc:
                    outcomes.append({"id": campaign["id"], "status": "tick_error", "error": str(exc)})
        self.refresh_discoveries()
        return {"items": outcomes}

    def inspect_local(self, body):
        fields(body, {"path"})
        path = text(body.get("path"), "path", 1000)
        result = discovery.inspect_local(path)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                for resource in result.get("items", [])[:100]:
                    self._resource(resource)
                self._event("local.inspected", {"count": len(result.get("items", []))})
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return result

    def status(self):
        self.refresh_discoveries()
        return {"workers": self.queue.workers(), "jobs": self.queue.jobs(), "campaigns": self.campaigns()["items"],
          "resources_count": len(self.resources()["items"]), "mode": "distributed_trusted_workers",
          "limitations": ["Планирование и уточнение запросов работают по явным правилам, без подключённой LLM.",
            "Удалённые worker входят в общую доверенную область; их результаты требуют независимой проверки.",
            "Метаданные ресурсов не подтверждают доступ к модели, установку адаптера или биологическую применимость."]}

    def export(self):
        snapshot = self.status()
        snapshot.update({"resources": self.resources()["items"], "accounts": self.accounts.list(), "exported_at": now()})
        with self.lock:
            snapshot["events"] = [{"id": row[0], "at": row[1], "kind": row[2], "details": json.loads(row[3])} for row in self.db.execute("SELECT id,timestamp,kind,payload FROM events ORDER BY id")]
        return snapshot

"""Bounded brain-inspired coordination; anatomical names are an analogy.

Roles execute explicit Python rules. They are neither brain areas nor LLMs.
SQLite persists decisions; the existing trusted-worker queue executes models.
"""
import copy
import hashlib
import json
import math
import re
import sqlite3
import threading
import uuid

from .network.control import fields, integer, text
from .store import canonical, now

PLUGINS = ("kan_benchmark", "cortical_sequence", "structural_plasticity")
TERMINAL = {"completed", "partial", "failed", "cancelled"}
MAX_SESSIONS = 200
MAX_ACTIVE = 20
LIMITATIONS = [
    "Названия областей мозга — функциональная инженерная аналогия, а не анатомическая или нейрофизиологическая модель.",
    "Роли выполняют программные правила; языковые модели, сознание и полноценное поведение человеческого мозга не реализованы.",
    "Вопрос служит для локального поиска и планирования; вычисления выполняют заранее определённые синтетические задачи, а не отвечают произвольно на вопрос.",
    "Хеши проверяют согласованность конверта доверенного worker; это не удалённая аттестация и не независимая научная репликация.",
    "Метрики разных задач не усредняются. Клиническая и биологическая валидация отсутствует.",
    "В очередь передаются только параметры синтетических моделей; запрос и локальные источники не отправляются внешним сервисам.",
]
REGIONS = [
    {"id": ident, "name": name, "function": purpose, "implementation": "programmed_rules"}
    for ident, name, purpose in [
        ("thalamus", "Таламус · вход и маршрутизация", "Проверка типов запроса, класса данных и разрешённых моделей"),
        ("association_cortex", "Ассоциативная кора · источники", "Лексическое сопоставление вопроса и локального каталога"),
        ("hippocampus", "Гиппокамп · эпизодическая память", "Извлечение завершённых вычислений и связанных источников"),
        ("prefrontal", "Префронтальная кора · план", "Фиксация раздельных задач, параметров и хешей кода"),
        ("basal_ganglia", "Базальные ганглии · выбор действий", "Бюджет, разрешённые действия, очередь и отмена"),
        ("cortex_kan", "Вычислительный эксперт · KAN", "Сравнение методов аппроксимации на синтетических данных"),
        ("cortex_sequence", "Вычислительный эксперт · последовательности", "Проверка контекстной памяти на символических последовательностях"),
        ("cortex_plasticity", "Вычислительный эксперт · пластичность", "Проверка адаптации и забывания при перестройке связей"),
        ("cerebellum", "Мозжечок · проверка исполнения", "Проверка параметров, чисел и хешей результата"),
        ("anterior_cingulate", "Передняя поясная кора · критик", "Сохранение ошибок, ограничений и несопоставимости задач"),
        ("global_workspace", "Общее рабочее пространство", "Интеграция проверенных ветвей без усреднения разных метрик"),
    ]
]
CONNECTIONS = [{"source": a, "target": b, "type": kind} for a, b, kind in [
    ("thalamus", "association_cortex", "validated_request"),
    ("association_cortex", "hippocampus", "source_context"),
    ("hippocampus", "prefrontal", "episodic_context"),
    ("prefrontal", "basal_ganglia", "bounded_plan"),
    ("basal_ganglia", "cortex_kan", "simulation_job"),
    ("basal_ganglia", "cortex_sequence", "simulation_job"),
    ("basal_ganglia", "cortex_plasticity", "simulation_job"),
    ("cortex_kan", "cerebellum", "run_envelope"),
    ("cortex_sequence", "cerebellum", "run_envelope"),
    ("cortex_plasticity", "cerebellum", "run_envelope"),
    ("cerebellum", "anterior_cingulate", "checked_branches"),
    ("anterior_cingulate", "global_workspace", "qualified_results"),
    ("global_workspace", "hippocampus", "completed_episode"),
]]
EXPERTS = dict(zip(PLUGINS, ("cortex_kan", "cortex_sequence", "cortex_plasticity")))
SCOPES = {
    "kan_benchmark": "Синтетическая аппроксимация; ошибки сравнимы только между контрольными методами этой задачи.",
    "cortical_sequence": "Символическая последовательность; предсказание контекста не является моделированием всей коры.",
    "structural_plasticity": "Смена синтетической зависимости; адаптация и забывание при фиксированном числе узлов.",
}


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


class Brain:
    def __init__(self, service):
        self.service, self.root = service, service.root
        self.queue = service.network.queue
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(service.store.path.parent / "brain.sqlite3"),
                                  check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS brain_sessions(id TEXT PRIMARY KEY,payload TEXT NOT NULL)")

    def close(self):
        with self.lock:
            self.db.close()

    def _save(self, session):
        session["updated_at"] = now()
        self.db.execute("INSERT INTO brain_sessions VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                        (session["id"], canonical(session)))

    def _load(self, ident):
        row = self.db.execute("SELECT payload FROM brain_sessions WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError("Brain session not found")
        return json.loads(row[0])

    def _event(self, session, region, kind, inputs, outputs, provenance=None):
        sequence = len(session["events"]) + 1
        event = {"id": f"{session['id']}:{sequence}", "sequence": sequence, "at": now(),
                 "region": region, "type": kind, "input": inputs, "output": outputs,
                 "provenance": {"implementation": "programmed_rules", **(provenance or {})}}
        event["provenance"]["message_sha256"] = digest({"input": inputs, "output": outputs})
        session["events"].append(event)

    @staticmethod
    def _summary(session):
        return {key: session[key] for key in ("id", "question", "seed", "data_class", "plugins", "status", "created_at", "updated_at")}

    def _view(self, session):
        result = copy.deepcopy(session)
        result["job_details"] = [self.queue.get(ident) for ident in session["jobs"].values()]
        return result

    def get(self, ident):
        ident = text(ident, "id", 100)
        with self.lock:
            return self._view(self._load(ident))

    def sessions(self):
        with self.lock:
            rows = self.db.execute("SELECT payload FROM brain_sessions ORDER BY rowid DESC").fetchall()
            return {"items": [self._summary(json.loads(row[0])) for row in rows]}

    def _sources(self, question, plugins):
        query = question.casefold() + " " + " ".join(plugins)
        terms = set(re.findall(r"[\w-]{3,}", query))
        for needles, expansion in ((["кан", "колмогор", "арнольд", "kan"], "kolmogorov arnold kan"),
                                   (["кортик", "cortical"], "cortical sequence dendrite htm"),
                                   (["морфо", "plasticity"], "morphogenesis plasticity neural")):
            if any(term in query for term in needles):
                terms.update(expansion.split())
        ranked = []
        for raw in self.service.sources()["items"]:
            haystack = canonical(raw).casefold()
            matched = sorted(term for term in terms if term in haystack)
            if matched:
                ranked.append({"id": str(raw["id"]), "title": str(raw.get("title", ""))[:500],
                    "url": str(raw.get("url", ""))[:3000], "matched_terms": matched[:12],
                    "selection_reason": "local_lexical_overlap_not_evidence_validation",
                    "score": len(matched), "metadata_sha256": digest(raw)})
        ranked.sort(key=lambda source: (-source["score"], source["id"]))
        return ranked[:12]

    def _memory(self, session):
        episodes = []
        for row in self.db.execute("SELECT payload FROM brain_sessions ORDER BY rowid DESC"):
            prior = json.loads(row[0])
            if prior["status"] != "completed" or (session["data_class"] == "public" and prior["data_class"] != "public"):
                continue
            for branch in prior.get("branches", []):
                if branch["plugin_id"] in session["plugins"] and branch["status"] == "accepted":
                    episodes.append({"id": prior["id"], "kind": "brain_episode", "plugin_id": branch["plugin_id"],
                        "summary": branch["result"].get("summary", "")[:1500],
                        "source_ids": [source["id"] for source in prior.get("sources", [])],
                        "provenance": branch["provenance"], "independent_replication": False})
            if len(episodes) >= 6:
                break
        # Standalone synthetic runs do not include the free-text question.
        for run in self.service.store.list("run"):
            if len(episodes) >= 6:
                break
            if run.get("status") == "completed" and run.get("plugin_id") in session["plugins"]:
                computation, provenance = run.get("result"), run.get("provenance", {})
                if isinstance(computation, dict) and provenance.get("output_sha256") == digest(computation):
                    episodes.append({"id": run["id"], "kind": "local_run", "plugin_id": run["plugin_id"],
                        "summary": str(computation.get("summary", ""))[:1500], "source_ids": [],
                        "provenance": provenance, "independent_replication": False})
        return episodes[:6]

    def start(self, body):
        fields(body, {"question", "plugins", "seed", "data_class"})
        question = text(body.get("question"), "question", 2000)
        seed = integer(body.get("seed", 42), "seed", 0, 2147483647)
        selected = body.get("plugins", list(PLUGINS))
        if (not isinstance(selected, list) or not 1 <= len(selected) <= 3
                or any(not isinstance(p, str) or p not in PLUGINS for p in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError("plugins must contain 1–3 unique approved model IDs")
        data_class = body.get("data_class", "public")
        if not isinstance(data_class, str) or data_class not in {"public", "internal"}:
            raise ValueError("data_class must be public or internal")
        pins = self.service._pins()
        specs = {spec["id"]: spec for spec in self.service.plugins()["items"]}
        if any(plugin not in specs for plugin in selected):
            raise ValueError("Requested model is not installed in this release")
        plan = []
        for plugin in selected:
            properties = specs[plugin].get("parameters", {}).get("properties", {})
            if "seed" not in properties:
                raise ValueError("Approved model must support an explicit seed")
            parameters = {key: copy.deepcopy(value["default"]) for key, value in properties.items() if "default" in value}
            parameters["seed"] = seed
            from .service import validate_parameters
            validate_parameters(parameters, specs[plugin]["parameters"])
            plan.append({"plugin_id": plugin, "parameters": parameters, "scope": SCOPES[plugin],
                         "limitations": list(specs[plugin].get("limitations", [])), "region": EXPERTS[plugin]})
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                rows = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM brain_sessions")]
                if len(rows) >= MAX_SESSIONS or sum(s["status"] not in TERMINAL for s in rows) >= MAX_ACTIVE:
                    raise ValueError("Brain session storage or active-session limit reached")
                session = {"id": "brain_" + uuid.uuid4().hex, "question": question, "seed": seed,
                    "data_class": data_class, "plugins": list(selected), "status": "created", "created_at": now(),
                    "mode": "rule_based_brain_analogy", "pins": pins, "plan": plan, "jobs": {},
                    "branches": [], "issues": [], "events": [], "limitations": list(LIMITATIONS),
                    "budget": {"jobs": len(selected), "max_worker_claims": 2 * len(selected),
                        "external_requests": 0, "automatic_replanning": False,
                        "note": "Очередь допускает повтор после потери аренды; каждый plugin ограничен собственным runtime."},
                    "workspace": {"summary": "Ожидается выполнение выбранных синтетических задач.", "branches": [],
                        "aggregation": "separate_experiments_no_cross_task_average", "scientific_conclusion": "not_established"}}
                self._event(session, "thalamus", "validated_request", {"question": question, "data_class": data_class},
                            {"plugins": selected, "seed": seed, "network_policy": "local_metadata_only"})
                session["sources"] = self._sources(question, selected)
                self._event(session, "association_cortex", "source_context", {"question": question},
                            {"sources": session["sources"], "external_requests": 0})
                session["memory"] = self._memory(session)
                self._event(session, "hippocampus", "episodic_context", {"plugins": selected},
                            {"episodes": session["memory"], "retrieval": "same_plugin_local_history_not_independent_evidence"})
                self._event(session, "prefrontal", "bounded_plan", {"source_ids": [s["id"] for s in session["sources"]],
                            "memory_ids": [m["id"] for m in session["memory"]]}, {"plan": plan, "budget": session["budget"]},
                            {"builtin_hashes": pins})
                self._save(session)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return self.tick({"id": session["id"]})

    def _verification(self, job, plan, pins):
        run = job.get("result")
        checks = {"queue_completed": job["status"] == "completed", "run_completed": False,
                  "plugin_matches": False, "parameters_match": False, "input_hash_matches": False,
                  "output_hash_matches": False, "planned_pins_match": False, "result_structure": False}
        if not isinstance(run, dict):
            return checks
        parameters, computation, provenance = run.get("parameters"), run.get("result"), run.get("provenance")
        checks.update(run_completed=run.get("status") == "completed", plugin_matches=run.get("plugin_id") == plan["plugin_id"],
                      parameters_match=parameters == plan["parameters"])
        if isinstance(provenance, dict):
            checks["planned_pins_match"] = provenance.get("builtin_hashes") == pins
            try:
                checks["input_hash_matches"] = provenance.get("input_sha256") == digest(parameters)
                checks["output_hash_matches"] = provenance.get("output_sha256") == digest(computation)
            except (TypeError, ValueError):
                pass
        if isinstance(computation, dict):
            metrics = computation.get("metrics")
            checks["result_structure"] = (isinstance(computation.get("summary"), str)
                and isinstance(computation.get("model"), dict) and isinstance(computation.get("validation"), dict)
                and isinstance(metrics, list) and 0 < len(metrics) <= 100
                and all(isinstance(m, dict) and isinstance(m.get("label"), str)
                    and finite_number(m.get("value")) for m in metrics))
        return checks

    @staticmethod
    def _critique(branch):
        """Describe within-task differences without cross-task score mixing."""
        result, plugin = branch["result"], branch["plugin_id"]
        if not result:
            return {"plugin_id": plugin, "assessment": "result_not_accepted", "comparisons": []}
        comparisons = []
        if plugin == "structural_plasticity":
            rows = result.get("paired_comparisons", [])
            for row in rows[:1000] if isinstance(rows, list) else []:
                if isinstance(row, dict) and finite_number(row.get("guided_minus_fixed_new_mse")):
                    comparisons.append({"seed": row.get("seed"), "comparison": "guided_minus_fixed_new_mse",
                                        "difference": row["guided_minus_fixed_new_mse"], "lower_is_better": True})
        else:
            rows = result.get("table", [])
            if isinstance(rows, list):
                name_key, model, baseline, metric = (("model", "KAN", "MLP", "test_mse") if plugin == "kan_benchmark"
                    else ("condition", "context_cells", "markov_2", "ambiguous_accuracy"))
                by_seed = {}
                for row in rows[:1000]:
                    if (isinstance(row, dict) and type(row.get("seed")) is int and finite_number(row.get(metric))
                            and isinstance(row.get(name_key), str) and row[name_key] in {model, baseline}):
                        by_seed.setdefault(row["seed"], {})[row.get(name_key)] = row[metric]
                for seed, values in sorted(by_seed.items()):
                    if model in values and baseline in values:
                        difference = values[model] - values[baseline]
                        if finite_number(difference):
                            comparisons.append({"seed": seed, "comparison": f"{model}_minus_{baseline}_{metric}",
                                                "difference": difference, "lower_is_better": metric == "test_mse"})
        better = sum((c["difference"] < 0 if c["lower_is_better"] else c["difference"] > 0) for c in comparisons)
        worse = sum((c["difference"] > 0 if c["lower_is_better"] else c["difference"] < 0) for c in comparisons)
        return {"plugin_id": plugin, "assessment": "descriptive_within_task_only", "comparisons": comparisons,
                "seeds_better_than_named_control": better, "seeds_worse_than_named_control": worse,
                "seeds_tied": len(comparisons) - better - worse, "direction_disagrees_between_seeds": bool(better and worse),
                "note": "Все сравнения сохранены; знак не является проверкой значимости или универсальным превосходством."}

    def _integrate(self, session, jobs):
        known = {branch["plugin_id"] for branch in session["branches"]}
        plans = {entry["plugin_id"]: entry for entry in session["plan"]}
        for plugin in session["plugins"]:
            job = jobs[plugin]
            if plugin in known or job["status"] in {"queued", "running"}:
                continue
            checks = self._verification(job, plans[plugin], session["pins"])
            valid = all(checks.values())
            run = job.get("result") if isinstance(job.get("result"), dict) else {}
            branch = {"plugin_id": plugin, "job_id": job["id"], "status": "accepted" if valid else "rejected",
                "worker_status": job["status"], "scope": plans[plugin]["scope"],
                "result": copy.deepcopy(run.get("result")) if valid else None,
                "provenance": copy.deepcopy(run.get("provenance", {})), "verification": {
                    "status": "envelope_consistent_trusted_worker_only" if valid else "failed_or_inconsistent",
                    "checks": checks, "scientific_validation": False, "worker_attestation": False},
                "limitations": plans[plugin]["limitations"]}
            session["branches"].append(branch)
            self._event(session, EXPERTS[plugin], "run_envelope", {"job_id": job["id"], "parameters": plans[plugin]["parameters"]},
                        {"worker_status": job["status"], "run_id": run.get("id"), "result_received": bool(run)},
                        {"worker_id": job.get("worker_id"), "attempts": job.get("attempts")})
            self._event(session, "cerebellum", "checked_branch", {"job_id": job["id"]},
                        {"plugin_id": plugin, "verification": branch["verification"]})
            if not valid:
                session["issues"].append({"plugin_id": plugin, "job_id": job["id"],
                    "reason": "failed_or_inconsistent_run_envelope", "failed_checks": [key for key, passed in checks.items() if not passed],
                    "worker_error": job.get("error")})
        if any(job["status"] in {"queued", "running"} for job in jobs.values()):
            return
        accepted = [branch for branch in session["branches"] if branch["status"] == "accepted"]
        session["status"] = "completed" if len(accepted) == len(session["plugins"]) else "partial" if accepted else "failed"
        session["critique"] = [self._critique(branch) for branch in session["branches"]]
        self._event(session, "anterior_cingulate", "qualified_results", {"branch_count": len(session["branches"])},
                    {"accepted_envelopes": len(accepted), "issues": session["issues"],
                     "within_task_comparisons": session["critique"],
                     "cross_task_metrics_comparable": False, "negative_results_policy": "retained_without_selection",
                     "scientific_conclusion": "not_established", "limitations": session["limitations"]})
        session["workspace"] = {"summary": f"Проверена согласованность {len(accepted)} из {len(session['plugins'])} вычислительных конвертов. Результаты представлены раздельно.",
            "branches": [{"plugin_id": branch["plugin_id"], "job_id": branch["job_id"], "status": branch["status"],
                "summary": branch["result"]["summary"] if branch["result"] else "Результат не принят проверкой исполнения.",
                "metrics": branch["result"]["metrics"] if branch["result"] else [],
                "scope": branch["scope"], "limitations": branch["limitations"]} for branch in session["branches"]],
            "aggregation": "separate_experiments_no_cross_task_average", "scientific_conclusion": "not_established"}
        self._event(session, "global_workspace", "integrated_episode", {"branch_job_ids": list(session["jobs"].values())},
                    session["workspace"])
        session["completed_at"] = now()

    def tick(self, body):
        fields(body, {"id"})
        ident = text(body.get("id"), "id", 100)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                session = self._load(ident)
                if session["status"] == "cancelling":
                    self._cancel_pending(session)
                    self._save(session)
                elif session["status"] not in TERMINAL:
                    # Queue commits are separate. Keys recover a submission even
                    # when this transaction rolls back after queue.submit().
                    submitted = False
                    for entry in session["plan"]:
                        plugin = entry["plugin_id"]
                        if plugin not in session["jobs"]:
                            job = self.queue.submit("simulation", {"plugin_id": plugin, "parameters": entry["parameters"]},
                                capabilities=["simulation"], idempotency_key=f"brain:{ident}:{plugin}", max_attempts=2)
                            session["jobs"][plugin] = job["id"]
                            submitted = True
                    if submitted:
                        self._event(session, "basal_ganglia", "actions_gated", {"plan_plugins": session["plugins"]},
                                    {"jobs": session["jobs"], "budget": session["budget"], "unapproved_actions": 0})
                    jobs = {plugin: self.queue.get(job_id) for plugin, job_id in session["jobs"].items()}
                    session["status"] = "running" if any(job["status"] == "running" for job in jobs.values()) else "awaiting_workers"
                    self._integrate(session, jobs)
                    self._save(session)
                self.db.commit()
                return self._view(session)
            except Exception:
                self.db.rollback()
                raise

    def tick_all(self):
        with self.lock:
            ids = [session["id"] for session in (json.loads(row[0]) for row in self.db.execute("SELECT payload FROM brain_sessions"))
                   if session["status"] not in TERMINAL]
        return {"items": [self.tick({"id": ident}) for ident in ids]}

    def cancel(self, body):
        fields(body, {"id"})
        ident = text(body.get("id"), "id", 100)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                session = self._load(ident)
                if session["status"] not in TERMINAL and session["status"] != "cancelling":
                    # Commit intent before touching the separately committed
                    # queue, so a crash cannot resume remaining computations.
                    session["status"] = "cancelling"
                    self._event(session, "basal_ganglia", "cancellation_requested", {"session_id": ident},
                                {"policy": "durable_intent_before_queue_mutations"})
                    self._save(session)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return self.tick({"id": ident})

    def _cancel_pending(self, session):
        for plugin in session["plugins"]:
            job = self.queue.by_idempotency_key(f"brain:{session['id']}:{plugin}")
            if job:
                session["jobs"][plugin] = job["id"]
                self.queue.cancel(job["id"])
        session["status"] = "cancelled"
        self._event(session, "basal_ganglia", "actions_cancelled", {"session_id": session["id"]},
                    {"jobs": session["jobs"], "note": "Аренда отозвана; уже начатый процесс worker может завершиться, но результат отменённой задачи не принимается."})

    def status(self):
        sessions = self.sessions()["items"]
        return {"mode": "rule_based_brain_analogy", "regions": copy.deepcopy(REGIONS), "connections": copy.deepcopy(CONNECTIONS),
            "sessions": sessions, "counts": {"sessions": len(sessions), "active": sum(s["status"] not in TERMINAL for s in sessions),
                "regions": len(REGIONS), "llm_agents_configured": 0}, "limitations": list(LIMITATIONS)}

    def export(self):
        with self.lock:
            result = self.status()
            result["sessions"] = [self._view(json.loads(row[0])) for row in self.db.execute("SELECT payload FROM brain_sessions ORDER BY rowid DESC")]
            result.update(exported_at=now(), contains="local_questions_sources_and_computations_without_worker_tokens",
                          sharing_note="Экспорт содержит локальные вопросы, включая internal; проверьте содержание перед передачей.")
            return result

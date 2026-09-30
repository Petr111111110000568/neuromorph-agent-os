"""Finite M02 project control, using the existing Queue and its lease fencing.

This is a synthetic dispatcher experiment, not an agent/model executor. Control
and Queue commits are separate: immutable intents recover their boundary. Only
the control transaction can promote an artifact; delivery never changes it.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading

from .network.queue import Queue
from . import m02_cpu


SCHEMA = "neuromorph.m02.control.v1"
HANDLER = "m02.fixture.v1"
MAX_PROJECTS = 2
MAX_TASKS = 8
MAX_REVISIONS = 2
MAX_RESERVED_ATTEMPTS = 8
ATTEMPTS_PER_HANDOFF = 2
POLICY = {"max_reserved_attempts": 8, "max_attempts_per_handoff": 2,
          "max_paid_calls": 0, "max_model_calls": 0}
FIXTURES = {
    "evidence_alpha": {
        "title": "Synthetic evidence alpha",
        "finding": "An immutable input and current revision are required for promotion.",
        "source": "fixture:evidence_alpha",
        "limitations": ["Fixed engineering fixture; not scientific evidence."],
    },
    "evidence_beta": {
        "title": "Synthetic evidence beta",
        "finding": "A completed queue job still requires independent review.",
        "source": "fixture:evidence_beta",
        "limitations": ["Fixed engineering fixture; not scientific evidence."],
    },
    "repair_alpha": {
        "title": "Synthetic bounded revision",
        "finding": "Rework creates a new handoff while retaining the previous result.",
        "source": "fixture:repair_alpha",
        "limitations": ["Fixed engineering fixture; not scientific evidence."],
    },
}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_PROJECT_FIELDS = {"project_id", "mission", "allowed_artifacts", "closure_criteria",
                   "resource_policy"}
_TASK_FIELDS = {"task_id", "project_id", "goal", "dependencies", "revision",
                "handoff_id", "base_commit", "handler", "fixture_id", "limitations"}
_IDENTITY_FIELDS = {"project_id", "task_id", "handoff_id", "revision",
                    "project_revision", "input_sha256"}


def _canonical(value):
    # Accepted contracts contain only independently validated shallow values.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _closed(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} has an unsupported schema")


def _text(value, name, limit=2048, *, empty=False):
    if (not isinstance(value, str) or (not empty and not value.strip())
            or len(value) > limit or "\x00" in value):
        raise ValueError(f"invalid {name}")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"invalid {name}") from exc
    return value


def _identity(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _texts(value, name, maximum=8):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"invalid {name}")
    result = [_text(item, name, 1024) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"duplicate {name}")
    return result


def _project_spec(spec):
    _closed(spec, _PROJECT_FIELDS, "project")
    _identity(spec["project_id"], "project_id")
    _text(spec["mission"], "mission")
    if "synthetic-evidence" not in _texts(spec["allowed_artifacts"], "allowed_artifacts"):
        raise ValueError("project must explicitly allow the synthetic-evidence artifact class")
    if not _texts(spec["closure_criteria"], "closure_criteria"):
        raise ValueError("closure_criteria must not be empty")
    _closed(spec["resource_policy"], set(POLICY), "resource_policy")
    if any(type(spec["resource_policy"][key]) is not int or
           spec["resource_policy"][key] != value for key, value in POLICY.items()):
        raise ValueError("M02 policy is fixed and shared across projects")
    encoded = _canonical(spec)
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise ValueError("project contract exceeds 16 KiB")
    return json.loads(encoded)


def _task_spec(spec, *, initial=False):
    cpu = isinstance(spec, dict) and spec.get("handler") == m02_cpu.HANDLER
    _closed(spec, (_TASK_FIELDS - {"fixture_id"}) | {"parameters"} if cpu else _TASK_FIELDS, "task")
    for name in ("task_id", "project_id", "handoff_id"):
        _identity(spec[name], name)
    _text(spec["goal"], "goal")
    dependencies = _texts(spec["dependencies"], "dependencies", MAX_TASKS - 1)
    for dep in dependencies:
        _identity(dep, "dependency")
    if spec["task_id"] in dependencies:
        raise ValueError("a task cannot depend on itself")
    if (type(spec["revision"]) is not int or
            not 1 <= spec["revision"] <= MAX_REVISIONS or
            (initial and spec["revision"] != 1)):
        raise ValueError("invalid task revision")
    if not isinstance(spec["base_commit"], str) or not _COMMIT.fullmatch(spec["base_commit"]):
        raise ValueError("base_commit must be a full lowercase commit SHA")
    if cpu:
        m02_cpu.parameters(spec["parameters"])
        if spec["base_commit"] != m02_cpu.ACCEPTED_BASE_COMMIT:
            raise ValueError("CPU base commit is not accepted")
    elif (spec["handler"] != HANDLER or not isinstance(spec["fixture_id"], str)
            or spec["fixture_id"] not in FIXTURES):
        raise ValueError("unsupported M02 handler")
    _texts(spec["limitations"], "limitations")
    encoded = _canonical(spec)
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise ValueError("task contract exceeds 16 KiB")
    return json.loads(encoded)


def _contract(project, task):
    return {"project": project, "task": task}


def _payload(project, task, input_sha256, parent_handoff=None):
    if task["handler"] == m02_cpu.HANDLER:
        payload = m02_cpu.make_input(task, parent_handoff)
        if payload["input_sha256"] != input_sha256:
            raise ValueError("CPU immutable input hash mismatch")
        return payload
    return {
        "schema": "neuromorph.m02.fixture-job.v1", "handler": HANDLER,
        "fixture_id": task["fixture_id"],
        "identity": {"project_id": task["project_id"], "task_id": task["task_id"],
                     "handoff_id": task["handoff_id"], "revision": task["revision"],
                     "project_revision": 1, "input_sha256": input_sha256},
        "contract": _contract(project, task),
    }


def fixture_result(payload):
    """Validate a closed fixture envelope and return its one fixed JSON result."""
    _closed(payload, {"schema", "handler", "fixture_id", "identity", "contract"}, "payload")
    if payload["schema"] != "neuromorph.m02.fixture-job.v1" or payload["handler"] != HANDLER:
        raise ValueError("unsupported fixture payload")
    _closed(payload["identity"], _IDENTITY_FIELDS, "identity")
    _closed(payload["contract"], {"project", "task"}, "contract")
    project = _project_spec(payload["contract"]["project"])
    task = _task_spec(payload["contract"]["task"])
    digest = _sha(_contract(project, task))
    if (task["project_id"] != project["project_id"] or
            _canonical(payload) != _canonical(_payload(project, task, digest))):
        raise ValueError("fixture identity does not match its immutable contract")
    # JSON roundtrip prevents callers mutating the process-wide fixed fixture.
    return json.loads(_canonical({
        "schema": "neuromorph.m02.fixture-result.v1", **payload["identity"],
        "fixture_id": task["fixture_id"], "artifact": FIXTURES[task["fixture_id"]],
        "model_calls": 0,
    }))


def run_fixture_once(queue, *, worker_id="m02-fixture", failpoint=None):
    """At most one real Queue claim/finish, without tools, models or network.

    Use a dedicated Queue: these envelopes intentionally are not payloads of the
    production network.worker. Its leases and retries remain Queue's concern.
    A failpoint is a trusted test callback, never read from project data.
    """
    queue.register_worker(worker_id, ["evidence"])
    job = queue.claim(worker_id, lease_seconds=60)
    if job is None:
        return {"status": "waiting", "reason": "no_fixture_job", "model_calls": 0}
    try:
        if job["kind"] != "evidence" or job["max_attempts"] != ATTEMPTS_PER_HANDOFF:
            raise ValueError("unsupported fixture job")
        result = fixture_result(job["payload"])
    except (ValueError, TypeError, KeyError):
        completed = queue.finish(job["id"], worker_id, job["lease_token"],
                                 error="M02 fixture contract rejected")
    else:
        completed = queue.finish(job["id"], worker_id, job["lease_token"], result=result)
    if failpoint is not None:
        failpoint("after_queue_finish")
    return {"status": completed["status"], "queue_job_id": completed["id"],
            "attempts": completed["attempts"], "model_calls": 0}


class ProjectDispatcher:
    """Two-project control experiment; callers own the separate Queue lifetime."""

    def __init__(self, control_path, queue: Queue, *, failpoint=None, cpu_root=None):
        if str(control_path) != ":memory:":
            Path(control_path).parent.mkdir(parents=True, exist_ok=True)
        self.queue = queue
        self.cpu_root = Path(cpu_root).resolve() if cpu_root is not None else None
        self.failpoint = failpoint
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(control_path), timeout=30, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS m02_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS m02_projects (
                project_id TEXT PRIMARY KEY, spec TEXT NOT NULL, spec_sha256 TEXT NOT NULL,
                revision INTEGER NOT NULL, channel_available INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS m02_tasks (
                task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, spec TEXT NOT NULL,
                revision INTEGER NOT NULL, handoff_id TEXT NOT NULL,
                input_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                queue_job_id TEXT, current_result_sha256 TEXT, parent_handoff TEXT,
                checkpoint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS m02_handoffs (
                handoff_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, project_id TEXT NOT NULL,
                revision INTEGER NOT NULL, project_revision INTEGER NOT NULL,
                input_sha256 TEXT NOT NULL, contract TEXT NOT NULL,
                UNIQUE(task_id, revision));
            CREATE TABLE IF NOT EXISTS m02_intents (
                handoff_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, project_id TEXT NOT NULL,
                revision INTEGER NOT NULL, project_revision INTEGER NOT NULL,
                input_sha256 TEXT NOT NULL, queue_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, queue_job_id TEXT, status TEXT NOT NULL,
                result_sha256 TEXT, reserved_attempts INTEGER NOT NULL CHECK(reserved_attempts=2));
            CREATE TABLE IF NOT EXISTS m02_artifacts (
                sha256 TEXT PRIMARY KEY, content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS m02_cpu_results (
                queue_job_id TEXT PRIMARY KEY, handoff_id TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL, sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS m02_outbox (
                event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, handoff_id TEXT NOT NULL UNIQUE,
                revision INTEGER NOT NULL, result_sha256 TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS m02_deliveries (
                event_id TEXT PRIMARY KEY, result_sha256 TEXT NOT NULL, content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS m02_reviews (
                handoff_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                result_sha256 TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS m02_cancellations (
                handoff_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, reason TEXT NOT NULL,
                status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS m02_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, task_id TEXT NOT NULL, project_id TEXT NOT NULL,
                handoff_id TEXT NOT NULL, revision INTEGER NOT NULL, details TEXT NOT NULL);
        """)
        with self._transaction():
            self._db.execute("INSERT OR IGNORE INTO m02_meta VALUES ('schema', ?)", (SCHEMA,))
            if self._meta("schema") != SCHEMA:
                raise ValueError("unsupported M02 state schema")
            for key in ("duplicate_suppression", "stale_rejections", "promotions"):
                self._db.execute("INSERT OR IGNORE INTO m02_meta VALUES (?, '0')", (key,))
            self._db.execute("INSERT OR IGNORE INTO m02_meta VALUES ('last_project', '')")

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def _hit(self, stage):
        if self.failpoint is not None:
            self.failpoint(stage)

    def _meta(self, key):
        return self._db.execute("SELECT value FROM m02_meta WHERE key=?", (key,)).fetchone()[0]

    def _count(self, key):
        self._db.execute("UPDATE m02_meta SET value=CAST(value AS INTEGER)+1 WHERE key=?", (key,))

    def _event(self, kind, task, details):
        """Within the caller's transaction; duplicates/waiting never grow history."""
        values = [kind, task["task_id"], task["project_id"], task["handoff_id"], task["revision"], details]
        event_id = "m02-event:" + _sha(values)
        if self._db.execute("SELECT 1 FROM m02_events WHERE event_id=?", (event_id,)).fetchone():
            return
        if self._db.execute("SELECT COUNT(*) FROM m02_events").fetchone()[0] >= 256:
            raise ValueError("M02 event limit reached; history is not silently discarded")
        self._db.execute("""INSERT INTO m02_events
            (event_id,kind,task_id,project_id,handoff_id,revision,details) VALUES (?,?,?,?,?,?,?)""",
            (event_id, *values[:-1], _canonical(details)))

    def _project(self, project_id):
        row = self._db.execute("SELECT * FROM m02_projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return row

    def _task(self, task_id):
        row = self._db.execute("SELECT * FROM m02_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row

    @staticmethod
    def _public_task(row):
        return {**json.loads(row["spec"]), **{key: row[key] for key in (
            "status", "input_sha256", "queue_job_id", "current_result_sha256",
            "parent_handoff", "checkpoint")}}

    def _reserved(self):
        return self._db.execute("SELECT COALESCE(SUM(reserved_attempts),0) FROM m02_intents").fetchone()[0]

    def create_project(self, spec):
        spec = _project_spec(spec)
        encoded = _canonical(spec)
        with self._transaction():
            old = self._db.execute("SELECT * FROM m02_projects WHERE project_id=?",
                                   (spec["project_id"],)).fetchone()
            if old is not None:
                if old["spec"] != encoded:
                    raise ValueError("project identity is already bound to a different contract")
                self._count("duplicate_suppression")
            else:
                if self._db.execute("SELECT COUNT(*) FROM m02_projects").fetchone()[0] >= MAX_PROJECTS:
                    raise ValueError("M02 project limit reached")
                self._db.execute("INSERT INTO m02_projects VALUES (?,?,?,1,1)",
                                 (spec["project_id"], encoded, _sha(spec)))
            row = self._project(spec["project_id"])
            return {**spec, "revision": row["revision"], "channel_available": bool(row["channel_available"])}

    def _insert_handoff(self, project, spec, parent_handoff=None):
        contract = _contract(json.loads(project["spec"]), spec)
        digest = (m02_cpu.make_input(spec, parent_handoff)["input_sha256"]
                  if spec["handler"] == m02_cpu.HANDLER else _sha(contract))
        if self._db.execute("SELECT 1 FROM m02_handoffs WHERE handoff_id=?",
                            (spec["handoff_id"],)).fetchone():
            raise ValueError("handoff identity is permanently bound")
        self._db.execute("INSERT INTO m02_handoffs VALUES (?,?,?,?,?,?,?)", (
            spec["handoff_id"], spec["task_id"], spec["project_id"], spec["revision"],
            project["revision"], digest, _canonical(contract)))
        return digest

    def create_task(self, spec):
        spec = _task_spec(spec, initial=True)
        if spec["handler"] == m02_cpu.HANDLER:
            self._cpu_pins()
        encoded = _canonical(spec)
        with self._transaction():
            old = self._db.execute("SELECT * FROM m02_tasks WHERE task_id=?", (spec["task_id"],)).fetchone()
            if old is not None:
                # Compare with original immutable R1, even if the current task has advanced.
                original = self._db.execute("SELECT contract FROM m02_handoffs WHERE task_id=? AND revision=1",
                                            (spec["task_id"],)).fetchone()
                if original is None or _canonical(json.loads(original[0])["task"]) != encoded:
                    raise ValueError("task identity is already bound to a different contract")
                self._count("duplicate_suppression")
                return self._public_task(old)
            project = self._project(spec["project_id"])
            if self._db.execute("SELECT COUNT(*) FROM m02_tasks").fetchone()[0] >= MAX_TASKS:
                raise ValueError("M02 task limit reached")
            # Existing dependencies make creation order a bounded DAG, not an arbitrary graph.
            for dep in spec["dependencies"]:
                self._task(dep)
            digest = self._insert_handoff(project, spec)
            self._db.execute("""INSERT INTO m02_tasks VALUES
                (?,?,?,?,?,?, 'pending', NULL,NULL,NULL, 'created')""", (
                    spec["task_id"], spec["project_id"], encoded, spec["revision"],
                    spec["handoff_id"], digest))
            return self._public_task(self._task(spec["task_id"]))

    def set_channel_available(self, project_id, available):
        """Operator/test scheduling input, never taken from a worker result."""
        if type(available) is not bool:
            raise ValueError("channel availability must be a boolean")
        with self._transaction():
            self._project(project_id)
            self._db.execute("UPDATE m02_projects SET channel_available=? WHERE project_id=?",
                             (int(available), project_id))

    def _cpu_pins(self):
        m02_cpu.require_supported_platform()
        if self.cpu_root is None:
            raise ValueError("CPU execution requires a trusted dispatcher root")
        return m02_cpu.verify_pins(self.cpu_root)

    def cpu_authorization(self, payload, job=None, *, require_current=False):
        """Resolve authority from accepted control records, never from Queue data.

        Without a job this is the pre-reservation/pre-enqueue validation. A
        worker additionally needs the same bound job and the current handoff.
        Promotion may inspect a historical handoff: the existing SQL CAS still
        decides whether it is current and permitted to publish.
        """
        payload = m02_cpu.validate_input(payload)
        with self._lock:
            handoff = self._db.execute("SELECT * FROM m02_handoffs WHERE handoff_id=?",
                                       (payload["handoff_id"],)).fetchone()
            if handoff is None:
                raise ValueError("CPU handoff is absent from the accepted control database")
            contract = json.loads(handoff["contract"])
            _closed(contract, {"project", "task"}, "CPU accepted contract")
            project_spec = _project_spec(contract["project"])
            spec = _task_spec(contract["task"])
            project = self._project(payload["project_id"])
            parent = None
            if payload["revision"] == 2:
                previous = self._db.execute("SELECT handoff_id FROM m02_handoffs WHERE task_id=? AND revision=1",
                                            (payload["task_id"],)).fetchone()
                if previous is None:
                    raise ValueError("CPU parent handoff is absent")
                parent = previous[0]
            expected = m02_cpu.make_input(spec, parent)
            if (spec["handler"] != m02_cpu.HANDLER or _canonical(expected) != _canonical(payload)
                    or handoff["input_sha256"] != payload["input_sha256"]
                    or any(handoff[key] != payload[key] for key in ("project_id", "task_id", "revision", "handoff_id"))
                    or project["revision"] != handoff["project_revision"]
                    or _canonical(project_spec) != project["spec"] or _sha(project_spec) != project["spec_sha256"]):
                raise ValueError("CPU input differs from its accepted control identity")
            task = self._task(payload["task_id"])
            if require_current and (task["handoff_id"] != payload["handoff_id"]
                    or task["revision"] != payload["revision"] or task["status"] != "dispatched"
                    or task["input_sha256"] != payload["input_sha256"]):
                raise ValueError("CPU handoff is no longer executable")
            if job is not None:
                intent = self._db.execute("SELECT * FROM m02_intents WHERE handoff_id=?",
                                          (payload["handoff_id"],)).fetchone()
                if intent is None or intent["queue_job_id"] != job["id"]:
                    raise ValueError("CPU job has no bound reservation")
                self._validate_job(intent, job)
                if intent["reserved_attempts"] != ATTEMPTS_PER_HANDOFF or not 1 <= job["attempts"] <= ATTEMPTS_PER_HANDOFF:
                    raise ValueError("CPU job exceeds its accepted reservation")
            return {"project_revision": project["revision"], "project_sha256": project["spec_sha256"],
                    "total_reserved_attempts": self._reserved()}

    def cpu_saved_result(self, job_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM m02_cpu_results WHERE queue_job_id=?", (job_id,)).fetchone()
            if row is None:
                return None
            result = json.loads(row["content"])
            if _sha(result) != row["sha256"] or result["input"]["handoff_id"] != row["handoff_id"]:
                raise ValueError("CPU durable result integrity mismatch")
            return result

    def cpu_bind_claim(self, job):
        """Recover exactly this job across submit ACK/control-binding loss.

        A worker may claim after Queue.submit commits and before _enqueue binds
        its ID. Resolve the existing immutable queue key, never allocate another
        reservation or treat that known gap as a failed computation attempt.
        """
        self.cpu_authorization(job["payload"])
        with self._lock:
            intent = self._db.execute("SELECT * FROM m02_intents WHERE handoff_id=?",
                                      (job["payload"]["handoff_id"],)).fetchone()
            if intent is None:
                raise ValueError("CPU job has no accepted reservation")
        self._validate_job(intent, job)
        existing = self.queue.by_idempotency_key(intent["queue_key"])
        if existing is None or existing["id"] != job["id"]:
            raise ValueError("CPU claimed job differs from its immutable queue identity")
        if intent["queue_job_id"] is None:
            self._enqueue(intent)
        elif intent["queue_job_id"] != job["id"]:
            raise ValueError("CPU reservation is already bound to another job")

    def cpu_save_result(self, job, result):
        with self._transaction():
            authorization = self.cpu_authorization(job["payload"], job, require_current=True)
            m02_cpu.validate_result(result, job["payload"], job, authorization)
            previous = self.cpu_saved_result(job["id"])
            if previous is not None:
                m02_cpu.validate_result(previous, job["payload"], job, authorization)
                if previous["scientific_sha256"] != result["scientific_sha256"]:
                    raise ValueError("CPU result identity is already bound to different scientific data")
                return previous
            self._db.execute("INSERT INTO m02_cpu_results VALUES (?,?,?,?)", (
                job["id"], job["payload"]["handoff_id"], _canonical(result), _sha(result)))
            return result

    def _validate_job(self, intent, job):
        kind = m02_cpu.KIND if json.loads(intent["payload"]).get("handler") == m02_cpu.HANDLER else "evidence"
        if (job["kind"] != kind or _canonical(job["payload"]) != intent["payload"]
                or job["capabilities"] != [kind]
                or job["max_attempts"] != ATTEMPTS_PER_HANDOFF):
            raise ValueError("queue identity has a conflicting immutable contract")

    def _enqueue(self, intent):
        job = self.queue.by_idempotency_key(intent["queue_key"])
        recovered = job is not None
        if job is None:
            payload = json.loads(intent["payload"])
            kind = m02_cpu.KIND if payload.get("handler") == m02_cpu.HANDLER else "evidence"
            if kind == m02_cpu.KIND:
                self._cpu_pins()
                self.cpu_authorization(payload)
            job = self.queue.submit(kind, payload,
                                    capabilities=[kind], idempotency_key=intent["queue_key"],
                                    max_attempts=ATTEMPTS_PER_HANDOFF)
            self._hit("after_queue_submit")
        self._validate_job(intent, job)
        with self._transaction():
            current = self._db.execute("SELECT * FROM m02_intents WHERE handoff_id=?",
                                       (intent["handoff_id"],)).fetchone()
            if current["queue_job_id"] is not None and current["queue_job_id"] != job["id"]:
                raise ValueError("intent is already bound to another queue job")
            if current["queue_job_id"] is None:
                if recovered:
                    self._count("duplicate_suppression")
                self._db.execute("""UPDATE m02_intents SET queue_job_id=?,
                    status=CASE WHEN status='reserved' THEN 'enqueued' ELSE status END
                    WHERE handoff_id=?""", (job["id"], intent["handoff_id"]))
                self._event("queue_bound", intent, {"queue_job_id": job["id"], "recovered": recovered})
            self._db.execute("""UPDATE m02_tasks SET queue_job_id=?, checkpoint='queue_submitted'
                WHERE task_id=? AND handoff_id=? AND revision=? AND status='dispatched'
                AND queue_job_id IS NULL""", (job["id"], intent["task_id"],
                                             intent["handoff_id"], intent["revision"]))
        return job

    def _cancel_intent(self, task, reason):
        changed = self._db.execute("INSERT OR IGNORE INTO m02_cancellations VALUES (?,?,?,'pending')",
                                   (task["handoff_id"], task["task_id"], reason)).rowcount
        if changed:
            self._event("cancel_intent", task, {"reason": reason})

    def _reconcile_cancellations(self, reasons=None):
        with self._lock:
            rows = list(self._db.execute("SELECT * FROM m02_cancellations WHERE status='pending' ORDER BY handoff_id"))
        changed = False
        for cancellation in rows:
            with self._lock:
                intent = self._db.execute("SELECT * FROM m02_intents WHERE handoff_id=?",
                                          (cancellation["handoff_id"],)).fetchone()
            if intent is not None:
                # Even an unbound reserved intent is materialized with the same key before
                # cancelling. A concurrent enqueue can never create an untracked late job.
                try:
                    job = self._enqueue(intent)
                except m02_cpu.UnsupportedCPUPlatform:
                    if reasons is not None:
                        reasons.append("cpu_platform_unsupported")
                    # Keep this cancellation pending for a supported host. The
                    # other project can still make progress without new CPU work.
                    continue
                self.queue.cancel(job["id"])
            self._hit("after_queue_cancel")
            with self._transaction():
                count = self._db.execute("""UPDATE m02_cancellations SET status='completed'
                    WHERE handoff_id=? AND status='pending'""", (cancellation["handoff_id"],)).rowcount
                if count:
                    handoff = self._db.execute("SELECT * FROM m02_handoffs WHERE handoff_id=?",
                                               (cancellation["handoff_id"],)).fetchone()
                    self._event("cancellation_confirmed", handoff,
                                {"queue_job_id": job["id"] if intent is not None else None})
                self._db.execute("""UPDATE m02_tasks SET status='cancelled', checkpoint='cancelled',
                    current_result_sha256=NULL WHERE task_id=? AND handoff_id=? AND status='cancel_requested'""",
                                 (cancellation["task_id"], cancellation["handoff_id"]))
            changed = True
        return changed

    def _promote(self, intent, job):
        try:
            payload = json.loads(intent["payload"])
            if payload.get("handler") == m02_cpu.HANDLER:
                authorization = self.cpu_authorization(payload, job)
                expected = self.cpu_saved_result(job["id"])
                m02_cpu.validate_result(expected, payload, job, authorization)
            else:
                expected = fixture_result(payload)
            valid = _canonical(job["result"]) == _canonical(expected)
        except (ValueError, TypeError, KeyError, UnicodeError):
            valid = False
        if not valid:
            with self._transaction():
                changed = self._db.execute("""UPDATE m02_intents SET status='failed'
                    WHERE handoff_id=? AND status IN ('reserved','enqueued')""", (intent["handoff_id"],)).rowcount
                self._db.execute("""UPDATE m02_tasks SET status='failed', checkpoint='result_contract_rejected'
                    WHERE task_id=? AND handoff_id=? AND revision=? AND status='dispatched'""",
                                 (intent["task_id"], intent["handoff_id"], intent["revision"]))
                if changed:
                    self._event("result_contract_rejected", intent, {"queue_job_id": job["id"]})
            return bool(changed)
        result_sha = _sha(expected)
        # Deliberate external-read/atomic-write boundary for the R1 -> R2 counterexample.
        self._hit("before_promotion")
        promoted = False
        with self._transaction():
            row = self._db.execute("SELECT * FROM m02_intents WHERE handoff_id=?",
                                   (intent["handoff_id"],)).fetchone()
            if row["status"] not in ("reserved", "enqueued"):
                return False
            changed = self._db.execute("""UPDATE m02_tasks
                SET status='awaiting_review', current_result_sha256=?, checkpoint='result_promoted'
                WHERE task_id=? AND project_id=? AND handoff_id=? AND revision=?
                AND input_sha256=? AND queue_job_id=? AND status='dispatched'
                AND EXISTS (SELECT 1 FROM m02_projects p WHERE p.project_id=m02_tasks.project_id AND p.revision=?)""",
                (result_sha, intent["task_id"], intent["project_id"], intent["handoff_id"],
                 intent["revision"], intent["input_sha256"], job["id"], intent["project_revision"])).rowcount
            if changed:
                self._db.execute("INSERT OR IGNORE INTO m02_artifacts VALUES (?,?)",
                                 (result_sha, _canonical(expected)))
                event_id = "m02-publication:" + _sha([intent["project_id"], intent["task_id"],
                                                     intent["handoff_id"], intent["revision"], result_sha])
                self._db.execute("INSERT INTO m02_outbox VALUES (?,?,?,?,?,'pending')",
                                 (event_id, intent["task_id"], intent["handoff_id"], intent["revision"], result_sha))
                self._count("promotions")
                status = "promoted"
                promoted = True
            else:
                self._count("stale_rejections")
                status = "stale_for_publication"
            self._db.execute("UPDATE m02_intents SET status=?,result_sha256=? WHERE handoff_id=?",
                             (status, result_sha, intent["handoff_id"]))
            self._event(status, intent, {"result_sha256": result_sha, "queue_job_id": job["id"]})
        if promoted:
            self._hit("after_control_promotion")
        return True

    def _reconcile(self, reasons=None):
        changed = self._reconcile_cancellations(reasons)
        with self._lock:
            intents = list(self._db.execute("SELECT * FROM m02_intents ORDER BY handoff_id"))
        for intent in intents:
            if intent["status"] not in ("reserved", "enqueued"):
                continue
            try:
                job = self._enqueue(intent)
            except m02_cpu.UnsupportedCPUPlatform:
                if reasons is not None:
                    reasons.append("cpu_platform_unsupported")
                continue
            if job["status"] == "completed":
                changed = self._promote(intent, job) or changed
            elif job["status"] in ("cancelled", "failed"):
                with self._transaction():
                    count = self._db.execute("""UPDATE m02_intents SET status=? WHERE handoff_id=?
                        AND status IN ('reserved','enqueued')""", (job["status"], intent["handoff_id"])).rowcount
                    self._db.execute("""UPDATE m02_tasks SET status=?,checkpoint=?
                        WHERE task_id=? AND handoff_id=? AND status='dispatched'""",
                        (job["status"], "queue_" + job["status"], intent["task_id"], intent["handoff_id"]))
                    if count:
                        self._event("queue_" + job["status"], intent, {"queue_job_id": job["id"]})
                changed = bool(count) or changed
        return changed

    def tick(self):
        """Reconcile durable work, dispatch at most one eligible task, then return."""
        reasons = []
        progressed = self._reconcile(reasons)
        selected = None
        with self._transaction():
            projects = list(self._db.execute("SELECT * FROM m02_projects ORDER BY project_id"))
            last = self._meta("last_project")
            projects.sort(key=lambda row: (row["project_id"] == last, row["project_id"]))
            for project in projects:
                tasks = list(self._db.execute("SELECT * FROM m02_tasks WHERE project_id=? ORDER BY task_id",
                                              (project["project_id"],)))
                if not any(task["status"] == "pending" for task in tasks):
                    continue
                if not project["channel_available"]:
                    reasons.append("channel_unavailable")
                    continue
                if any(task["status"] in ("dispatched", "awaiting_review", "cancel_requested") for task in tasks):
                    reasons.append("project_slot_busy")
                    continue
                # Revision can have a pending cancellation for an old handoff.
                if self._db.execute("""SELECT 1 FROM m02_cancellations c JOIN m02_tasks t
                    ON t.task_id=c.task_id WHERE t.project_id=? AND c.status='pending'""",
                    (project["project_id"],)).fetchone():
                    reasons.append("cancellation_pending")
                    continue
                for task in tasks:
                    if task["status"] != "pending":
                        continue
                    spec = json.loads(task["spec"])
                    if any(self._task(dep)["status"] != "accepted" for dep in spec["dependencies"]):
                        reasons.append("dependencies_not_accepted")
                        continue
                    if self._reserved() + ATTEMPTS_PER_HANDOFF > MAX_RESERVED_ATTEMPTS:
                        reasons.append("budget_exhausted")
                        continue
                    key = "m02:" + _sha([task["project_id"], task["task_id"], task["handoff_id"], task["input_sha256"]])
                    payload = _payload(json.loads(project["spec"]), spec, task["input_sha256"], task["parent_handoff"])
                    if spec["handler"] == m02_cpu.HANDLER:
                        try:
                            self._cpu_pins()
                        except m02_cpu.UnsupportedCPUPlatform:
                            reasons.append("cpu_platform_unsupported")
                            continue
                        # DB authority and hashes are checked before writing an
                        # intent or spending any part of the shared reserve.
                        self.cpu_authorization(payload)
                    self._db.execute("""INSERT INTO m02_intents VALUES
                        (?,?,?,?,?,?,?,?,NULL,'reserved',NULL,?)""", (
                        task["handoff_id"], task["task_id"], task["project_id"], task["revision"],
                        project["revision"], task["input_sha256"], key, _canonical(payload), ATTEMPTS_PER_HANDOFF))
                    self._db.execute("UPDATE m02_tasks SET status='dispatched',checkpoint='intent_reserved' WHERE task_id=?",
                                     (task["task_id"],))
                    self._db.execute("UPDATE m02_meta SET value=? WHERE key='last_project'", (task["project_id"],))
                    selected = dict(self._db.execute("SELECT * FROM m02_intents WHERE handoff_id=?",
                                                    (task["handoff_id"],)).fetchone())
                    self._event("reserved", selected, {
                        "reason": "dependencies_accepted_channel_available_budget_reserved",
                        "dependencies": spec["dependencies"], "channel_available": True,
                        "reserved_attempts": ATTEMPTS_PER_HANDOFF,
                        "total_reserved_attempts": self._reserved(),
                    })
                    break
                if selected:
                    break
        if selected:
            self._hit("after_intent_reserved")
            job = self._enqueue(selected)
            return {"status": "dispatched", "reason": "dependencies_accepted_channel_available_budget_reserved",
                    "task_id": selected["task_id"], "queue_job_id": job["id"]}
        return {"status": "progressed" if progressed else "waiting",
                "reason": ",".join(sorted(set(reasons))) if reasons else
                          ("reconciled" if progressed else "no_eligible_task")}

    def _revise(self, task, *, handoff_id, reason, goal=None, fixture_id=None, parameters=None):
        if task["revision"] >= MAX_REVISIONS:
            raise ValueError("bounded rework limit reached")
        if task["status"] in ("accepted", "cancelled", "cancel_requested", "exhausted"):
            raise ValueError("terminal task cannot be revised")
        _identity(handoff_id, "handoff_id")
        _text(reason, "revision reason")
        spec = json.loads(task["spec"])
        spec.update(revision=task["revision"] + 1, handoff_id=handoff_id)
        if goal is not None:
            spec["goal"] = goal
        if fixture_id is not None:
            spec["fixture_id"] = fixture_id
        if parameters is not None:
            spec["parameters"] = parameters
        spec = _task_spec(spec)
        if spec["handler"] == m02_cpu.HANDLER:
            self._cpu_pins()
        digest = self._insert_handoff(self._project(task["project_id"]), spec, task["handoff_id"])
        self._cancel_intent(task, reason)
        self._db.execute("""UPDATE m02_tasks SET spec=?,revision=?,handoff_id=?,input_sha256=?,
            status='pending',queue_job_id=NULL,current_result_sha256=NULL,parent_handoff=?,checkpoint='revised'
            WHERE task_id=? AND revision=?""", (_canonical(spec), spec["revision"], handoff_id, digest,
            task["handoff_id"], task["task_id"], task["revision"]))
        self._event("revised", spec, {"parent_handoff": task["handoff_id"], "reason": reason,
                                      "input_sha256": digest})

    def revise(self, task_id, *, expected_revision, handoff_id, reason, goal=None, fixture_id=None, parameters=None):
        with self._transaction():
            task = self._task(task_id)
            if type(expected_revision) is not int or task["revision"] != expected_revision:
                raise ValueError("stale task revision")
            self._revise(task, handoff_id=handoff_id, reason=reason, goal=goal, fixture_id=fixture_id, parameters=parameters)
            return self._public_task(self._task(task_id))

    def review(self, task_id, *, expected_revision, result_sha256, decision, note):
        if decision not in ("accepted", "rejected", "inconclusive"):
            raise ValueError("unsupported review decision")
        _text(note, "review note", empty=decision == "accepted")
        if not isinstance(result_sha256, str) or not _SHA.fullmatch(result_sha256):
            raise ValueError("invalid review result hash")
        if type(expected_revision) is not int:
            raise ValueError("invalid review revision")
        with self._transaction():
            task = self._task(task_id)
            previous = self._db.execute("SELECT * FROM m02_reviews WHERE task_id=? AND revision=?",
                                        (task_id, expected_revision)).fetchone()
            if previous is not None:
                if any(previous[key] != value for key, value in (
                        ("result_sha256", result_sha256), ("decision", decision), ("note", note))):
                    raise ValueError("review identity is already bound to another decision")
                self._count("duplicate_suppression")
                return self._public_task(task)
            if (task["revision"] != expected_revision or task["status"] != "awaiting_review"
                    or task["current_result_sha256"] != result_sha256):
                raise ValueError("review must reference the current promoted artifact and revision")
            self._db.execute("INSERT INTO m02_reviews VALUES (?,?,?,?,?,?)", (
                task["handoff_id"], task_id, expected_revision, result_sha256, decision, note))
            self._event("review", task, {"result_sha256": result_sha256, "decision": decision, "note": note})
            if decision == "accepted":
                self._db.execute("UPDATE m02_tasks SET status='accepted',checkpoint='review_accepted' WHERE task_id=?",
                                 (task_id,))
            elif expected_revision >= MAX_REVISIONS:
                self._db.execute("UPDATE m02_tasks SET status='exhausted',checkpoint='rework_limit_reached' WHERE task_id=?",
                                 (task_id,))
            else:
                handoff = "rework:" + _sha([task_id, task["handoff_id"], result_sha256, decision, note])
                self._revise(task, handoff_id=handoff, reason=note,
                             fixture_id=None if json.loads(task["spec"])["handler"] == m02_cpu.HANDLER else "repair_alpha")
            return self._public_task(self._task(task_id))

    def cancel(self, task_id, *, expected_revision, reason):
        _text(reason, "cancellation reason")
        with self._transaction():
            task = self._task(task_id)
            if type(expected_revision) is not int or task["revision"] != expected_revision:
                raise ValueError("stale task revision")
            if task["status"] == "accepted":
                raise ValueError("accepted task is already closed")
            if task["status"] == "cancelled":
                return self._public_task(task)
            self._cancel_intent(task, reason)
            self._db.execute("UPDATE m02_tasks SET status='cancel_requested',checkpoint='cancel_intent' WHERE task_id=?",
                             (task_id,))
        self._hit("after_cancel_intent")
        self._reconcile_cancellations()
        with self._lock:
            return self._public_task(self._task(task_id))

    def deliver_outbox(self):
        """Deliver finite immutable local events; no external mutable pointer.

        A real remote sink needs its own conditional-write contract. This local
        delivery ledger uses event_id as its unique receipt key and no network.
        """
        with self._lock:
            pending = list(self._db.execute("SELECT * FROM m02_outbox WHERE status='pending' ORDER BY event_id"))
        delivered = []
        for event in pending:
            self._hit("before_outbox_delivery")
            with self._transaction():
                current = self._db.execute("SELECT * FROM m02_outbox WHERE event_id=?", (event["event_id"],)).fetchone()
                if current["status"] != "pending":
                    continue
                task = self._task(event["task_id"])
                if (task["handoff_id"] != event["handoff_id"] or task["revision"] != event["revision"]
                        or task["current_result_sha256"] != event["result_sha256"]
                        or task["status"] not in ("awaiting_review", "accepted")):
                    self._db.execute("UPDATE m02_outbox SET status='superseded' WHERE event_id=?", (event["event_id"],))
                    old_handoff = self._db.execute("SELECT * FROM m02_handoffs WHERE handoff_id=?",
                                                   (event["handoff_id"],)).fetchone()
                    self._event("outbox_superseded", old_handoff,
                                {"publication_event_id": event["event_id"], "result_sha256": event["result_sha256"]})
                    continue
                artifact = self._db.execute("SELECT content FROM m02_artifacts WHERE sha256=?",
                                            (event["result_sha256"],)).fetchone()
                self._db.execute("INSERT OR IGNORE INTO m02_deliveries VALUES (?,?,?)",
                                 (event["event_id"], event["result_sha256"], artifact[0]))
                self._db.execute("UPDATE m02_outbox SET status='delivered' WHERE event_id=?", (event["event_id"],))
                self._event("outbox_delivered", task,
                            {"publication_event_id": event["event_id"], "result_sha256": event["result_sha256"]})
                delivered.append({**dict(event), "status": "delivered", "content": json.loads(artifact[0])})
        return delivered

    def snapshot(self):
        """Public bounded state; never exports Queue's lease tokens or digests."""
        with self._transaction():
            projects = [{**json.loads(row["spec"]), "revision": row["revision"],
                         "channel_available": bool(row["channel_available"])} for row in
                        self._db.execute("SELECT * FROM m02_projects ORDER BY project_id")]
            tasks = [self._public_task(row) for row in self._db.execute("SELECT * FROM m02_tasks ORDER BY task_id")]
            intents = [{key: row[key] for key in (
                "handoff_id", "task_id", "project_id", "revision", "input_sha256", "queue_key",
                "queue_job_id", "status", "result_sha256", "reserved_attempts")} for row in
                self._db.execute("SELECT * FROM m02_intents ORDER BY handoff_id")]
            result = {
                "schema": SCHEMA, "projects": projects, "tasks": tasks, "intents": intents,
                "artifacts": [{"sha256": row[0], "content": json.loads(row[1])} for row in
                              self._db.execute("SELECT sha256,content FROM m02_artifacts ORDER BY sha256")],
                "cpu_results": [{"queue_job_id": row[0], "sha256": row[1], "content": json.loads(row[2])} for row in
                                self._db.execute("SELECT queue_job_id,sha256,content FROM m02_cpu_results ORDER BY queue_job_id")],
                "outbox": [dict(row) for row in self._db.execute("SELECT * FROM m02_outbox ORDER BY event_id")],
                "reviews": [dict(row) for row in self._db.execute("SELECT * FROM m02_reviews ORDER BY handoff_id")],
                "cancellations": [dict(row) for row in self._db.execute("SELECT * FROM m02_cancellations ORDER BY handoff_id")],
                "events": [{**dict(row), "details": json.loads(row["details"])} for row in
                           self._db.execute("SELECT * FROM m02_events ORDER BY sequence")],
                "budget": {"max_reserved_attempts": MAX_RESERVED_ATTEMPTS, "reserved_attempts": self._reserved()},
                "counts": {key: int(self._meta(key)) for key in
                           ("duplicate_suppression", "stale_rejections", "promotions")},
                "model_calls": 0,
            }
        # Queue has a separate commit boundary. Public snapshots do not claim one
        # transaction spanning both databases; the cloud backup closes both first.
        actual = 0
        for intent in intents:
            job = self.queue.by_idempotency_key(intent["queue_key"])
            if job is not None:
                actual += job["attempts"]
        result["budget"]["actual_attempts"] = actual
        return result

    def close(self):
        with self._lock:
            self._db.close()

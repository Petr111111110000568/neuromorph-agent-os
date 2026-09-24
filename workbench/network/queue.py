"""Durable, fenced, at-least-once work queue for trusted research workers.

SQLite is the coordinator's local storage, not a shared network filesystem.
Workers communicate with one coordinator over its authenticated transport.
Lease tokens are returned only from ``claim``; only their digests are stored.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid


KINDS = frozenset({"discovery", "simulation", "evidence", "directory"})
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_ACTIVE_JOBS = 10_000
MAX_WORKERS = 1_000
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _json(value, limit, name):
    """Validate without accepting Python's NaN, non-string keys, or cycles."""
    def visit(item, depth):
        if depth > 32:
            raise ValueError(f"{name} exceeds JSON nesting limit")
        if item is None or isinstance(item, (str, bool)):
            return
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"{name} must contain finite JSON numbers")
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{name} must use string object keys")
                visit(child, depth + 1)
            return
        raise ValueError(f"{name} must be JSON")

    try:
        visit(value, 0)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > limit:
            raise ValueError(f"{name} exceeds {limit} bytes")
        return encoded
    except (RecursionError, OverflowError, UnicodeError) as exc:
        raise ValueError(f"{name} must be bounded valid JSON") from exc


def _identity(value):
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ValueError("worker_id must contain 1–128 ASCII letters, digits, . _ : -")
    return value


def _capabilities(value, *, allow_empty=False):
    if not isinstance(value, (list, tuple)) or len(value) > len(KINDS):
        raise ValueError("capabilities must be a bounded list")
    if not all(isinstance(x, str) and x in KINDS for x in value):
        raise ValueError("unknown capability")
    if len(value) != len(set(value)):
        raise ValueError("duplicate capabilities are unsupported")
    if not value and not allow_empty:
        raise ValueError("at least one capability is required")
    return sorted(set(value))


def _lease_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("lease_seconds must be a number")
    if not 1 <= value <= 3600 or not math.isfinite(value):
        raise ValueError("lease_seconds must be between 1 and 3600")
    return float(value)


def _idempotency_key(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or not all(32 < ord(x) < 127 for x in value)):
        raise ValueError("idempotency_key must contain 1–128 visible ASCII characters")
    return value


def _stamp(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds")


def _digest(token):
    if not isinstance(token, str) or len(token) < 32 or len(token) > 256:
        raise ValueError("invalid lease token")
    try:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise ValueError("invalid lease token") from exc


class Queue:
    """An atomic local coordinator queue with independent connection support.

    A job may execute again after worker failure. Consumers should make any
    external effects idempotent; a successful result is fenced by the lease.
    One worker identity may hold at most one live lease.
    """

    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), timeout=30, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS network_workers (
                id TEXT PRIMARY KEY, capabilities TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS network_jobs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                payload TEXT NOT NULL, capabilities TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL, worker_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                result TEXT, error TEXT,
                lease_digest TEXT, lease_until REAL,
                completion_digest TEXT, completion_fingerprint TEXT,
                completion_worker_id TEXT,
                idempotency_key TEXT UNIQUE, submission_fingerprint TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS network_jobs_status
                ON network_jobs(status, sequence);
            CREATE INDEX IF NOT EXISTS network_jobs_owner
                ON network_jobs(worker_id, status);
        """)

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

    @staticmethod
    def _public(row):
        return {
            "id": row["id"], "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "capabilities": json.loads(row["capabilities"]),
            "status": row["status"], "attempts": row["attempts"],
            "max_attempts": row["max_attempts"], "worker_id": row["worker_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "result": json.loads(row["result"]) if row["result"] is not None else None,
            "error": json.loads(row["error"]) if row["error"] is not None else None,
        }

    @staticmethod
    def _worker_public(row):
        return {"id": row["id"], "worker_id": row["id"],
                "capabilities": json.loads(row["capabilities"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def _row(self, job_id):
        if not isinstance(job_id, str) or len(job_id) > 128:
            raise ValueError("invalid job id")
        row = self._db.execute("SELECT * FROM network_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return row

    def _reap(self, now):
        self._db.execute("""
            UPDATE network_jobs
            SET status=CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                worker_id=NULL, lease_digest=NULL, lease_until=NULL,
                completion_digest=NULL, completion_fingerprint=NULL,
                completion_worker_id=NULL, updated_at=?,
                error='"Worker lease expired"'
            WHERE status='running' AND lease_until <= ?
        """, (_stamp(now), now))

    def submit(self, kind, payload, capabilities=None, idempotency_key=None, max_attempts=3):
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("kind must be discovery or simulation")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        payload_json = _json(payload, MAX_PAYLOAD_BYTES, "payload")
        required = _capabilities([kind] if capabilities is None else capabilities)
        if kind not in required:
            raise ValueError("required capabilities must include the job kind")
        capabilities_json = json.dumps(required)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be an integer between 1 and 10")
        if idempotency_key is not None:
            idempotency_key = _idempotency_key(idempotency_key)
        fingerprint = hashlib.sha256(_json(
            [kind, payload, required, max_attempts], MAX_PAYLOAD_BYTES + 1024, "submission"
        ).encode()).hexdigest()
        with self._transaction():
            now = time.time()
            self._reap(now)
            if idempotency_key is not None:
                old = self._db.execute("SELECT * FROM network_jobs WHERE idempotency_key=?",
                                       (idempotency_key,)).fetchone()
                if old is not None:
                    if old["submission_fingerprint"] != fingerprint:
                        raise ValueError("idempotency key is already used by another submission")
                    return self._public(old)
            count = self._db.execute("SELECT COUNT(*) FROM network_jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= MAX_ACTIVE_JOBS:
                raise ValueError("active job limit reached")
            job_id = "job_" + uuid.uuid4().hex
            self._db.execute("""
                INSERT INTO network_jobs
                (id,kind,payload,capabilities,status,max_attempts,created_at,updated_at,
                 idempotency_key,submission_fingerprint)
                VALUES (?,?,?,?,'queued',?,?,?,?,?)
            """, (job_id, kind, payload_json, capabilities_json, max_attempts,
                  _stamp(now), _stamp(now), idempotency_key, fingerprint))
            return self._public(self._row(job_id))

    def register_worker(self, worker_id, capabilities):
        worker_id = _identity(worker_id)
        capabilities = _capabilities(capabilities)
        with self._transaction():
            now = _stamp(time.time())
            existing = self._db.execute("SELECT * FROM network_workers WHERE id=?", (worker_id,)).fetchone()
            if existing is None:
                count = self._db.execute("SELECT COUNT(*) FROM network_workers").fetchone()[0]
                if count >= MAX_WORKERS:
                    raise ValueError("worker limit reached")
                self._db.execute("INSERT INTO network_workers VALUES (?,?,?,?)",
                                 (worker_id, json.dumps(capabilities), now, now))
            else:
                self._db.execute("UPDATE network_workers SET capabilities=?,updated_at=? WHERE id=?",
                                 (json.dumps(capabilities), now, worker_id))
            return self._worker_public(self._db.execute("SELECT * FROM network_workers WHERE id=?", (worker_id,)).fetchone())

    def claim(self, worker_id, lease_seconds=60):
        worker_id = _identity(worker_id)
        lease_seconds = _lease_seconds(lease_seconds)
        with self._transaction():
            now = time.time()
            self._reap(now)
            worker = self._db.execute("SELECT * FROM network_workers WHERE id=?", (worker_id,)).fetchone()
            if worker is None:
                raise KeyError(worker_id)
            self._db.execute("UPDATE network_workers SET updated_at=? WHERE id=?", (_stamp(now), worker_id))
            if self._db.execute("SELECT 1 FROM network_jobs WHERE worker_id=? AND status='running'", (worker_id,)).fetchone():
                return None
            available = set(json.loads(worker["capabilities"]))
            for row in self._db.execute("SELECT * FROM network_jobs WHERE status='queued' ORDER BY sequence"):
                if not set(json.loads(row["capabilities"])).issubset(available):
                    continue
                token = secrets.token_urlsafe(32)
                self._db.execute("""
                    UPDATE network_jobs SET status='running',attempts=attempts+1,
                        worker_id=?,updated_at=?,lease_digest=?,lease_until=?,
                        result=NULL,error=NULL,completion_digest=NULL,
                        completion_fingerprint=NULL,completion_worker_id=NULL
                    WHERE id=?
                """, (worker_id, _stamp(now), _digest(token), now + lease_seconds, row["id"]))
                job = self._public(self._row(row["id"]))
                job["lease_token"] = token
                job["lease_expires_at"] = _stamp(now + lease_seconds)
                return job
            return None

    def _owns(self, row, worker_id, token_digest, now):
        return (row["status"] == "running" and row["worker_id"] == worker_id
                and row["lease_until"] is not None and row["lease_until"] > now
                and row["lease_digest"] is not None
                and hmac.compare_digest(row["lease_digest"], token_digest))

    def heartbeat(self, job_id, worker_id, lease_token, lease_seconds=60):
        worker_id = _identity(worker_id)
        token_digest = _digest(lease_token)
        lease_seconds = _lease_seconds(lease_seconds)
        with self._transaction():
            now = time.time()
            row = self._row(job_id)
            if not self._owns(row, worker_id, token_digest, now):
                raise ValueError("lease is stale, expired, or belongs to another worker")
            # A heartbeat can extend, but never shorten, a still-valid lease.
            until = max(row["lease_until"], now + lease_seconds)
            self._db.execute("UPDATE network_jobs SET lease_until=?,updated_at=? WHERE id=?",
                             (until, _stamp(now), job_id))
            self._db.execute("UPDATE network_workers SET updated_at=? WHERE id=?", (_stamp(now), worker_id))
            return self._public(self._row(job_id))

    def finish(self, job_id, worker_id, lease_token, result=None, error=None):
        worker_id = _identity(worker_id)
        token_digest = _digest(lease_token)
        if error is not None and result is not None:
            raise ValueError("finish accepts either result or error")
        result_json = _json(result, MAX_RESULT_BYTES, "result")
        error_json = _json(error, MAX_ERROR_BYTES, "error")
        if error is not None and not isinstance(error, (str, dict)):
            raise ValueError("error must be a string or JSON object")
        completion = hashlib.sha256((result_json + "\n" + error_json).encode()).hexdigest()
        with self._transaction():
            now = time.time()
            row = self._row(job_id)
            if (row["completion_digest"] is not None
                    and hmac.compare_digest(row["completion_digest"], token_digest)
                    and row["completion_worker_id"] == worker_id):
                if row["completion_fingerprint"] != completion:
                    raise ValueError("completion already recorded with different content")
                return self._public(row)
            if not self._owns(row, worker_id, token_digest, now):
                raise ValueError("lease is stale, expired, or belongs to another worker")
            status = "completed" if error is None else ("failed" if row["attempts"] >= row["max_attempts"] else "queued")
            self._db.execute("""
                UPDATE network_jobs SET status=?,updated_at=?,result=?,error=?,
                    lease_digest=NULL,lease_until=NULL,completion_digest=?,
                    completion_fingerprint=?,completion_worker_id=?,
                    worker_id=CASE WHEN ?='queued' THEN NULL ELSE worker_id END
                WHERE id=?
            """, (status, _stamp(now), result_json, error_json, token_digest,
                  completion, worker_id, status, job_id))
            self._db.execute("UPDATE network_workers SET updated_at=? WHERE id=?", (_stamp(now), worker_id))
            return self._public(self._row(job_id))

    def cancel(self, job_id):
        with self._transaction():
            row = self._row(job_id)
            if row["status"] in {"queued", "running"}:
                self._db.execute("""
                    UPDATE network_jobs SET status='cancelled',updated_at=?,
                        lease_digest=NULL,lease_until=NULL,completion_digest=NULL,
                        completion_fingerprint=NULL,completion_worker_id=NULL
                    WHERE id=?
                """, (_stamp(time.time()), job_id))
            return self._public(self._row(job_id))

    def get(self, job_id):
        with self._transaction():
            self._reap(time.time())
            return self._public(self._row(job_id))

    def by_idempotency_key(self, key):
        """Recover a submission across coordinator crashes, without lease secrets.

        This coordinator-side lookup intentionally does not claim the job or
        create a replacement if the key is absent.
        """
        key = _idempotency_key(key)
        with self._transaction():
            self._reap(time.time())
            row = self._db.execute("SELECT * FROM network_jobs WHERE idempotency_key=?",
                                   (key,)).fetchone()
            return self._public(row) if row is not None else None

    def jobs(self):
        with self._transaction():
            self._reap(time.time())
            return [self._public(row) for row in self._db.execute("SELECT * FROM network_jobs ORDER BY sequence")]

    def workers(self):
        with self._lock:
            return [self._worker_public(row) for row in self._db.execute("SELECT * FROM network_workers ORDER BY created_at,id")]

    def close(self):
        with self._lock:
            self._db.close()

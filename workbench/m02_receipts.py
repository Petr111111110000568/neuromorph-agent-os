"""Pinned historical M02 receipts, associated with owner-scoped Studio projects.

The accepted checkout and its separate operator registry are the trust root.
This module never downloads a receipt, imports a SQLite checkpoint, dispatches a
worker or accepts a digest/registry from a client. Hashes establish identity to
that registry, not scientific truth or a fresh verification of GitHub.
"""
import hashlib
import hmac
import json
from pathlib import Path
import re
import stat
import uuid

from .store import canonical, now
from .studio import ID, invalid


MAX_RECEIPT_BYTES = 128 * 1024
MAX_REGISTRY_BYTES = 16 * 1024
MAX_IMPORTS_PER_OWNER = 4000
REPOSITORY = "Petr111111110000568/neuromorph-agent-os"
COMMIT = "ceae671452365a84dcff2787b4ac66d7372a4f13"
WORKFLOW = ".github/workflows/m02-checkpoint.yml"
_PHASES = {"seed": "36277624672", "resume": "36277674850"}
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_FIELDS = {"repository", "workflow_id", "branch", "commit", "run_id"}
_ENTRY_FIELDS = {"receipt_id", "path", "phase", "identity", "canonical_sha256",
                 "previous_run_id", "previous_manifest_sha256"}
_RECEIPT_FIELDS = {"schema_version", "phase", "identity", "previous_run_id",
                   "previous_manifest_sha256", "restored_file_hashes", "initial_state",
                   "state", "input_sha256", "model_calls", "additional_spend_usd", "limitations"}
_SENSITIVE_KEYS = {"lease_token", "lease_digest", "completion_digest", "completion_fingerprint",
                   "authorization", "cookie", "cookies", "password", "api_key", "secret"}


def _require(condition):
    if not condition:
        raise ValueError("receipt admission refused")


def _closed(value, fields):
    _require(isinstance(value, dict) and set(value) == fields)


def _digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _identity(phase):
    return {"repository": REPOSITORY, "workflow_id": WORKFLOW, "branch": "main",
            "commit": COMMIT, "run_id": _PHASES[phase]}


def _read_json(root, relative, maximum):
    """Read only a fixed repository path, bounded before and during parsing."""
    path = root / relative
    for part in (root, *list(path.parents)[:len(Path(relative).parts) - 1], path):
        info = part.lstat()
        _require(not stat.S_ISLNK(info.st_mode) and
                 not (getattr(info, "st_file_attributes", 0) & 0x400))
    info = path.stat()
    _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= maximum)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    _require(len(raw) <= maximum)

    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result)
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError("non-finite JSON")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant)
    nodes = 0

    def bounded(item, depth=0):
        nonlocal nodes
        nodes += 1
        _require(depth <= 16 and nodes <= 20000)
        if isinstance(item, dict):
            _require(len(item) <= 64 and not (set(item) & _SENSITIVE_KEYS))
            for key, child in item.items():
                _require(len(key) <= 100)
                bounded(child, depth + 1)
        elif isinstance(item, list):
            _require(len(item) <= 256)
            for child in item:
                bounded(child, depth + 1)
        elif isinstance(item, str):
            _require(len(item) <= 4096 and "\x00" not in item)
            item.encode("utf-8")
        elif item is None or type(item) is bool:
            pass
        else:
            _require(type(item) is int and abs(item) <= 2 ** 53)

    bounded(value)
    return value


def _state_summary(state):
    _closed(state, {"budget", "counts", "jobs", "reviews", "tasks", "transitions"})
    _closed(state["budget"], {"actual_attempts", "max_reserved_attempts", "reserved_attempts"})
    _closed(state["counts"], {"duplicate_suppression", "promotions", "stale_rejections"})
    for numbers in (state["budget"], state["counts"]):
        _require(all(type(n) is int and 0 <= n <= 256 for n in numbers.values()))
    for name, limit in (("jobs", 4), ("reviews", 4), ("tasks", 8), ("transitions", 256)):
        _require(isinstance(state[name], list) and len(state[name]) <= limit)
    _require(all(isinstance(task, dict) and isinstance(task.get("status"), str) for task in state["tasks"]))
    return {"task_count": len(state["tasks"]),
            "accepted_tasks": sum(task["status"] == "accepted" for task in state["tasks"]),
            **state["budget"], "review_count": len(state["reviews"]),
            "transition_count": len(state["transitions"]), "model_calls": 0,
            "additional_spend_usd": 0}


def _public_receipt(receipt):
    """A projection, not bytes to rehash against the source receipt digest."""
    state = receipt["state"]
    return {**{key: receipt[key] for key in (
        "schema_version", "phase", "identity", "previous_run_id", "previous_manifest_sha256",
        "input_sha256", "model_calls", "additional_spend_usd", "limitations")},
        "state": {"budget": dict(state["budget"]), "counts": dict(state["counts"]),
            "tasks": [{key: task[key] for key in ("task_id", "project_id", "revision", "status")}
                      for task in state["tasks"]],
            "reviews": [{key: review[key] for key in ("task_id", "revision", "decision")}
                        for review in state["reviews"]]}}


class M02ReceiptLibrary:
    """A server-configured library and immutable owner/project associations."""

    def __init__(self, store, root=None):
        self.store = store
        # root is application/test configuration, never taken from HTTP input.
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        with store.lock, store.db:
            store.db.execute("""CREATE TABLE IF NOT EXISTS studio_m02_imports (
                owner TEXT NOT NULL, id TEXT NOT NULL, project_id TEXT NOT NULL,
                receipt_id TEXT NOT NULL, request_key TEXT NOT NULL,
                request_hash TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(owner,id), UNIQUE(owner,request_key),
                UNIQUE(owner,project_id,receipt_id))""")

    @staticmethod
    def _owner(owner):
        if (not isinstance(owner, str) or not 1 <= len(owner) <= 240
                or any(ord(c) < 32 for c in owner)):
            raise invalid("Invalid workspace owner")
        return owner

    def _catalog(self):
        try:
            registry = _read_json(self.root, "config/m02_receipt_registry.json", MAX_REGISTRY_BYTES)
            _closed(registry, {"schema_version", "registry_id", "receipts"})
            _require(type(registry["schema_version"]) is int and registry["schema_version"] == 1)
            _require(registry["registry_id"] == "m02-cloud-receipts-pr18-v1")
            _require(isinstance(registry["receipts"], list) and len(registry["receipts"]) == 2)
            catalog = {}
            for entry in registry["receipts"]:
                _closed(entry, _ENTRY_FIELDS)
                phase = entry["phase"]
                _require(isinstance(phase, str) and phase in _PHASES and phase not in catalog)
                _closed(entry["identity"], _IDENTITY_FIELDS)
                _require(entry["identity"] == _identity(phase))
                receipt_id = "m02-" + phase + "-" + _PHASES[phase]
                _require(entry["receipt_id"] == receipt_id)
                _require(entry["path"] == "data/m02_receipts/" + phase + ".json")
                _require(isinstance(entry["canonical_sha256"], str) and _SHA.fullmatch(entry["canonical_sha256"]))
                receipt = _read_json(self.root, entry["path"], MAX_RECEIPT_BYTES)
                _closed(receipt, _RECEIPT_FIELDS)
                _require(hmac.compare_digest(_digest(receipt), entry["canonical_sha256"]))
                _require(type(receipt["schema_version"]) is int and receipt["schema_version"] == 1)
                _require(receipt["phase"] == phase and receipt["identity"] == entry["identity"])
                for name in ("previous_run_id", "previous_manifest_sha256"):
                    _require(receipt[name] == entry[name])
                _require(type(receipt["model_calls"]) is int and receipt["model_calls"] == 0)
                _require(type(receipt["additional_spend_usd"]) is int and receipt["additional_spend_usd"] == 0)
                _require(isinstance(receipt["input_sha256"], str) and _SHA.fullmatch(receipt["input_sha256"]))
                _require(isinstance(receipt["limitations"], list) and receipt["limitations"] and
                         all(isinstance(item, str) for item in receipt["limitations"]))
                summary = _state_summary(receipt["state"])
                _require(summary["max_reserved_attempts"] == 8 and summary["task_count"] == 2)
                _require((summary["reserved_attempts"], summary["actual_attempts"], summary["accepted_tasks"]) ==
                         ((4, 1, 1) if phase == "seed" else (6, 3, 2)))
                if phase == "seed":
                    _require(all(receipt[key] is None for key in (
                        "previous_run_id", "previous_manifest_sha256", "initial_state", "restored_file_hashes")))
                else:
                    _require(receipt["previous_run_id"] == _PHASES["seed"])
                    _require(isinstance(receipt["previous_manifest_sha256"], str)
                             and _SHA.fullmatch(receipt["previous_manifest_sha256"]))
                    _closed(receipt["restored_file_hashes"], {"control.sqlite", "queue.sqlite", "seed-receipt.json"})
                catalog[phase] = {"receipt_id": receipt_id,
                    "title": "M02: исходный облачный запуск" if phase == "seed" else "M02: восстановление и рецензирование",
                    "phase": phase, "identity": entry["identity"],
                    "source_url": "https://github.com/" + REPOSITORY + "/actions/runs/" + _PHASES[phase],
                    "canonical_sha256": entry["canonical_sha256"], "summary": summary,
                    "limitations": list(receipt["limitations"]), "receipt": receipt}
            _require(set(catalog) == set(_PHASES))
            _require(catalog["resume"]["receipt"]["initial_state"] == catalog["seed"]["receipt"]["state"])
            _require(catalog["resume"]["receipt"]["input_sha256"] == catalog["seed"]["receipt"]["input_sha256"])
            for entry in catalog.values():
                entry["receipt"] = _public_receipt(entry["receipt"])
                entry["receipt_representation"] = "bounded_projection"
            return [catalog["seed"], catalog["resume"]]
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            # Do not expose local paths, malformed content or file-system diagnostics.
            raise invalid("Pinned historical receipt library is unavailable", "receipt_library_unavailable", 503) from None

    def snapshot(self, owner="local"):
        owner = self._owner(owner)
        catalog = self._catalog()
        with self.store.lock:
            rows = self.store.db.execute("""SELECT i.payload FROM studio_m02_imports i
                JOIN studio_records p ON p.owner=i.owner AND p.id=i.project_id AND p.kind='project'
                WHERE i.owner=? ORDER BY i.rowid DESC LIMIT ?""", (owner, MAX_IMPORTS_PER_OWNER)).fetchall()
        return {"catalog": catalog, "imports": [json.loads(row[0]) for row in rows],
                "capabilities": {"import_receipts": True, "model_calls": False,
                    "network_calls": False, "execution": False, "live_verification": False,
                    "historical": True, "scientific_validation": False,
                    "notice": "Pinned historical cloud receipts; no new execution, live GitHub check or scientific validation."}}

    def import_receipt(self, data, owner="local"):
        owner = self._owner(owner)
        if not isinstance(data, dict) or set(data) != {"project_id", "receipt_id", "idempotency_key"}:
            raise invalid("Expected only project_id, receipt_id and idempotency_key")
        for key in ("project_id", "receipt_id", "idempotency_key"):
            if not isinstance(data[key], str) or not ID.fullmatch(data[key]):
                raise invalid("Invalid field: " + key)
        catalog = {item["receipt_id"]: item for item in self._catalog()}
        if data["receipt_id"] not in catalog:
            raise invalid("Pinned receipt not found", "receipt_not_found", 404)
        item = catalog[data["receipt_id"]]
        request_hash = _digest({"project_id": data["project_id"], "receipt_id": data["receipt_id"]})
        db = self.store.db
        with self.store.lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                # Ownership and kind are checked in the same transaction as binding,
                # including an idempotent replay; there is no preflight-only check.
                if not db.execute("SELECT 1 FROM studio_records WHERE owner=? AND id=? AND kind='project'",
                                  (owner, data["project_id"])).fetchone():
                    raise invalid("Project not found", "not_found", 404)
                previous = db.execute("SELECT request_hash,payload FROM studio_m02_imports WHERE owner=? AND request_key=?",
                                      (owner, data["idempotency_key"])).fetchone()
                if previous:
                    if previous[0] != request_hash:
                        raise invalid("Idempotency key already used for another request", "idempotency_conflict", 409)
                    db.commit()
                    return json.loads(previous[1])
                if db.execute("SELECT 1 FROM studio_m02_imports WHERE owner=? AND project_id=? AND receipt_id=?",
                              (owner, data["project_id"], data["receipt_id"])).fetchone():
                    raise invalid("Receipt already imported; refresh the project", "receipt_already_imported", 409)
                if db.execute("SELECT COUNT(*) FROM studio_m02_imports WHERE owner=?", (owner,)).fetchone()[0] >= MAX_IMPORTS_PER_OWNER:
                    raise invalid("Workspace receipt import limit reached", "workspace_limit", 409)
                record = {"id": uuid.uuid4().hex, "project_id": data["project_id"],
                          **{key: item[key] for key in ("receipt_id", "canonical_sha256", "identity",
                                                       "source_url", "phase", "title", "summary")},
                          "imported_at": now()}
                db.execute("INSERT INTO studio_m02_imports VALUES (?,?,?,?,?,?,?)",
                           (owner, record["id"], data["project_id"], data["receipt_id"],
                            data["idempotency_key"], request_hash, canonical(record)))
                db.commit()
                return record
            except BaseException:
                db.rollback()
                raise

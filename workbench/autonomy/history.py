"""Opt-in, bounded SQLite observations of public discovery metadata.

No network requests, inference, enrollment, or executable resource imports.
An exact cycle artifact is idempotent; a later observation of the same content
updates last_seen without inventing a change. Retention is not source deletion.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

MAX_RESOURCES = 2000
MAX_OBSERVATIONS = 10000
MAX_RUNS = 2000
MAX_CANDIDATES = 32
MAX_CANDIDATE_BYTES = 12 * 1024
MAX_RUN_BYTES = 16 * 1024
MAX_DB_BYTES = 256 * 1024 * 1024
APPLICATION_ID = 0x4D484448
SCHEMA_VERSION = 1
TIMESTAMP_KEYS = frozenset({
    "retrieved_at", "seen_at", "imported_at", "observed_at",
    "first_seen", "last_seen", "created_at", "updated_at",
})
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_SCHEMA = {
    "runs": """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, observed_at TEXT NOT NULL,
        fingerprint TEXT NOT NULL, summary_json TEXT NOT NULL)""",
    "resources": """CREATE TABLE resources (
        resource_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        content_digest TEXT NOT NULL, candidate_json TEXT NOT NULL)""",
    "observations": """CREATE TABLE observations (
        run_id TEXT NOT NULL, resource_id TEXT NOT NULL, observed_at TEXT NOT NULL,
        change_kind TEXT NOT NULL, content_digest TEXT NOT NULL,
        candidate_json TEXT NOT NULL, PRIMARY KEY(run_id, resource_id))""",
    "observations_resource": """CREATE INDEX observations_resource
        ON observations(resource_id, observed_at)""",
}


def _canonical(value, maximum):
    """Bounded plain JSON; no non-string keys, NaN, cycles, or custom objects."""
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 20 or count > 5000:
            raise ValueError("History JSON structure exceeds limits")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("History JSON keys must be strings")
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise ValueError("History accepts plain JSON values only")
        elif type(item) is str and any(ord(c) < 32 and c not in "\n\r\t" for c in item):
            raise ValueError("History JSON contains control characters")
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > maximum:
            raise ValueError("History JSON byte limit exceeded")
        return raw
    except (TypeError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError("Invalid history JSON") from exc


def _stable(value):
    if type(value) is dict:
        return {key: _stable(child) for key, child in value.items()
                if key not in TIMESTAMP_KEYS}
    if type(value) is list:
        return [_stable(child) for child in value]
    return value


def candidate_digest(candidate):
    """Hash public candidate fields, excluding observation times recursively.

    Revision, license, provider IDs and raw_sha256 remain significant.
    This is a metadata digest, not independent verification of the resource.
    """
    if type(candidate) is not dict:
        raise ValueError("History candidate must be an object")
    _canonical(candidate, MAX_CANDIDATE_BYTES)
    return hashlib.sha256(_canonical(_stable(candidate), MAX_CANDIDATE_BYTES).encode("utf-8")).hexdigest()


def _timestamp(value):
    if type(value) is not str or not _TIME.fullmatch(value):
        raise ValueError("History requires an ISO timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.year < 1970:
            raise ValueError("History timestamp is before 1970")
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as exc:
        raise ValueError("Invalid history timestamp") from exc


def _check_candidate_times(value):
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is dict:
            for key, child in item.items():
                if key in TIMESTAMP_KEYS and child not in (None, ""):
                    _timestamp(child)
                elif type(child) in (dict, list):
                    stack.append(child)
        elif type(item) is list:
            stack.extend(item)


def _prepare_cycle(cycle):
    if type(cycle) is not dict:
        raise ValueError("History cycle must be an object")
    cycle_id = cycle.get("cycle_id")
    if type(cycle_id) is not str or not _HASH.fullmatch(cycle_id):
        raise ValueError("History cycle_id must be a SHA256")
    observed_at = _timestamp(cycle.get("created_at"))
    discovery = cycle.get("discovery")
    if type(discovery) is not dict:
        raise ValueError("History cycle has no discovery object")
    candidates = discovery.get("candidates")
    if type(candidates) is not list or len(candidates) > MAX_CANDIDATES:
        raise ValueError("History candidate count exceeds limits")
    prepared = {}
    for candidate in candidates:
        raw = _canonical(candidate, MAX_CANDIDATE_BYTES)
        digest = candidate_digest(candidate)
        _check_candidate_times(candidate)
        ident = candidate.get("id")
        if type(ident) is not str or not 1 <= len(ident) <= 600 or not ident.strip():
            raise ValueError("History candidate has an invalid ID")
        if candidate.get("endpoint_contacted", False) is not False or candidate.get("enrolled", False) is not False:
            raise ValueError("History only accepts read-only discovery candidates")
        if ident in prepared:
            if prepared[ident]["digest"] != digest:
                raise ValueError("Conflicting duplicate history candidate")
            continue
        prepared[ident] = {"id": ident, "digest": digest, "json": raw}
    mode = cycle.get("mode", "offline")
    if mode not in ("online", "offline"):
        raise ValueError("Invalid history discovery mode")
    requests = discovery.get("requests", 0)
    if type(requests) is not int or not 0 <= requests <= 4:
        raise ValueError("Invalid history discovery request count")
    reports = discovery.get("provider_reports", [])
    if type(reports) is not list or len(reports) > 4:
        raise ValueError("Invalid history provider reports")
    inference = cycle.get("inference", {})
    if type(inference) is not dict:
        raise ValueError("Invalid history inference summary")
    status = inference.get("status", cycle.get("status", "unknown"))
    if type(status) is not str or not 1 <= len(status) <= 80:
        raise ValueError("Invalid history inference status")
    summary = {"mode": mode, "inference_status": status,
               "discovery_requests": requests, "provider_reports": reports,
               "query": discovery.get("query", ""), "candidate_count": len(prepared)}
    summary_json = _canonical(summary, MAX_RUN_BYTES)
    run_id = hashlib.sha256((cycle_id + "\n" + observed_at).encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256(_canonical({
        "cycle_id": cycle_id, "observed_at": observed_at, "summary": summary,
        "candidates": [[ident, entry["digest"]] for ident, entry in sorted(prepared.items())],
    }, MAX_RUN_BYTES + 32 * 800).encode("utf-8")).hexdigest()
    return cycle_id, observed_at, run_id, fingerprint, summary_json, prepared


def _file_state(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _reject_link(path, info):
    if info is not None and (stat.S_ISLNK(info.st_mode) or
            getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("History symlink or reparse point rejected")


def _check_path(path):
    raw = os.fspath(path)
    if not isinstance(raw, str) or "\x00" in raw:
        raise ValueError("Invalid history database path")
    selected = Path(raw).absolute()
    if selected.anchor.startswith("\\\\"):
        raise ValueError("History requires a local filesystem")
    if ":" in selected.name or selected.name.rstrip(" .") != selected.name:
        raise ValueError("Invalid history database filename")
    if selected.name.split(".")[0].casefold() in {
            "con", "prn", "aux", "nul", *("com" + str(i) for i in range(1, 10)),
            *("lpt" + str(i) for i in range(1, 10))}:
        raise ValueError("Reserved history database filename")
    for component in (*reversed(selected.parents), selected):
        info = _file_state(component)
        _reject_link(component, info)
        if component != selected and (info is None or not stat.S_ISDIR(info.st_mode)):
            raise ValueError("History parent directory must already exist")
    info = _file_state(selected)
    if info is not None:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("History database must be one regular file")
        if info.st_size > MAX_DB_BYTES:
            raise ValueError("History database exceeds size limit")
    # Never let SQLite interpret, truncate or delete a pre-existing foreign sidecar.
    for suffix in ("-journal", "-wal", "-shm"):
        if _file_state(Path(str(selected) + suffix)) is not None:
            raise ValueError("Existing SQLite sidecar requires explicit recovery")
    return selected, info


def _schema_sql(value):
    return " ".join(value.split())


def _validate_schema(connection):
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise ValueError("Not a Meta-Harness discovery history database")
    if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise ValueError("Unsupported discovery history schema")
    rows = connection.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    expected = {name: ("index" if name == "observations_resource" else "table",
                       _schema_sql(sql)) for name, sql in _SCHEMA.items()}
    actual = {name: (kind, _schema_sql(sql or "")) for name, kind, sql in rows}
    if actual != expected:
        raise ValueError("Unexpected discovery history schema objects")
    if connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        raise ValueError("Discovery history database is corrupt")


def _open(path):
    path, original = _check_path(path)
    created = original is None
    if created:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        original = path.lstat()
    else:
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise ValueError("Not a SQLite history database")
    connection = None
    try:
        _check_path(path)
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                     timeout=5, isolation_level=None)
        connection.execute("PRAGMA trusted_schema=OFF")
        try:
            connection.enable_load_extension(False)
        except AttributeError:
            pass
        if hasattr(connection, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        current = path.lstat()
        _reject_link(path, current)
        if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("History database path changed while opening")
        if not created:
            _validate_schema(connection)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        connection.execute("PRAGMA max_page_count=" + str(MAX_DB_BYTES // page_size))
        if created:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for sql in _SCHEMA.values():
                    connection.execute(sql)
                connection.execute("PRAGMA application_id=" + str(APPLICATION_ID))
                connection.execute("PRAGMA user_version=" + str(SCHEMA_VERSION))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        raise


def _counts(connection):
    return {name: connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            for name in ("resources", "observations", "runs")}


def _trim(connection):
    before = _counts(connection)
    # This is explicitly bounded retention, never inference that a source vanished.
    for name, key, order, limit in (
        ("resources", "resource_id", "last_seen DESC,resource_id", MAX_RESOURCES),
        ("runs", "run_id", "observed_at DESC,run_id", MAX_RUNS),
    ):
        connection.execute("DELETE FROM " + name + " WHERE " + key +
                           " NOT IN (SELECT " + key + " FROM " + name +
                           " ORDER BY " + order + " LIMIT ?)", (limit,))
    # Keep provenance summaries for every retained observation. An expired run
    # is outside the bounded replay window and must not leave an orphan key.
    connection.execute("DELETE FROM observations WHERE run_id NOT IN (SELECT run_id FROM runs)")
    connection.execute("""DELETE FROM observations WHERE rowid NOT IN
        (SELECT rowid FROM observations ORDER BY observed_at DESC,rowid DESC LIMIT ?)""",
                       (MAX_OBSERVATIONS,))
    after = _counts(connection)
    return {name: before[name] - after[name] for name in before}


def record_cycle(path, cycle):
    """Record one sanitized engine cycle; parent directory must already exist.

    An exact (cycle_id, created_at) replay is idempotent. Same-time conflicting
    content is rejected. Old observations never roll current metadata backward.
    Rows are retained up to MAX_* limits; this is not an unbounded archive.
    Existing unknown databases, links, reparse points and sidecars are rejected.
    SQLite may create its own short-lived rollback journal next to the database.
    A hot journal left after an OS crash requires explicit operator recovery.
    """
    cycle_id, stamp, run_id, fingerprint, summary_json, candidates = _prepare_cycle(cycle)
    connection = _open(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute("SELECT fingerprint FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if prior is not None:
            if prior[0] != fingerprint:
                raise ValueError("History run identity has conflicting content")
            stats = {"status": "already_recorded", "run_id": run_id, "cycle_id": cycle_id,
                     "candidates": len(candidates), "new_resources": 0, "changed_resources": 0,
                     "unchanged_resources": 0, "historical_observations": 0,
                     "counts": _counts(connection),
                     "retention": {"resources": 0, "observations": 0, "runs": 0}}
            connection.commit()
            return stats
        connection.execute("INSERT INTO runs VALUES(?,?,?,?,?)",
                           (run_id, cycle_id, stamp, fingerprint, summary_json))
        stats = {"status": "recorded", "run_id": run_id, "cycle_id": cycle_id,
                 "candidates": len(candidates), "new_resources": 0, "changed_resources": 0,
                 "unchanged_resources": 0, "historical_observations": 0}
        for ident, entry in sorted(candidates.items()):
            prior = connection.execute(
                "SELECT first_seen,last_seen,content_digest FROM resources WHERE resource_id=?",
                (ident,)).fetchone()
            kind = None
            if prior is None:
                connection.execute("INSERT INTO resources VALUES(?,?,?,?,?)",
                                   (ident, stamp, stamp, entry["digest"], entry["json"]))
                stats["new_resources"] += 1
                kind = "new"
            else:
                first, last, previous_digest = prior
                if stamp == last and previous_digest != entry["digest"]:
                    raise ValueError("History same-time resource observation has conflicting content")
                if stamp < last:
                    connection.execute("UPDATE resources SET first_seen=? WHERE resource_id=?",
                                       (min(first, stamp), ident))
                    if previous_digest != entry["digest"]:
                        kind = "historical"
                        stats["historical_observations"] += 1
                    else:
                        stats["unchanged_resources"] += 1
                else:
                    if previous_digest != entry["digest"]:
                        kind = "changed"
                        stats["changed_resources"] += 1
                    else:
                        stats["unchanged_resources"] += 1
                    connection.execute("""UPDATE resources SET first_seen=?,last_seen=?,
                        content_digest=?,candidate_json=? WHERE resource_id=?""",
                        (min(first, stamp), stamp, entry["digest"], entry["json"], ident))
            if kind is not None:
                connection.execute("INSERT INTO observations VALUES(?,?,?,?,?,?)",
                    (run_id, ident, stamp, kind, entry["digest"], entry["json"]))
        stats["retention"] = _trim(connection)
        stats["counts"] = _counts(connection)
        connection.commit()
        return stats
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


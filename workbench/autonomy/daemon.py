"""Finite, restartable public-catalog discovery; never invokes a model.

Run state is checkpointed after outputs and history. A crash before that checkpoint
may repeat one catalog read (at-least-once), never enrollment or model inference.
The OS releases the process lock on exit; the small lock file intentionally remains.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import uuid

from .engine import ROOT, _no_secrets, run_cycle, validate_config
from .history import record_cycle
from .providers import MAX_RESPONSE_BYTES, json_load
from ..resource_policy import load_policy

MAX_STATE_BYTES = 4096
MAX_INTERVAL = 86400
MAX_EPOCH = 32503680000
_STATE_KEYS = {"schema_version", "job_id", "created_at", "next_due",
               "completed_cycles", "last_completed_at", "last_status"}
_STATUSES = {"pending", "cycle_recorded", "cycle_failed", "stopped"}


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < MAX_EPOCH:
        raise ValueError("Invalid schedule timestamp")
    return float(value)


def _safe_path(value, directory=False):
    path = Path(value).absolute()
    if path.anchor.startswith("\\\\"):
        raise ValueError("Schedule requires local files")
    for component in (*reversed(path.parents), path):
        if ":" in component.name or component.name.rstrip(" .") != component.name:
            raise ValueError("Invalid schedule path")
        if component.name.split(".")[0].casefold() in {
                "con", "prn", "aux", "nul", *("com" + str(i) for i in range(1, 10)),
                *("lpt" + str(i) for i in range(1, 10))}:
            raise ValueError("Reserved schedule path")
        try:
            info = component.lstat()
        except FileNotFoundError:
            if component != path:
                raise ValueError("Schedule parent directory must exist")
            return path
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Schedule link or reparse point rejected")
        if component != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Expected schedule directory")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Expected one regular schedule file")
    return path


@contextmanager
def _job_lock(path):
    """Nonblocking OS lock, retained by the open handle, not by PID assumptions."""
    path = _safe_path(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        info = os.fstat(fd)
        current = path.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("Schedule lock path changed")
        if info.st_nlink != 1 or info.st_size > 1:
            raise ValueError("Invalid schedule lock file")
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError("Schedule is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("Schedule is already running") from exc
        locked = True
        if info.st_size == 0:
            os.write(fd, b"1")
            os.fsync(fd)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _save_state(path, state):
    _safe_path(path)
    raw = (json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("Schedule checkpoint too large")
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path)
        os.replace(temporary, path)
        if os.name != "nt":
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        # Only our exclusively created temporary file is eligible for removal.
        if temporary.exists():
            temporary.unlink()


def _load_state(path, job_id, now, interval, max_cycles):
    if not path.exists():
        return {"schema_version": 1, "job_id": job_id, "created_at": now,
                "next_due": now, "completed_cycles": 0,
                "last_completed_at": None, "last_status": "pending"}
    with path.open("rb") as stream:
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("Schedule checkpoint too large")
    value = json_load(raw)
    if (type(value) is not dict or set(value) != _STATE_KEYS or
            type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise ValueError("Unknown schedule checkpoint")
    if value["job_id"] != job_id:
        raise ValueError("Schedule configuration changed; choose a new explicit state file")
    for name in ("created_at", "next_due"):
        _number(value[name])
    count = value["completed_cycles"]
    if type(count) is not int or not 0 <= count <= max_cycles:
        raise ValueError("Invalid schedule cycle count")
    if value["last_status"] not in _STATUSES:
        raise ValueError("Invalid schedule status")
    last = value["last_completed_at"]
    if count == 0:
        if last is not None or value["next_due"] != value["created_at"]:
            raise ValueError("Invalid initial schedule checkpoint")
    else:
        _number(last)
        if not math.isclose(value["next_due"], last + interval, rel_tol=0, abs_tol=0.001):
            raise ValueError("Invalid schedule deadline")
    return value


def run_schedule(config, output_dir, history_db, state_file, *, stop_file=None,
                 interval=3600, max_cycles=168, once=False, offline=False,
                 root=ROOT, clock=time.time, monotonic=time.monotonic, sleep=time.sleep,
                 cycle_runner=run_cycle, history_writer=record_cycle):
    """Execute at most max_cycles successful checkpoints, resuming the same job.

    All parent directories and the output directory must exist. Once returns
    immediately if not due. Local errors stop execution instead of retrying
    forever. Sleep is interruptible in at most five seconds; a running bounded
    catalog request completes before the stop file can be observed.
    """
    if type(interval) is not int or not 900 <= interval <= MAX_INTERVAL:
        raise ValueError("Interval must be 900..86400 seconds")
    if type(max_cycles) is not int or not 1 <= max_cycles <= 1000:
        raise ValueError("max_cycles must be 1..1000")
    if type(once) is not bool or type(offline) is not bool:
        raise ValueError("Expected boolean schedule options")
    validate_config(config)
    _no_secrets(config)
    load_policy(root)
    output = _safe_path(output_dir, directory=True)
    if not output.is_dir():
        raise ValueError("Schedule output directory must exist")
    history = _safe_path(history_db)
    state_path = _safe_path(state_file)
    stop = _safe_path(stop_file) if stop_file is not None else None
    lock = _safe_path(str(state_path) + ".lock")
    selected = [history, state_path, lock] + ([stop] if stop is not None else [])
    outputs = [output / name for name in ("cycle.json", "proposal.json", "report.md")]
    if len(set(selected)) != len(selected) or set(selected) & set(outputs):
        raise ValueError("Schedule paths overlap")
    for path in outputs:
        _safe_path(path)
    identity = {"config": config, "output": str(output), "history": str(history),
                "state": str(state_path), "stop": str(stop) if stop else None,
                "interval": interval, "max_cycles": max_cycles, "offline": offline}
    job_id = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                       allow_nan=False).encode("utf-8")).hexdigest()
    with _job_lock(lock):
        _safe_path(state_path)
        now = _number(clock())
        state = _load_state(state_path, job_id, now, interval, max_cycles)
        if not state_path.exists():
            _save_state(state_path, state)
        # A wall-clock change after startup never accelerates in-process pacing.
        due_monotonic = monotonic() + min(interval, max(0, state["next_due"] - now))
        while state["completed_cycles"] < max_cycles:
            if stop is not None and _safe_path(stop).exists():
                state = dict(state, last_status="stopped")
                _save_state(state_path, state)
                return dict(state, status="stopped")
            remaining = due_monotonic - monotonic()
            if remaining > 0:
                if once:
                    return dict(state, status="not_due")
                sleep(min(5.0, remaining))
                continue
            # Policy is revalidated on every iteration; never inherit credentials.
            load_policy(root)
            try:
                result = cycle_runner(config, output, online=not offline, provider="none",
                                      environment={}, root=root)
                history_writer(history, result)
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error):
                failed = dict(state, last_status="cycle_failed")
                _save_state(state_path, failed)
                raise
            finished = _number(clock())
            next_due = _number(finished + interval)
            updated = dict(state, completed_cycles=state["completed_cycles"] + 1,
                           last_completed_at=finished, next_due=next_due,
                           last_status="cycle_recorded")
            _save_state(state_path, updated)
            state = updated
            if once:
                return dict(state, status="cycle_recorded")
            due_monotonic = monotonic() + interval
        return dict(state, status="limit_reached")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Finite public discovery schedule without model calls")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True, help="Existing output directory")
    parser.add_argument("--history-db", required=True, help="History database; existing parent directory")
    parser.add_argument("--state-file", required=True, help="Atomic JSON checkpoint; existing parent directory")
    parser.add_argument("--stop-file", help="Create this file to stop between bounded catalog requests")
    parser.add_argument("--interval", type=int, default=3600)
    parser.add_argument("--max-cycles", type=int, default=168)
    parser.add_argument("--once", action="store_true", help="Perform one due cycle, otherwise return immediately")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = _safe_path(args.config)
        with path.open("rb") as stream:
            config = json_load(stream.read(MAX_RESPONSE_BYTES + 1))
        result = run_schedule(config, args.output_dir, args.history_db, args.state_file,
                              stop_file=args.stop_file, interval=args.interval,
                              max_cycles=args.max_cycles, once=args.once, offline=args.offline)
    except KeyboardInterrupt:
        print("Discovery schedule interrupted.", file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error):
        print("Discovery schedule stopped: invalid local state, configuration, or unavailable storage.", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


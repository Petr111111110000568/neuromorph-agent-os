"""Trusted distributed worker. Only discovery metadata and pinned builtins run.

No plugin installation, arbitrary shell execution or account credentials are
accepted in job payloads. The coordinator bearer secret stays in this process.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import threading

from .transport import Client, TransportError, _encode

HEARTBEAT_SECONDS = 15


def _worker_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("Worker id must contain 1–64 ASCII letters, digits, dots, underscores or hyphens")
    return value


def _retry(client, path, body, stop, attempts=3):
    for attempt in range(attempts):
        if stop.is_set():
            raise TransportError("Worker stopped")
        try:
            return client.request(path, body)
        except TransportError as exc:
            if not exc.retryable or attempt == attempts - 1:
                raise
            stop.wait(0.2 * (2 ** attempt))


def _execute(job, root, data_dir):
    """Whitelist handlers. Return a transport envelope, never an executable command."""
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Job payload must be an object")
    kind = job.get("kind")
    if kind == "simulation":
        if payload.keys() - {"plugin_id", "parameters"}:
            raise ValueError("Unknown simulation payload fields")
        from ..service import Service
        service = Service(root=root, db_path=data_dir / "workbench.sqlite3")
        try:
            result = service.run(payload)
        finally:
            service.close()
        if result.get("status") != "completed":
            # Builtin error messages are controlled, but do not copy arbitrary
            # diagnostics across the coordinator trust boundary.
            return {"error": {"code": "simulation_failed", "message": "Pinned builtin simulation did not complete"}}
        return {"result": result}
    if kind == "discovery":
        if payload.keys() - {"query", "providers", "limit", "offline"}:
            raise ValueError("Unknown discovery payload fields")
        from .discovery import discover
        return {"result": discover(**payload, root=root)}
    if kind == "evidence":
        if payload.keys() - {"query", "limit", "offline"}:
            raise ValueError("Unknown evidence payload fields")
        from ..society.evidence import collect
        return {"result": collect(**payload, root=root)}
    if kind == "directory":
        if payload.keys() - {"query", "provider", "limit", "offline"}:
            raise ValueError("Unknown directory payload fields")
        from ..society.directory import discover
        return {"result": discover(**payload, root=root)}
    raise ValueError("Unsupported job kind")


def run_worker(url, token, worker_id, root, data_dir=None, once=False,
               max_jobs=0, poll_seconds=1, capabilities=None, ca_file=None,
               stop_event=None):
    worker_id = _worker_id(worker_id)
    if not isinstance(max_jobs, int) or isinstance(max_jobs, bool) or not 0 <= max_jobs <= 1000000:
        raise ValueError("max_jobs must be an integer between 0 and 1000000")
    if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) or not 0.01 <= poll_seconds <= 60:
        raise ValueError("poll_seconds must be between 0.01 and 60")
    capabilities = ["simulation", "discovery", "evidence", "directory"] if capabilities is None else capabilities
    if not isinstance(capabilities, list) or not capabilities or any(not isinstance(c, str) or c not in ("simulation", "discovery", "evidence", "directory") for c in capabilities) or len(capabilities) != len(set(capabilities)):
        raise ValueError("Capabilities must be a nonempty unique list of simulation, discovery, evidence and/or directory")
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("Project root does not exist")
    data_dir = Path(data_dir).resolve() if data_dir else root / "state" / "workers" / worker_id
    data_dir.mkdir(parents=True, exist_ok=True)
    stop = stop_event or threading.Event()
    client = Client(url, token, ca_file=ca_file)
    _retry(client, "/v1/register", {"worker_id": worker_id, "capabilities": capabilities}, stop)
    summary = {"worker_id": worker_id, "claims": 0, "completed": 0, "failed": 0, "lost_leases": 0, "stopped": False}
    while not stop.is_set() and (max_jobs == 0 or summary["claims"] < max_jobs):
        job = _retry(client, "/v1/claim", {"worker_id": worker_id}, stop)
        if job is None:
            if once:
                break
            stop.wait(poll_seconds)
            continue
        if not isinstance(job, dict) or not all(isinstance(job.get(key), str) and job[key] for key in ("id", "lease_token")):
            raise TransportError("Hub returned invalid job lease")
        summary["claims"] += 1
        owner = {"job_id": job["id"], "worker_id": worker_id, "lease_token": job["lease_token"]}
        heartbeat_stop = threading.Event()
        lost_lease = threading.Event()

        def heartbeat():
            while not heartbeat_stop.wait(HEARTBEAT_SECONDS):
                try:
                    _retry(client, "/v1/heartbeat", owner, heartbeat_stop)
                except TransportError:
                    if not heartbeat_stop.is_set():
                        lost_lease.set()
                    return

        pulse = threading.Thread(target=heartbeat, name="lease-heartbeat", daemon=True)
        pulse.start()
        try:
            try:
                outcome = _execute(job, root, data_dir)
                # Check serialization/size before entering retries. Fail cleanly
                # instead of leaving a lease with an unreportable oversized result.
                _encode(dict(owner, **outcome))
            except Exception:
                outcome = {"error": {"code": "worker_execution_failed", "message": "Worker rejected the input or execution failed"}}
            if lost_lease.is_set():
                summary["lost_leases"] += 1
            else:
                try:
                    response = _retry(client, "/v1/finish", dict(owner, **outcome), stop)
                    if not isinstance(response, dict):
                        raise TransportError("Hub returned invalid finish response")
                    if "error" in outcome:
                        summary["failed"] += 1
                    else:
                        summary["completed"] += 1
                except TransportError as exc:
                    if exc.status in (400, 404):
                        summary["lost_leases"] += 1
                    else:
                        raise
        finally:
            heartbeat_stop.set()
            pulse.join(timeout=30)
        if once:
            break
    summary["stopped"] = stop.is_set()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Trusted Meta-Harness distributed worker")
    parser.add_argument("--hub", required=True, help="HTTPS hub URL; HTTP allowed only on loopback")
    parser.add_argument("--id", required=True, dest="worker_id")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--once", action="store_true", help="Claim at most one job, then exit (also exits if queue is empty)")
    parser.add_argument("--max-jobs", type=int, default=0, help="Stop after this many claims; 0 means continuous polling")
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument("--capabilities", default="simulation,discovery,evidence,directory", help="Comma-separated simulation,discovery,evidence,directory")
    parser.add_argument("--ca-file", type=Path, help="Additional trusted PEM CA for a private TLS deployment")
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        result = run_worker(args.hub, os.environ.get("META_HUB_TOKEN"), args.worker_id,
            args.root, args.data_dir, args.once, args.max_jobs, args.poll_seconds,
            args.capabilities.split(","), args.ca_file, stop)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        return 1 if result["failed"] or result["lost_leases"] else 0
    except (ValueError, TransportError, OSError):
        # No exception repr: a malformed URI/path may itself contain a credential.
        print(json.dumps({"error": "Worker could not start or coordinator connection failed; check endpoint, credentials, TLS and local paths"}), flush=True)
        return 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())

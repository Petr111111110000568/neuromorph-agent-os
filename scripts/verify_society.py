"""Actual local-network launcher, two worker processes and HTTP mission proof.

Offline by default. --online sends only the fixed public benchmark query to
Europe PMC/Crossref. It never opens a browser or claims biological validation.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "partial", "failed", "cancelled"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def worker_processes(pid):
    """Inspect only children of our launcher; optional on restricted hosts."""
    children = Path(f"/proc/{pid}/task/{pid}/children")
    if children.exists():
        found = []
        for item in children.read_text().split():
            try:
                args = Path(f"/proc/{item}/cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if b"workbench.network.worker" in args:
                found.append(int(item))
        return found
    if os.name == "posix" and shutil.which("ps"):
        try:
            result = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True,
                                    text=True, timeout=5, check=True)
            found = []
            for line in result.stdout.splitlines():
                fields = line.strip().split(None, 2)
                if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
                    if int(fields[1]) == pid and "workbench.network.worker" in fields[2].split():
                        found.append(int(fields[0]))
            return found
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def assert_no_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in {"lease_token", "lease_digest", "completion_digest", "authorization", "meta_hub_token", "api_key", "password"}:
                raise AssertionError("Export contains a credential field")
            assert_no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_credentials(item)


def verify(online=False, directories=False):
    proof = None
    observed_pids = None
    with tempfile.TemporaryDirectory(prefix="meta-society-verify-") as folder:
        folder = Path(folder)
        logfile = folder / "launcher.log"
        with logfile.open("w+") as stream:
            command = [sys.executable, "-m", "workbench", "--data-dir", str(folder / "state"),
                       "local-network", "--port", "0", "--hub-port", "0", "--federation-port", "0", "--workers", "2"]
            creation = {"start_new_session": True} if os.name == "posix" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            process = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=stream,
                                       stdin=subprocess.DEVNULL, text=True, **creation)
            try:
                deadline, port = time.monotonic() + 20, None
                while time.monotonic() < deadline:
                    match = re.search(r"(?:Meta-Harness|network): http://127\.0\.0\.1:(\d+)/(?:society|network|federation)\.html", logfile.read_text())
                    if match:
                        port = int(match.group(1))
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Launcher exited before readiness")
                    time.sleep(.1)
                if port is None:
                    raise RuntimeError("Launcher readiness deadline exceeded")

                def request(path, payload=None):
                    client = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                    try:
                        data = None if payload is None else json.dumps(payload).encode()
                        client.request("GET" if data is None else "POST", path, body=data,
                                       headers={} if data is None else {"Content-Type": "application/json"})
                        response = client.getresponse()
                        raw, status = response.read(), response.status
                        content_type = response.getheader("Content-Type", "")
                        if status != 200:
                            raise RuntimeError(f"HTTP {status} for {path}")
                        return json.loads(raw) if "application/json" in content_type else raw
                    finally:
                        client.close()

                deadline, workers = time.monotonic() + 10, []
                while time.monotonic() < deadline:
                    workers = request("/api/network")["workers"]
                    if len(workers) == 2:
                        break
                    time.sleep(.1)
                assert len(workers) == 2, "Two independent worker registrations required"
                observed_pids = worker_processes(process.pid)
                if observed_pids is not None:
                    assert len(observed_pids) == 2, "Expected two live worker subprocesses"
                static_assets = {}
                for path in ("/society.html", "/society.js", "/society.css"):
                    data = request(path)
                    assert isinstance(data, bytes) and len(data) > 100
                    static_assets[path] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

                question = "single cell epigenetics model validation"
                mission = request("/api/society/mission", {"question": question, "online": online, "max_sources": 3})
                deadline = time.monotonic() + 100
                while time.monotonic() < deadline:
                    # The daemon advances; the test does not call manual tick.
                    status = request("/api/society")
                    mission = next(x for x in status["missions"] if x["id"] == mission["id"])
                    if mission["status"] in TERMINAL:
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Launcher terminated during mission")
                    time.sleep(.25)
                assert mission["status"] == "completed", "Mission did not complete: " + json.dumps(mission.get("issues", []))
                jobs = mission["job_details"]
                assert len(jobs) == 3 and {x["kind"] for x in jobs} == {"discovery", "evidence", "simulation"}
                assert all(x["status"] == "completed" for x in jobs)
                evidence = next(x for x in jobs if x["kind"] == "evidence")["result"]
                discovery = next(x for x in jobs if x["kind"] == "discovery")["result"]
                method = mission["method_results"][0]
                assert evidence["items"], "Probe query should retrieve at least one evidence record"
                assert evidence["mode"] == ("public_abstract_triage" if online else "offline_index")
                assert method["provenance"]["output_sha256"] == hashlib.sha256(canonical(method["result"]).encode()).hexdigest()
                if online:
                    assert any(i["abstract_available"] for i in evidence["items"]), "Online probe returned no abstracts"
                else:
                    assert evidence["requests"] == discovery["requests"] == 0
                    assert all(not i["abstract_available"] for i in evidence["items"])
                directory_results = []
                if directories:
                    directory_ids = []
                    for provider, query in (("agentverse", "research"), ("huggingface_models", "scGPT"),
                                            ("huggingface_datasets", "ScienceAgentBench")):
                        job = request("/api/society/directory", {"query": query, "provider": provider,
                                      "limit": 3, "offline": not online, "data_class": "public"})
                        directory_ids.append(job["id"])
                    deadline = time.monotonic() + 80
                    while time.monotonic() < deadline:
                        seen = {j["id"]: j for j in request("/api/society")["directory_jobs"]}
                        current = [seen[j] for j in directory_ids]
                        if all(j["status"] not in ("queued", "running") for j in current):
                            break
                        time.sleep(.25)
                    for job in current:
                        assert job["status"] == "completed", "Directory job failed or returned invalid fields"
                        assert job["result_validation"] == "envelope_checked_not_external_identity_verified"
                        result = job["result"]
                        assert not result["errors"], "Directory provider returned errors"
                        assert result["requests"] == (1 if online else 0)
                        assert result["query"] == job["payload"]["query"]
                        assert result["provider"] == job["payload"]["provider"]
                        if online:
                            assert result["items"], "Public directory probe returned no matching entries"
                        for item in result["items"]:
                            assert item["status"] == "directory_listed_not_connected"
                            if online:
                                expected_host = "agentverse.ai" if result["provider"] == "agentverse" else "huggingface.co"
                                assert urlsplit(item["url"]).hostname == expected_host
                                assert urlsplit(item["url"]).scheme == "https"
                        directory_results.append({"job_id": job["id"], "worker_id": job["worker_id"],
                            "status": job["status"], "result_validation": job["result_validation"], "result": result,
                            "external_agent_executed": False})
                claim = request("/api/society/claim", {"mission_id": mission["id"],
                    "text": "Получены источники для последующей проверки содержания",
                    "scope": "Проверка программного пути; без вывода об эффективности вмешательств",
                    "source_ids": [evidence["items"][0]["id"]], "assessment": "unreviewed"})
                assert claim["independently_validated"] is False
                exported = request("/api/society/export")
                assert_no_credentials(exported)
                proof = {
                    "passed": True, "checked_at": datetime.now(timezone.utc).isoformat(), "online": online,
                    "launch": "python -m workbench local-network --workers 2",
                    "daemon_advanced_without_browser_or_manual_tick": True,
                    "worker_registrations": len(workers), "observed_worker_processes": None if observed_pids is None else len(observed_pids),
                    "worker_process_ids": observed_pids,
                    "mission": {key: mission[key] for key in ("id", "question", "status", "events", "limitations")},
                    "jobs": [{key: job[key] for key in ("id", "kind", "worker_id", "status", "attempts")} for job in jobs],
                    "evidence": evidence, "discovery": {"mode": discovery["mode"], "requests": discovery["requests"],
                                                           "items": len(discovery["items"]), "errors": discovery["errors"]},
                    "method_control": {"plugin_id": method["plugin_id"], "parameters": method["parameters"],
                        "provenance": method["provenance"], "metrics": method["result"]["metrics"], "interpretation": method["interpretation"]},
                    "claim": claim, "export_without_credentials_verified": True,
                    "directory_queries_requested": directories, "directory_results": directory_results,
                    "static_assets_http": static_assets, "browser_render_test": "not_performed",
                    "physical_multihost_test": "not_performed", "scientific_validation": "not_performed",
                }
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT if os.name == "posix" else signal.CTRL_BREAK_EVENT)
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5)
                    process.wait(timeout=5)
                if observed_pids is not None:
                    deadline = time.monotonic() + 3
                    while any(alive(pid) for pid in observed_pids) and time.monotonic() < deadline:
                        time.sleep(.1)
                    remaining = [pid for pid in observed_pids if alive(pid)]
                    if remaining and os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if remaining:
                        raise RuntimeError("Worker process cleanup failed")
                if proof is not None:
                    proof["process_cleanup"] = ("launcher_and_observed_workers_stopped" if observed_pids is not None
                                                else "launcher_stopped_worker_cleanup_delegated")
    assert proof is not None
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--directories", action="store_true", help="Also search Agentverse and Hugging Face model/dataset metadata; no external agents run")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.online, args.directories)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("passed", "online", "worker_registrations", "observed_worker_processes", "process_cleanup")}, ensure_ascii=False, indent=2))

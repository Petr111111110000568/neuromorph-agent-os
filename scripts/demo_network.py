"""Execute pinned simulation and offline discovery using TWO worker subprocesses.

No external services are contacted. Temporary coordinator/worker databases are
deleted afterward. A random bearer secret is passed only through environment.
"""
import json
import os
from pathlib import Path
import secrets
import selectors
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_demo():
    from workbench.network.queue import Queue
    processes = []
    with tempfile.TemporaryDirectory(prefix="meta-network-demo-") as temporary:
        state = Path(temporary)
        token = secrets.token_urlsafe(48)
        environment = dict(os.environ, META_HUB_TOKEN=token)
        hub_program = """
import json, sys
from workbench.network.queue import Queue
from workbench.network.transport import make_hub
q = Queue(sys.argv[1])
s = make_hub(q, port=0)
print(json.dumps({'port': s.server_address[1]}), flush=True)
try:
    s.serve_forever()
finally:
    s.server_close()
    q.close()
"""
        try:
            hub = subprocess.Popen([sys.executable, "-u", "-c", hub_program, str(state / "network.sqlite3")],
                cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(hub)
            # Avoid indefinitely blocking on an unsuccessful hub startup.
            deadline = time.monotonic() + 10
            if os.name == "posix":
                with selectors.DefaultSelector() as ready:
                    ready.register(hub.stdout, selectors.EVENT_READ)
                    if not ready.select(timeout=10):
                        raise RuntimeError("Hub startup timed out")
            elif hub.poll() is not None:
                raise RuntimeError("Hub exited before startup")
            line = hub.stdout.readline()
            if not line or time.monotonic() > deadline:
                raise RuntimeError("Hub startup failed")
            url = "http://127.0.0.1:" + str(json.loads(line)["port"])
            queue = Queue(state / "network.sqlite3")
            try:
                for index in range(2):
                    queue.submit("simulation", {"plugin_id": "quantum_circuit", "parameters": {"theta": 0.2 * index, "shots": 100, "seed": index + 1}})
                    queue.submit("discovery", {"query": "epigenetics simulation" if index else "quantum", "offline": True, "limit": 3})
                workers = []
                for index in (1, 2):
                    worker = subprocess.Popen([sys.executable, "-m", "workbench.network.worker",
                        "--hub", url, "--id", f"demo-worker-{index}", "--capabilities", "simulation,discovery",
                        "--max-jobs", "2", "--poll-seconds", "0.05", "--data-dir", str(state / f"worker-{index}")],
                        cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    processes.append(worker)
                    workers.append(worker)
                summaries = []
                for worker in workers:
                    out, err = worker.communicate(timeout=45)
                    if token in out or token in err:
                        raise RuntimeError("Credential appeared in worker output")
                    if worker.returncode != 0:
                        raise RuntimeError("A real worker failed")
                    summaries.append(json.loads(out))
                jobs = queue.jobs()
                if len(jobs) != 4 or any(job["status"] != "completed" for job in jobs):
                    raise RuntimeError("Not all jobs completed")
                if len({job["worker_id"] for job in jobs}) != 2:
                    raise RuntimeError("Expected two distinct job owners")
                simulations = [job for job in jobs if job["kind"] == "simulation"]
                discoveries = [job for job in jobs if job["kind"] == "discovery"]
                if any(not job["result"]["provenance"].get("output_sha256") for job in simulations):
                    raise RuntimeError("Missing actual plugin execution provenance")
                if any(job["result"]["mode"] != "offline_catalog" or job["result"]["requests"] != 0 for job in discoveries):
                    raise RuntimeError("Discovery did not stay offline")
                for file in state.rglob("*"):
                    if file.is_file() and token.encode() in file.read_bytes():
                        raise RuntimeError("Credential was persisted")
                return {"mode": "real_processes_loopback", "hub_pid": hub.pid,
                    "worker_pids": [worker.pid for worker in workers], "workers": summaries,
                    "jobs_completed": len(jobs), "unique_workers": 2,
                    "simulation_jobs": len(simulations), "offline_discovery_jobs": len(discoveries),
                    "builtin_subprocess_provenance": True, "token_persisted": False,
                    "external_requests": 0,
                    "limits": ["One computer; this verifies process/network boundaries, not multi-host TLS deployment.",
                               "Pinned synthetic CPU simulation; no biological validation."]}
            finally:
                queue.close()
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))

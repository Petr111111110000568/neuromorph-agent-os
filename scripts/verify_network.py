"""Real local launcher + HTTP campaign verification; --online contacts public APIs."""
import argparse
import http.client
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(online=False):
    with tempfile.TemporaryDirectory(prefix="meta-network-verify-") as folder:
        logs = Path(folder) / "process.log"
        with logs.open("w+") as stream:
            command = [sys.executable, "-m", "workbench", "--data-dir", str(Path(folder) / "state"), "local-network", "--port", "0", "--hub-port", "0", "--federation-port", "0", "--workers", "2"]
            process = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=stream, text=True)
            try:
                deadline = time.monotonic() + 15
                port = None
                while time.monotonic() < deadline:
                    found = re.search(r"(?:Meta-Harness|network): http://127\.0\.0\.1:(\d+)/(?:network|society|federation)\.html", logs.read_text())
                    if found:
                        port = int(found.group(1))
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Launcher exited before readiness: " + logs.read_text())
                    time.sleep(0.1)
                if port is None:
                    raise RuntimeError("Launcher readiness deadline exceeded")

                def request(path, payload=None):
                    client = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
                    data = None if payload is None else json.dumps(payload).encode()
                    client.request("GET" if payload is None else "POST", path, body=data, headers={} if data is None else {"Content-Type": "application/json"})
                    response = client.getresponse()
                    body, status = response.read(), response.status
                    mime = response.getheader("Content-Type", "")
                    client.close()
                    assert status == 200, (path, status, body[:300])
                    return json.loads(body) if "application/json" in mime else body

                # Wait for registration through the actual transport.
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    initial = request("/api/network")
                    if len(initial["workers"]) == 2:
                        break
                    time.sleep(0.1)
                assert len(initial["workers"]) == 2, initial
                for asset in ("/network.html", "/network.js", "/network.css"):
                    assert len(request(asset)) > 100
                question = "single cell epigenetics model validation" if online else "Эпигенетика: проверка качества вычислительных моделей"
                campaign = request("/api/campaign", {"question": question, "online": online, "simulate": True, "max_iterations": 1, "max_jobs": 2})
                deadline = time.monotonic() + 80
                while time.monotonic() < deadline:
                    snapshot = request("/api/network")
                    campaign = next(c for c in snapshot["campaigns"] if c["id"] == campaign["id"])
                    if campaign["status"] in ("completed", "failed", "budget_exhausted", "cancelled"):
                        break
                    if process.poll() is not None:
                        raise RuntimeError("Launcher terminated during campaign")
                    time.sleep(0.25)
                assert campaign["status"] == "completed", campaign
                assert campaign["iteration"] == 1
                jobs = snapshot["jobs"]
                assert len(jobs) == 2 and all(j["status"] == "completed" for j in jobs)
                search = next(j for j in jobs if j["kind"] == "discovery")
                simulation = next(j for j in jobs if j["kind"] == "simulation")
                assert search["result"]["items"]
                assert simulation["result"]["status"] == "completed"
                account = request("/api/accounts/request", {"provider": "europepmc", "reason": "public metadata discovery"})
                assert account["status"] == "no_registration_required"
                export = request("/api/network/export")
                assert '"lease_token"' not in json.dumps(export)
                return {"passed": True, "online": online, "independent_workers": len(initial["workers"]),
                    "campaign": {key: campaign[key] for key in ("id", "question", "status", "iteration", "max_jobs", "events")},
                    "resources_before": initial["resources_count"], "resources_after": snapshot["resources_count"],
                    "search": search["result"], "simulation": {"plugin_id": simulation["result"]["plugin_id"], "provenance": simulation["result"]["provenance"], "metrics": simulation["result"]["result"]["metrics"]},
                    "job_workers": [{"kind": j["kind"], "worker_id": j["worker_id"]} for j in jobs],
                    "public_api_registration": account["status"], "browser_render_test": "not_performed"}
            finally:
                # SIGINT asks the launcher to stop and reap its worker children.
                import signal
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--online", action="store_true", help="Send a public test query to Europe PMC and Crossref")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.online)
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("passed", "online", "independent_workers", "resources_before", "resources_after", "public_api_registration")}, ensure_ascii=False, indent=2))

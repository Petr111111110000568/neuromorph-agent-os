"""Real HTTP/process checks for the trusted-worker protocol and its boundaries."""
import http.client
import http.server
import importlib.util
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from workbench.network.queue import Queue
from workbench.network.transport import Client, MAX_BODY, TransportError, make_hub, validate_url
from workbench.network import worker

ROOT = Path(__file__).resolve().parents[1]


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.token = secrets.token_urlsafe(48)
        self.queue = Queue(self.directory / "network.sqlite3")
        self.hub = make_hub(self.queue, port=0, token=self.token)
        self.thread = threading.Thread(target=self.hub.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.hub.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.client = Client(self.url, self.token)

    def tearDown(self):
        self.hub.shutdown()
        self.hub.server_close()
        self.thread.join(timeout=2)
        self.queue.close()
        self.temp.cleanup()

    def raw(self, path="/v1/register", body=b"{}", headers=None, method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        base = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        if headers:
            base.update(headers)
        connection.request(method, path, body=body, headers=base)
        response = connection.getresponse()
        status, raw = response.status, response.read()
        connection.close()
        return status, raw

    def test_real_worker_runs_pinned_simulation_and_secret_is_not_persisted(self):
        job = self.queue.submit("simulation", {"plugin_id": "regression_benchmark", "parameters": {"seed": 7}})
        summary = worker.run_worker(self.url, self.token, "test-worker", ROOT,
                                    data_dir=self.directory / "worker", once=True)
        self.assertEqual(summary["completed"], 1)
        final = self.queue.get(job["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"]["status"], "completed")
        self.assertEqual(final["result"]["provenance"]["execution"], "isolated_python_subprocess")
        self.assertTrue(final["result"]["provenance"]["output_sha256"])
        for path in self.directory.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.token.encode(), path.read_bytes())
        self.assertNotIn("lease_token", json.dumps(self.queue.jobs()))

    def test_authentication_scope_and_browser_boundary(self):
        for path in ("/v1/health", "/v1/register"):
            status, raw = self.raw(path, headers={"Authorization": "Bearer wrong"}, method="GET" if path.endswith("health") else "POST")
            self.assertEqual(status, 401)
            self.assertNotIn(self.token.encode(), raw)
        self.assertEqual(self.raw(headers={"Origin": "https://untrusted.invalid"})[0], 403)
        self.assertEqual(self.raw("/v1/submit")[0], 404)
        self.assertEqual(self.raw("/v1/cancel")[0], 404)
        self.assertEqual(self.raw("/v1/register?extra=1")[0], 404)
        self.assertEqual(self.client.request("/v1/health")["status"], "ok")

    def test_strict_bounded_json_and_request_fields(self):
        for body in (b'{"worker_id":"one","worker_id":"two","capabilities":["simulation"]}',
                     b'{"worker_id":"one","capabilities":["simulation"],"limit":NaN}',
                     b'{"worker_id":"one","capabilities":["simulation"],"command":"rm"}', b'[]'):
            self.assertEqual(self.raw(body=body)[0], 400)
        self.assertEqual(self.raw(headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.raw(headers={"Content-Length": str(MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.raw(headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_tls_required_for_remote_and_url_credentials_rejected(self):
        for url in ("http://example.com", "http://127.0.0.1.evil.invalid", "https://u:p@example.com", "https://example.com/a", "https://example.com?key=secret", "https://example.com/#x", "ftp://localhost", "https://example.com\\@127.0.0.1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        self.assertEqual(validate_url("https://hub.example.org/"), "https://hub.example.org")
        self.assertEqual(validate_url("http://[::1]:8766/"), "http://[::1]:8766")
        with self.assertRaises(ValueError):
            make_hub(self.queue, host="0.0.0.0", port=0, token=self.token)
        with self.assertRaises(ValueError):
            Client(self.url, "weak")

    @unittest.skipUnless(shutil.which("openssl"), "openssl is only required for this TLS test")
    def test_tls_certificate_verification_and_explicit_private_ca(self):
        cert = self.directory / "cert.pem"
        key = self.directory / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True, timeout=15)
        server = make_hub(self.queue, port=0, token=self.token, certfile=cert, keyfile=key)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"https://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(TransportError):
                Client(url, self.token).request("/v1/health")
            self.assertEqual(Client(url, self.token, ca_file=cert).request("/v1/health")["status"], "ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_lease_ownership_and_idempotent_completion_over_http(self):
        for name in ("one", "two"):
            self.client.request("/v1/register", {"worker_id": name, "capabilities": ["simulation"]})
        submitted = self.queue.submit("simulation", {"plugin_id": "quantum_circuit"})
        job = self.client.request("/v1/claim", {"worker_id": "one"})
        owner = {"job_id": job["id"], "worker_id": "one", "lease_token": job["lease_token"]}
        self.assertEqual(self.client.request("/v1/heartbeat", owner)["status"], "running")
        with self.assertRaises(TransportError):
            self.client.request("/v1/finish", dict(owner, worker_id="two", result={"reported": True}))
        completed = self.client.request("/v1/finish", dict(owner, result={"reported": True}))
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.client.request("/v1/finish", dict(owner, result={"reported": True})), completed)
        self.assertNotIn(job["lease_token"], json.dumps(self.queue.get(submitted["id"])))

    def test_real_heartbeats_while_builtin_handler_runs(self):
        self.queue.submit("simulation", {"plugin_id": "quantum_circuit"})
        original = worker._execute
        def slow_real_handler(*args):
            time.sleep(0.10)
            return original(*args)
        with mock.patch.object(worker, "HEARTBEAT_SECONDS", 0.02), \
             mock.patch.object(worker, "_execute", slow_real_handler), \
             mock.patch.object(self.queue, "heartbeat", wraps=self.queue.heartbeat) as heartbeats:
            result = worker.run_worker(self.url, self.token, "heartbeat-worker", ROOT,
                                       data_dir=self.directory / "worker", once=True)
        self.assertEqual(result["completed"], 1)
        self.assertGreaterEqual(heartbeats.call_count, 2)

    def test_unknown_payload_cannot_execute_commands(self):
        self.queue.submit("simulation", {"plugin_id": "quantum_circuit", "command": "no arbitrary shell"}, max_attempts=1)
        result = worker.run_worker(self.url, self.token, "reject-worker", ROOT,
                                   data_dir=self.directory / "worker", once=True)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.queue.jobs()[0]["status"], "failed")

    def test_two_real_worker_processes(self):
        spec = importlib.util.spec_from_file_location("network_demo", ROOT / "scripts" / "demo_network.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.run_demo()
        self.assertEqual(result["jobs_completed"], 4)
        self.assertEqual(result["unique_workers"], 2)
        self.assertEqual(len(set(result["worker_pids"] + [result["hub_pid"]])), 3)
        self.assertFalse(result["token_persisted"])

    def test_redirects_are_never_followed_and_transient_retries_are_bounded(self):
        state = {"redirect_target_hits": 0, "retry_hits": 0}
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                if self.path == "/v1/health":
                    self.send_response(302)
                    self.send_header("Location", "/stolen")
                else:
                    state["redirect_target_hits"] += 1
                    self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def do_POST(self):
                state["retry_hits"] += 1
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(TransportError):
                Client(url, self.token).request("/v1/health")
            self.assertEqual(state["redirect_target_hits"], 0)
            with self.assertRaises(TransportError):
                worker.run_worker(url, self.token, "retry-worker", ROOT,
                                  data_dir=self.directory / "worker", once=True)
            self.assertEqual(state["retry_hits"], 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

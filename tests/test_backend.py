"""Integration checks for persistence, trust boundaries and local transports."""
import hashlib
import http.client
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from workbench.mcp_server import serve_stdio
from workbench.server import make_server
from workbench.service import Service, ServiceError, parse_json

ROOT = Path(__file__).resolve().parent.parent


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        self.service = Service(ROOT, self.db)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def test_source_persistence_and_unverified_provenance(self):
        source = self.service.add_source({"title": "New evidence", "url": "https://example.org/paper", "category": "methods", "summary": "Not yet assessed"})
        self.assertEqual(source["status"], "unverified")
        self.service.close()
        self.service = Service(ROOT, self.db)
        found = self.service.sources("New evidence")["items"]
        self.assertEqual(found[0]["id"], source["id"])
        self.assertTrue(self.service.audit()["valid"])

    def test_workflow_persists_review_computation_and_audit(self):
        outcome = self.service.workflow({"question": "Как проверить квантовую модель?", "plugin_id": "quantum_circuit", "parameters": {"shots": 100, "seed": 9}})
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(len(outcome["council"]["advisors"]), 8)
        self.assertTrue(all(a["kind"] == "rule_based" for a in outcome["council"]["advisors"]))
        self.assertEqual(outcome["run"]["result"]["model"]["qpu_used"], False)
        self.service.close()
        self.service = Service(ROOT, self.db)
        self.assertEqual(len(self.service.workflows()["items"]), 1)
        self.assertEqual(len(self.service.councils()["items"]), 1)
        self.assertEqual(len(self.service.runs()["items"]), 1)
        self.assertTrue(self.service.audit()["valid"])
        self.assertEqual(len(self.service.audit()["items"]), 3)
        self.assertIn("quantum_circuit", self.service.report())

    def test_pinned_subprocess_is_reproducible(self):
        for plugin in self.service.plugins()["items"]:
            body = {"plugin_id": plugin["id"], "parameters": {}}
            a, b = self.service.run(body), self.service.run(body)
            self.assertEqual(a["status"], "completed", a.get("error"))
            self.assertEqual(a["result"], b["result"])
            self.assertIn("plugin_worker.py", a["provenance"]["builtin_hashes"])
            self.assertEqual(a["parameters"], a["result"]["parameters"])
            self.assertEqual(a["provenance"]["input_sha256"], b["provenance"]["input_sha256"])
            self.assertEqual(a["provenance"]["output_sha256"], b["provenance"]["output_sha256"])

    def test_reject_invalid_parameters_and_urls(self):
        for parameters in ({"shots": True}, {"shots": 1}, {"shots": float("nan")}, {"unknown": 3}, []):
            with self.subTest(parameters=parameters), self.assertRaises(ServiceError):
                self.service.run({"plugin_id": "quantum_circuit", "parameters": parameters})
        for url in ("javascript:alert(1)", "file:///tmp/test", "https://user:pass@example.org", "https://", "https://example.org:bad"):
            with self.subTest(url=url), self.assertRaises(ServiceError):
                self.service.add_source({"title": "x", "url": url})
        with self.assertRaises(ServiceError):
            self.service.run({"plugin_id": "../../arbitrary.py", "parameters": {}})
        with self.assertRaises(ServiceError):
            self.service.council({"question": "x", "source_ids": ["missing"]})
        self.assertEqual(self.service.runs()["items"], [])

    def test_finite_json_enforced_at_parse_and_depth(self):
        for value in ('{"x": NaN}', '{"x": Infinity}', '{"x": 1e9999}', '[' * 32 + '0' + ']' * 32):
            with self.subTest(value=value), self.assertRaises(ServiceError):
                parse_json(value)

    def test_audit_detects_record_and_chain_tampering(self):
        self.service.add_source({"title": "Evidence", "url": "https://example.org"})
        self.assertTrue(self.service.audit()["valid"])
        with self.service.store.db:
            self.service.store.db.execute("UPDATE records SET payload=replace(payload, 'Evidence', 'Changed')")
        self.assertFalse(self.service.audit()["valid"])

    def test_missing_or_changed_pins_disable_execution(self):
        original = self.service.read_data
        with patch.object(self.service, "read_data", side_effect=lambda name, default: {} if name == "builtin_pins.json" else original(name, default)):
            with self.assertRaises(ServiceError) as context:
                self.service.run({"plugin_id": "quantum_circuit"})
            self.assertEqual(context.exception.code, "pins_missing")
        missing_init = original("builtin_pins.json", {})
        missing_init["files"].pop("workbench/__init__.py", None)
        with patch.object(self.service, "read_data", return_value=missing_init):
            with self.assertRaises(ServiceError) as context:
                self.service.run({"plugin_id": "quantum_circuit"})
            self.assertEqual(context.exception.code, "pins_missing")
        missing_morphogenesis = original("builtin_pins.json", {})
        missing_morphogenesis["files"].pop("workbench/morphogenesis.py", None)
        with patch.object(self.service, "read_data", return_value=missing_morphogenesis):
            with self.assertRaises(ServiceError) as context:
                self.service.run({"plugin_id": "structural_plasticity"})
            self.assertEqual(context.exception.code, "pins_missing")
        pins = original("builtin_pins.json", {})
        pins["files"]["plugin_worker.py"] = "0" * 64
        with patch.object(self.service, "read_data", return_value=pins):
            with self.assertRaises(ServiceError) as context:
                self.service.run({"plugin_id": "quantum_circuit"})
            self.assertEqual(context.exception.code, "integrity_failure")

    def test_worker_timeout_is_persisted_as_failure(self):
        root = Path(self.tmp.name) / "timeout-fixture"
        (root / "data").mkdir(parents=True)
        (root / "workbench").mkdir()
        shutil.copy(ROOT / "workbench/plugins.py", root / "workbench/plugins.py")
        shutil.copy(ROOT / "workbench/__init__.py", root / "workbench/__init__.py")
        shutil.copy(ROOT / "workbench/morphogenesis.py", root / "workbench/morphogenesis.py")
        shutil.copy(ROOT / "workbench/kan.py", root / "workbench/kan.py")
        shutil.copy(ROOT / "workbench/cortical.py", root / "workbench/cortical.py")
        (root / "plugin_worker.py").write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
        files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ("plugin_worker.py", "workbench/plugins.py", "workbench/__init__.py", "workbench/morphogenesis.py", "workbench/kan.py", "workbench/cortical.py")}
        (root / "data/builtin_pins.json").write_text(json.dumps({"files": files}))
        bounded = Service(root, root / "state.sqlite3", timeout=0.1)
        try:
            result = bounded.run({"plugin_id": "quantum_circuit", "parameters": {}})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "plugin_timeout")
            self.assertEqual(len(bounded.runs()["items"]), 1)
            self.assertTrue(bounded.audit()["valid"])
        finally:
            bounded.close()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(ROOT, Path(self.tmp.name) / "db.sqlite3")
        self.server = make_server(self.service, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.service.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = headers or {}
        if body is not None:
            hdrs = {"Content-Type": "application/json", **hdrs}
        client.request(method, path, body=body, headers=hdrs)
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        client.close()
        return result

    def test_loopback_only_and_valid_status(self):
        with self.assertRaises(ValueError):
            make_server(self.service, host="0.0.0.0", port=0)
        status, headers, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], "0.9.0")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_host_origin_and_traversal_guards(self):
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", "/api/status", headers=headers)[0], 403)
        for path in ("/%2e%2e/CONTRACT.md", "/..%5cCONTRACT.md", "/%00", "/api/nonexistent"):
            with self.subTest(path=path):
                self.assertIn(self.request("GET", path)[0], (400, 404))
        self.assertEqual(self.request("GET", "/api/status", headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)

    def test_http_mutation_validation_and_export(self):
        status, _, raw = self.request("POST", "/api/sources", json.dumps({"title": "HTTP source", "url": "https://example.org"}))
        self.assertEqual(status, 201)
        id = json.loads(raw)["id"]
        status, headers, raw = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(any(s["id"] == id for s in json.loads(raw)["sources"]))
        self.assertEqual(self.request("POST", "/api/run", '{"plugin_id":"quantum_circuit","parameters":{"theta":NaN}}')[0], 400)
        self.assertEqual(self.request("POST", "/api/run", "{}", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/run", "x" * 131073)[0], 413)


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(ROOT, Path(self.tmp.name) / "db.sqlite3")

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def frames(self):
        return [
          {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "tests", "version": "1"}}},
          {"jsonrpc": "2.0", "method": "notifications/initialized"},
          {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
          {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "run_plugin", "arguments": {"plugin_id": "quantum_circuit", "parameters": {"shots": 100}}}},
        ]

    def test_stdio_process_handshake_and_actual_plugin(self):
        request = "\n".join(json.dumps(item) for item in self.frames()) + "\n"
        result = subprocess.run([sys.executable, "-m", "workbench", "--data-dir", self.tmp.name, "mcp"],
            input=request, capture_output=True, text=True, cwd=ROOT, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertTrue(any(t["name"] == "run_plugin" for t in responses[1]["result"]["tools"]))
        self.assertFalse(responses[2]["result"]["isError"])
        payload = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "completed")

    def test_requires_handshake_and_recovers_invalid_frame(self):
        request = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\nnot-json\n' + "\n".join(json.dumps(item) for item in self.frames()) + "\n"
        output = io.StringIO()
        serve_stdio(self.service, io.StringIO(request), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertIn("error", responses[0])
        self.assertEqual(responses[1]["error"]["code"], -32700)
        self.assertEqual(responses[-1]["id"], 3)
        self.assertFalse(responses[-1]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()

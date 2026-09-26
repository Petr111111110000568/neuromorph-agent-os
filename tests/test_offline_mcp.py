"""Offline MCP boundary and CLI shutdown: finite fake runner, no model calls."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from workbench.__main__ import main
from workbench.mcp_server import OFFLINE_COUNCIL_TOOLS, PROTOCOL, serve_stdio
from workbench.offline_control import OfflineControl
from workbench.service import Service, ServiceError


ROOT = Path(__file__).resolve().parents[1]


class UTF8Buffer(io.StringIO):
    def reconfigure(self, **options):
        self.options = options


class OfflineMCPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.service = Service(ROOT, self.folder / "state" / "workbench.sqlite3")
        self.addCleanup(self.service.close)
        self.control = Mock(spec=["snapshot", "start", "stop", "close"])
        self.control.snapshot.return_value = {"running": False, "jobs": []}
        self.control.start.return_value = {"id": "job", "status": "reserved", "duplicate_suppressed": False}
        self.control.stop.return_value = {"stop_requested": True}
        self.service._offline = self.control
        self.request = {"question": "How can this hypothesis be falsified?", "request_id": "mcp-1",
                        "public_data_confirmed": True}

    @staticmethod
    def frames(methods, *, initialize=True):
        frames = []
        if initialize:
            frames.extend([
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ])
        for index, (method, params) in enumerate(methods, start=2):
            frames.append({"jsonrpc": "2.0", "id": index, "method": method, "params": params})
        return "".join(json.dumps(frame) + "\n" for frame in frames)

    def exchange(self, methods, *, allowed=OFFLINE_COUNCIL_TOOLS, initialize=True):
        output = io.StringIO()
        serve_stdio(self.service, io.StringIO(self.frames(methods, initialize=initialize)), output,
                    allowed_tools=allowed)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        return responses[1:] if initialize else responses

    def call(self, name, arguments):
        return self.exchange([("tools/call", {"name": name, "arguments": arguments})])[0]

    @staticmethod
    def payload(response):
        return json.loads(response["result"]["content"][0]["text"])

    def test_offline_discovery_has_exact_capabilities_without_starting_controller(self):
        self.service._offline = None
        tools = self.exchange([("tools/list", {})])[0]["result"]["tools"]
        self.assertIsNone(self.service._offline)
        self.assertEqual({tool["name"] for tool in tools}, OFFLINE_COUNCIL_TOOLS)
        for tool in tools:
            self.assertIn("Local-only", tool["description"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        start = next(tool for tool in tools if tool["name"] == "offline_council_start")["inputSchema"]
        self.assertEqual(set(start["required"]), set(self.request))
        self.assertEqual(start["properties"]["question"]["maxLength"], 2000)
        self.assertEqual(start["properties"]["public_data_confirmed"]["enum"], [True])

    def test_valid_calls_delegate_without_overrides(self):
        responses = self.exchange([
            ("tools/call", {"name": "offline_council_status", "arguments": {}}),
            ("tools/call", {"name": "offline_council_start", "arguments": self.request}),
            ("tools/call", {"name": "offline_council_stop", "arguments": {}}),
        ])
        self.control.snapshot.assert_called_once_with()
        self.control.start.assert_called_once_with(self.request)
        self.control.stop.assert_called_once_with()
        self.assertEqual(self.payload(responses[0]), {"running": False, "jobs": []})
        self.assertEqual(self.payload(responses[1])["status"], "reserved")
        self.assertEqual(self.payload(responses[2]), {"stop_requested": True})
        self.assertTrue(all(not response["result"]["isError"] for response in responses))

    def test_start_rejects_missing_extra_false_and_wrong_type_before_dispatch(self):
        cases = [{key: value for key, value in self.request.items() if key != missing}
                 for missing in self.request]
        cases.extend([
            {**self.request, "executable": "untrusted-command"},
            {**self.request, "model": "another-model"},
            {**self.request, "public_data_confirmed": False},
            {**self.request, "public_data_confirmed": 1},
            {**self.request, "public_data_confirmed": "true"},
            {**self.request, "question": ""},
            {**self.request, "question": "x" * 2001},
            {**self.request, "request_id": "x" * 81},
            {**self.request, "request_id": ""},
            {**self.request, "request_id": 7},
        ])
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertTrue(self.call("offline_council_start", arguments)["result"]["isError"])
        self.control.start.assert_not_called()

    def test_status_and_stop_reject_all_arguments(self):
        for name in ("offline_council_status", "offline_council_stop"):
            with self.subTest(name=name):
                self.assertTrue(self.call(name, {"request_id": "other"})["result"]["isError"])
        self.control.snapshot.assert_not_called()
        self.control.stop.assert_not_called()

    def test_controller_busy_and_missing_model_stay_tool_errors_without_retry(self):
        for code, status in (("offline_busy", 409), ("model_not_configured", 503)):
            with self.subTest(code=code):
                self.control.start.reset_mock()
                self.control.start.side_effect = ServiceError("Controlled refusal", code, status)
                result = self.call("offline_council_start", self.request)["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(result["content"], [{"type": "text", "text": "Controlled refusal"}])
                self.control.start.assert_called_once_with(self.request)

    def test_duplicate_receipt_is_returned_as_success_without_mcp_retry(self):
        self.control.start.return_value = {"id": "same", "status": "existing", "duplicate_suppressed": True}
        response = self.call("offline_council_start", self.request)
        self.assertFalse(response["result"]["isError"])
        self.assertTrue(self.payload(response)["duplicate_suppressed"])
        self.control.start.assert_called_once()

    def test_offline_allowlist_blocks_direct_legacy_tool_call(self):
        with patch.object(self.service, "sources", side_effect=AssertionError("Hidden capability called")) as sources:
            response = self.call("list_sources", {})
        self.assertEqual(response["error"]["code"], -32602)
        sources.assert_not_called()
        self.control.start.assert_not_called()

    def test_default_interface_retains_legacy_tools_and_empty_allowlist_exposes_none(self):
        default = self.exchange([("tools/list", {})], allowed=None)[0]["result"]["tools"]
        self.assertIn("list_sources", {tool["name"] for tool in default})
        self.assertTrue(OFFLINE_COUNCIL_TOOLS.issubset({tool["name"] for tool in default}))
        self.assertEqual(self.exchange([("tools/list", {})], allowed=frozenset())[0]["result"]["tools"], [])

    def test_invalid_allowlist_is_rejected_before_protocol(self):
        for allowed in ("offline_council_start", ["offline_council_start"], {"invented"}, {1}):
            with self.subTest(allowed=allowed), self.assertRaises(ValueError):
                self.exchange([], allowed=allowed)

    def test_initialize_is_required_before_local_start(self):
        response = self.exchange([("tools/call", {"name": "offline_council_start", "arguments": self.request})], initialize=False)[0]
        self.assertEqual(response["error"]["code"], -32602)
        self.control.start.assert_not_called()

    def test_cli_offline_only_enforces_discovery_and_closes_after_eof(self):
        output = UTF8Buffer()
        frames = self.frames([("tools/list", {}), ("tools/call", {"name": "list_sources", "arguments": {}})])
        with patch("workbench.__main__.Service", return_value=self.service), patch("sys.stdin", UTF8Buffer(frames)), patch("sys.stdout", output):
            status = main(["--data-dir", str(self.folder / "state"), "mcp", "--offline-only"])
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(status, 0)
        self.assertEqual({tool["name"] for tool in responses[1]["result"]["tools"]}, OFFLINE_COUNCIL_TOOLS)
        self.assertIn("error", responses[2])
        self.control.close.assert_called_once_with()

    def test_cli_eof_stops_and_joins_only_owned_fake_runner(self):
        model_root = self.folder / "fixture"
        manifest = model_root / "runtime" / "local-model" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
        entered = threading.Event()
        finished = threading.Event()

        def fake_runner(question, **options):
            entered.set()
            for _ in range(500):
                if options["stop_file"].exists():
                    finished.set()
                    return {"schema_version": 1, "status": "stopped", "external_model_calls": 0}
                finished.wait(0.01)
            raise AssertionError("CLI did not request own runner stop")

        controller = OfflineControl(model_root, self.folder / "state", runner=fake_runner)
        self.service._offline = controller
        output = UTF8Buffer()
        frames = self.frames([("tools/call", {"name": "offline_council_start", "arguments": self.request})])
        with patch("workbench.__main__.Service", return_value=self.service), patch("sys.stdin", UTF8Buffer(frames)), patch("sys.stdout", output), patch("workbench.offline_council.run_council", side_effect=AssertionError("Model call forbidden")):
            status = main(["mcp", "--offline-only"])
        self.assertEqual(status, 0)
        self.assertTrue(entered.is_set())
        self.assertTrue(finished.is_set())
        self.assertFalse(controller.thread.is_alive())
        self.assertEqual(controller.snapshot()["jobs"][0]["status"], "stopped")

    def test_launcher_pins_own_root_and_cannot_expand_tool_profile(self):
        spec = importlib.util.spec_from_file_location("offline_mcp_launcher", ROOT / "scripts" / "run_offline_mcp.py")
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        saved_path = list(sys.path)
        self.addCleanup(lambda: setattr(sys, "path", saved_path))
        with patch("workbench.__main__.main", return_value=0) as delegate:
            self.assertEqual(launcher.main(["--data-dir", str(self.folder / "state")]), 0)
        delegate.assert_called_once_with(["--data-dir", str(self.folder / "state"), "mcp", "--offline-only"])
        self.assertEqual(sys.path[0], str(ROOT))
        with patch("sys.stderr", io.StringIO()), patch("workbench.__main__.main") as delegate, self.assertRaises(SystemExit):
            launcher.main(["--data-dir", str(self.folder / "state"), "--model", "foreign"])
        delegate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

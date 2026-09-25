"""Pinned trusted subprocess tests; no paid provider or model-generated code runs."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from workbench.harnesses import HarnessRegistry
from workbench.harnesses import runner
from workbench.harnesses.registry import file_digest, tree_digest


def unreal_event(text="Protocol answer", stop="complete", phase="final_answer"):
    return {"Sequence": 3, "Kind": "model_response", "Data": {"Response": {
        "Stop": stop, "Failure": None, "Output": [{"Type": "message", "Data": {
            "Role": "assistant", "Text": text, "Phase": phase}}]}}}


def pi_events(text="Pi answer", settled=True):
    events = [{"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
               "content": [{"type": "text", "text": text}]}}, {"type": "agent_end"}]
    if settled:
        events.append({"type": "agent_settled"})
    return b"\n".join(json.dumps(event, ensure_ascii=False).encode() for event in events) + b"\n"


class ParsingTests(unittest.TestCase):
    def test_complete_unreal_event_and_unicode_separator(self):
        result = runner.parse_events(json.dumps(unreal_event("a\u2028b"), ensure_ascii=False).encode() + b"\n", "unreal_jsonl")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], "a\u2028b")
        self.assertEqual(result["model_responses"], 1)

    def test_refusal_truncation_commentary_and_tool_calls_not_completed(self):
        cases = [(unreal_event(stop="refused"), "incomplete_response"),
                 (unreal_event(stop="max_output_tokens"), "incomplete_response"),
                 (unreal_event(phase="commentary"), "empty_response"),
                 ({"Kind": "tool_call_status", "Data": {}}, "policy_violation")]
        tool = unreal_event()
        tool["Data"]["Response"]["Output"] = [{"Type": "tool_call", "Data": {"Name": "Bash"}}]
        cases.append((tool, "policy_violation"))
        for event, status in cases:
            with self.subTest(status=status):
                result = runner.parse_events(json.dumps(event).encode(), "unreal_jsonl")
                self.assertEqual(result["status"], status)
                self.assertEqual(result["output_text"], "")

    def test_pi_requires_settled_after_final_assistant(self):
        self.assertEqual(runner.parse_events(pi_events(), "pi_jsonl")["status"], "completed")
        self.assertEqual(runner.parse_events(pi_events(settled=False), "pi_jsonl")["status"], "empty_response")
        self.assertEqual(runner.parse_events(pi_events().replace(b'"stop"', b'"length"'), "pi_jsonl")["status"], "incomplete_response")

    def test_malformed_json_and_duplicate_keys_rejected(self):
        for raw in [b"not-json", b'{"Kind":"input","Kind":"model_response"}', b"[]", b"{}"]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                runner.parse_events(raw, "unreal_jsonl")


@unittest.skipUnless(os.name == "posix", "POSIX process groups are required")
class RegistryProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "runtime/harnesses").mkdir(parents=True)
        self.binary = self.root / "runtime/harnesses/fake-runner"
        self.config = {"schema_version": 1, "harnesses": [{
            "id": "unreal", "name": "Unreal fixture", "upstream_url": "https://example.org/unreal",
            "revision": "a" * 40, "adapter": "unreal_jsonl", "default_model": "test-model",
            "providers": ["openai"], "installation_status": "test_fixture",
            "binary": {"path": "runtime/harnesses/fake-runner", "sha256": None}}]}
        self.registry = HarnessRegistry(self.root)

    def write_binary(self, body):
        self.binary.write_text("#!" + sys.executable + "\n" + body)
        self.binary.chmod(0o755)
        self.config["harnesses"][0]["binary"]["sha256"] = file_digest(self.binary)
        (self.root / "config/harnesses.json").write_text(json.dumps(self.config))

    def run_fake_provider(self, *args, **kwargs):
        # These local fixture executables exercise runner behavior, not permission
        # to contact a provider. Public zero-spend gates are tested separately.
        with mock.patch("workbench.harnesses.registry.live_inference_block_reason", return_value=None):
            return self.registry.run(*args, **kwargs)

    def success_binary(self):
        self.write_binary("import json, os, sys\nrequest=json.load(sys.stdin)\n"
                          "assert request['disallowed_tools']==['Bash','ViewImage','SkillUse']\n"
                          "assert request['max_attempts']==1\n"
                          "assert 'isolated sandbox container' not in request['system_prompt']\n"
                          "assert not os.path.exists('.env')\n"
                          "assert not os.path.exists('.harness')\n"
                          "assert 'PRIVATE_TEST_SECRET' not in os.environ\n"
                          "assert 'HTTPS_PROXY' not in os.environ\n"
                          "assert 'HOME' not in os.environ\n"
                          "print(" + repr(json.dumps(unreal_event())) + ")\n")

    def test_real_process_default_gate_and_no_ambient_credentials(self):
        self.success_binary()
        (self.root / ".env").write_text("PRIVATE_TEST_SECRET=must-not-load")
        denied = self.run_fake_provider("unreal", "Public prompt", environment={"OPENAI_API_KEY": "test-token"})
        self.assertEqual(denied["status"], "blocked_model_calls_not_allowed")
        result = self.run_fake_provider("unreal", "Public prompt", allow_model_calls=True,
                                   environment={"OPENAI_API_KEY": "test-token", "PRIVATE_TEST_SECRET": "private", "HTTPS_PROXY": "https://proxy.invalid"})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], "Protocol answer")
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn("test-token", json.dumps(result))
        self.assertTrue(self.registry.status()["harnesses"][0]["live_authenticated"])

    def test_protocol_test_does_not_claim_live_authentication_and_hash_invalidates_receipt(self):
        self.success_binary()
        result = self.registry.run("unreal", "Public protocol fixture", protocol_test_base_url="http://127.0.0.1:9000/v1", environment={})
        self.assertEqual(result["status"], "completed")
        status = self.registry.status()["harnesses"][0]
        self.assertTrue(status["protocol_tested"])
        self.assertFalse(status["live_authenticated"])
        self.binary.write_text(self.binary.read_text() + "# altered\n")
        status = self.registry.status()["harnesses"][0]
        self.assertFalse(status["pin_verified"])
        self.assertFalse(status["protocol_tested"])
        self.assertEqual(self.registry.run("unreal", "Public prompt")["status"], "untrusted_or_unpinned_executable")

    def test_private_nonloopback_mock_endpoint_rejected_before_process(self):
        self.success_binary()
        for url in ["https://127.0.0.1:9000/v1", "http://localhost:9000/v1", "http://169.254.169.254:80/v1",
                    "http://example.org:9000/v1", "http://user:secret@127.0.0.1:9000/v1", "http://127.0.0.1:9000/v1?token=x"]:
            with self.subTest(url=url), mock.patch.object(runner, "capture") as capture, self.assertRaises(ValueError):
                self.registry.run("unreal", "Public prompt", protocol_test_base_url=url)
            capture.assert_not_called()

    def test_receipt_does_not_survive_repinned_runtime_or_interpreter(self):
        self.success_binary()
        self.registry.run("unreal", "Public protocol fixture", protocol_test_base_url="http://127.0.0.1:9000/v1", environment={})
        entry = self.config["harnesses"][0]
        inspection, _ = self.registry._inspect(entry)
        receipt_path = self.root / "runtime/harnesses/unreal.last-run.json"
        receipt = json.loads(receipt_path.read_text())
        receipt.update(interpreter_sha256="a" * 64, runtime_tree_sha256="b" * 64)
        receipt_path.write_text(json.dumps(receipt))
        inspection.update(interpreter_sha256="a" * 64, runtime_tree_sha256="b" * 64)
        self.assertTrue(self.registry._receipt(entry, inspection)["protocol_tested"])
        for key in ("interpreter_sha256", "runtime_tree_sha256"):
            changed = dict(inspection, **{key: "c" * 64})
            self.assertEqual(self.registry._receipt(entry, changed), {})

    def test_missing_key_and_symlink_pin_rejected(self):
        self.success_binary()
        self.assertEqual(self.run_fake_provider("unreal", "Public prompt", allow_model_calls=True, environment={})["status"], "blocked_provider_missing")
        original = self.binary.with_name("actual-runner")
        self.binary.rename(original)
        self.binary.symlink_to(original)
        self.assertEqual(self.registry.run("unreal", "Public prompt")["status"], "symlink_rejected")

    def test_output_limit_timeout_and_secret_error_not_returned(self):
        cases = [("import os\nos.write(1,b'x'*300000)\n", "output_limit"),
                 ("import time\ntime.sleep(5)\n", "timeout"),
                 ("import os,sys\nprint(os.environ['OPENAI_API_KEY'])\nsys.exit(1)\n", "credential_in_output_rejected")]
        for body, status in cases:
            with self.subTest(status=status):
                self.write_binary(body)
                result = self.run_fake_provider("unreal", "Public prompt", allow_model_calls=True, timeout_seconds=1,
                                           environment={"OPENAI_API_KEY": "private-test-token"})
                self.assertEqual(result["status"], status)
                self.assertEqual(result["output_text"], "")
                self.assertNotIn("private-test-token", json.dumps(result))

    def test_tool_event_stops_stream_before_timeout(self):
        self.write_binary("import time\nprint('{\"Kind\":\"tool_call_status\",\"Data\":{}}',flush=True)\ntime.sleep(5)\n")
        start = time.monotonic()
        result = self.registry.run("unreal", "Public prompt", protocol_test_base_url="http://127.0.0.1:9000/v1", timeout_seconds=4)
        self.assertEqual(result["status"], "policy_violation")
        self.assertLess(time.monotonic() - start, 2)

    def test_json_escaped_credential_echo_is_rejected_after_decoding(self):
        self.success_binary()
        token = "private-test-token"
        raw = json.dumps(unreal_event(token)).replace(token, "\\u0070rivate-test-token").encode()
        self.assertNotIn(token.encode(), raw)
        with mock.patch.object(runner, "capture", return_value={"status": "exited", "exit_code": 0,
                               "stdout": raw, "stderr": b""}):
            result = self.run_fake_provider("unreal", "Public prompt", allow_model_calls=True,
                                       environment={"OPENAI_API_KEY": token})
        self.assertEqual(result["status"], "credential_in_output_rejected")
        self.assertEqual(result["output_text"], "")
        self.assertNotIn(token, json.dumps(result))

    def test_process_group_descendant_is_stopped_on_timeout(self):
        pid_file = self.root / "child.pid"
        self.write_binary("import os,time\npid=os.fork()\nif pid==0:\n time.sleep(20)\nelse:\n"
                          " open(" + repr(str(pid_file)) + ",'w').write(str(pid))\n time.sleep(20)\n")
        result = self.registry.run("unreal", "Public prompt", protocol_test_base_url="http://127.0.0.1:9000/v1", timeout_seconds=1)
        self.assertEqual(result["status"], "timeout")
        child = int(pid_file.read_text())
        stat = Path(f"/proc/{child}/stat")
        deadline = time.monotonic() + 1
        while stat.exists() and stat.read_text().split()[2] not in {"Z", "X"} and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(not stat.exists() or stat.read_text().split()[2] in {"Z", "X"})

    def test_stdio_eof_before_process_exit_is_not_killed_early(self):
        self.write_binary("import os,time\nos.write(1," + repr((json.dumps(unreal_event()) + "\n").encode()) + ")\n"
                          "os.close(1)\nos.close(2)\ntime.sleep(0.2)\n")
        result = self.registry.run("unreal", "Public prompt", protocol_test_base_url="http://127.0.0.1:9000/v1")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)

    def test_pi_uses_stdin_not_attachment_syntax_and_disables_discovery(self):
        entry = {"adapter": "pi_jsonl", "providers": ["openai"], "default_model": "test-model"}
        with mock.patch.object(runner, "capture", return_value={"status": "exited", "exit_code": 0,
                               "stdout": pi_events(), "stderr": b""}) as capture:
            result = runner.run(entry, [Path(sys.executable), Path("/test/pi.js")], "@/etc/passwd",
                                allow_model_calls=True, environment={"OPENAI_API_KEY": "test-token"})
        args = capture.call_args.args
        self.assertNotIn("@/etc/passwd", args[0])
        self.assertEqual(args[1], b"@/etc/passwd")
        for flag in ["--no-session", "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-approve", "--offline"]:
            self.assertIn(flag, args[0])
        self.assertEqual(args[3]["PI_TELEMETRY"], "0")
        self.assertEqual(args[3]["PI_OFFLINE"], "1")
        self.assertEqual(result["status"], "completed")

    def test_runtime_tree_pin_detects_dependency_edit_and_external_symlink(self):
        tree = self.root / "pi"
        tree.mkdir()
        (tree / "dependency.js").write_text("trusted dependency")
        first = tree_digest(tree)
        (tree / "dependency.js").write_text("modified dependency")
        self.assertNotEqual(first, tree_digest(tree))
        (tree / "escape.js").symlink_to(Path(sys.executable))
        with self.assertRaises(ValueError):
            tree_digest(tree)

    def test_external_worker_capability_does_not_invoke_process(self):
        entry = copy.deepcopy(self.config["harnesses"][0])
        entry.update(id="openhands", adapter="external_worker_required", binary=None, providers=[])
        self.config["harnesses"] = [entry]
        (self.root / "config/harnesses.json").write_text(json.dumps(self.config))
        with mock.patch.object(runner, "capture") as capture:
            result = self.registry.run("openhands", "Public prompt")
        self.assertEqual(result["status"], "external_worker_required")
        capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Zero-spend public-entrypoint checks; no external requests or model runs."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from workbench.harnesses import HarnessRegistry
from workbench.harnesses import runner
from workbench.mcp_server import serve_stdio
from workbench.resource_policy import load_policy, live_inference_block_reason
from workbench.service import Service

ZERO_SPEND = {
    "schema_version": 1,
    "daily_spend_limit_usd": 0,
    "paid_model_calls_allowed": False,
    "public_catalog_reads_allowed": True,
}


class _PolicyFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.path = self.root / "config/resource_policy.json"

    def write_policy(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")


class ResourcePolicyTests(_PolicyFixture, unittest.TestCase):
    def test_missing_file_defaults_to_zero_and_cannot_mutate_next_read(self):
        policy = load_policy(self.root)
        self.assertEqual(policy, ZERO_SPEND)
        policy["paid_model_calls_allowed"] = True
        self.assertEqual(load_policy(self.root), ZERO_SPEND)
        self.assertEqual(live_inference_block_reason(self.root), "blocked_zero_spend_policy")

    def test_explicit_policy_permits_catalog_reads_but_no_live_inference(self):
        self.write_policy(dict(ZERO_SPEND, daily_spend_limit_usd=0.0))
        self.assertEqual(load_policy(self.root), ZERO_SPEND)
        self.assertEqual(live_inference_block_reason(self.root), "blocked_zero_spend_policy")

    def test_invalid_unknown_and_unsupported_budget_values_rejected(self):
        cases = []
        for field, values in {
            "schema_version": (True, 1.0, 2, "1", None),
            "daily_spend_limit_usd": (True, "0", -1, 0.01, float("nan"), float("inf")),
            "paid_model_calls_allowed": (True, 0, None),
            "public_catalog_reads_allowed": (False, 1, None),
        }.items():
            cases.extend(dict(ZERO_SPEND, **{field: value}) for value in values)
        missing = dict(ZERO_SPEND)
        del missing["daily_spend_limit_usd"]
        cases.extend((missing, dict(ZERO_SPEND, allow_free_model_calls=True), [], None))
        for index, value in enumerate(cases):
            with self.subTest(case=index):
                self.write_policy(value)
                with self.assertRaises(ValueError):
                    load_policy(self.root)

    def test_malformed_duplicate_and_oversized_policy_rejected(self):
        duplicate = json.dumps(ZERO_SPEND).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
        for raw in ("{", duplicate, " " * 4097):
            with self.subTest(size=len(raw)):
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ValueError):
                    live_inference_block_reason(self.root)

    def test_changed_configuration_is_revalidated(self):
        self.write_policy(ZERO_SPEND)
        self.assertEqual(live_inference_block_reason(self.root), "blocked_zero_spend_policy")
        self.write_policy(dict(ZERO_SPEND, daily_spend_limit_usd=1))
        with self.assertRaises(ValueError):
            live_inference_block_reason(self.root)


class PublicInferenceBoundaryTests(_PolicyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        entry = {
            "id": "unreal", "name": "Policy fixture", "upstream_url": "https://example.invalid/unreal",
            "revision": "a" * 40, "adapter": "unreal_jsonl", "default_model": "fixture",
            "providers": ["openai", "anthropic"], "installation_status": "fixture",
            "binary": None,
        }
        (self.root / "config/harnesses.json").write_text(
            json.dumps({"schema_version": 1, "harnesses": [entry]}), encoding="utf-8")
        self.registry = HarnessRegistry(self.root)
        # A verified available executable must still be denied before the runner.
        inspection = {"detected": True, "pin_verified": True, "build_status": "pinned_executable",
                      "executable_sha256": "b" * 64}
        self.inspect = mock.patch.object(HarnessRegistry, "_inspect",
                                        return_value=(inspection, self.root / "never-executed"))
        self.inspect.start()
        self.addCleanup(self.inspect.stop)

    def test_registry_blocks_keys_and_explicit_permission_before_runner(self):
        for explicit_policy in (False, True):
            if explicit_policy:
                self.write_policy(ZERO_SPEND)
            for provider, key in (("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")):
                with self.subTest(explicit=explicit_policy, provider=provider), mock.patch.object(runner, "run") as run:
                    result = self.registry.run(
                        "unreal", "Public task", allow_model_calls=True, provider=provider,
                        environment={key: "credential-must-not-be-used", "AUTONOMY_ALLOW_MODEL_CALLS": "true"})
                    self.assertEqual(result["status"], "blocked_zero_spend_policy")
                    self.assertEqual(result["counts"]["model_responses"], 0)
                    self.assertNotIn("credential-must-not-be-used", json.dumps(result))
                    run.assert_not_called()

    def test_invalid_policy_cannot_fall_through_to_runner(self):
        self.write_policy(dict(ZERO_SPEND, paid_model_calls_allowed=True))
        with mock.patch.object(runner, "run") as run, self.assertRaises(ValueError):
            self.registry.run("unreal", "Public task", allow_model_calls=True)
        run.assert_not_called()

    def test_local_protocol_mock_is_still_available(self):
        self.write_policy(ZERO_SPEND)
        fixture = {"status": "empty_response", "mode": "protocol_test", "output_text": ""}
        with mock.patch.object(runner, "run", return_value=fixture) as run:
            result = self.registry.run("unreal", "Local fixture",
                                       protocol_test_base_url="http://127.0.0.1:9000/v1")
        self.assertEqual(result["mode"], "protocol_test")
        self.assertEqual(run.call_args.kwargs["protocol_test_base_url"], "http://127.0.0.1:9000/v1")
        self.assertFalse(run.call_args.kwargs["allow_model_calls"])

    def test_service_status_reports_effective_disabled_despite_environment_flag(self):
        service = Service(root=self.root, db_path=":memory:")
        self.addCleanup(service.close)
        with mock.patch.dict(os.environ, {"AUTONOMY_ALLOW_MODEL_CALLS": "true",
                                         "OPENAI_API_KEY": "do-not-disclose"}):
            status = service.harnesses_status()
            self.assertTrue(status["model_calls_requested"])
            self.assertFalse(status["model_calls_enabled"])
            self.assertEqual(status["resource_policy"], ZERO_SPEND)
            self.assertEqual(status["model_calls_block_reason"], "blocked_zero_spend_policy")
            self.assertFalse(service.status()["model_calls_enabled"])
            self.assertNotIn("do-not-disclose", json.dumps(status))

    def test_service_and_mcp_use_the_same_gate_and_save_blocked_result(self):
        service = Service(root=self.root, db_path=":memory:")
        self.addCleanup(service.close)
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "run_harness_task",
                        "arguments": {"harness_id": "unreal", "prompt": "MCP task"}}},
        ]
        with mock.patch.dict(os.environ, {"AUTONOMY_ALLOW_MODEL_CALLS": "true",
                                         "OPENAI_API_KEY": "do-not-use"}), mock.patch.object(runner, "run") as run:
            result = service.harnesses_run({"harness_id": "unreal", "prompt": "Service task"})
            self.assertEqual(result["status"], "blocked_zero_spend_policy")
            out = io.StringIO()
            serve_stdio(service, io.StringIO("\n".join(json.dumps(f) for f in frames) + "\n"), out)
            replies = [json.loads(line) for line in out.getvalue().splitlines()]
            result = json.loads(replies[-1]["result"]["content"][0]["text"])
            self.assertEqual(result["status"], "blocked_zero_spend_policy")
            self.assertEqual(len(service.harnesses_runs()["items"]), 2)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()


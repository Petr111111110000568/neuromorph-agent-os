"""Offline scheduler checks: no public Space, account, or network requests."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbench.autonomy import cloud_review as cloud
from workbench.autonomy.daemon import _job_lock


class Clock:
    def __init__(self):
        self.wall = 1790294400.0
        self.steady = 10.0
        self.sleeps = []
        self.on_sleep = None

    def time(self):
        return self.wall

    def monotonic(self):
        return self.steady

    def sleep(self, seconds):
        self.wall += seconds
        self.steady += seconds
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep()

    def advance(self, seconds):
        self.wall += seconds
        self.steady += seconds


class CloudReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.intake = self.root / "runtime" / "continuous-discovery" / "cycle.json"
        self.intake.parent.mkdir(parents=True)
        self.output = self.root / "reviews"
        self.output.mkdir()
        self.state = self.output / "state.json"
        self.stop = self.output / "STOP"
        self.card = {"id": "catalog:huggingface_models:org/research", "name": "Research example",
            "source_url": "https://huggingface.co/org/research", "provenance": {"provider": "huggingface_models"},
            "verification_scope": "public_catalog_metadata_only"}
        self.write_intake([self.card])
        self.clock = Clock()
        self.runner = Mock(return_value={"status": "response_received", "text": "Непроверенное предложение"})

    def write_intake(self, cards):
        self.intake.write_text(json.dumps({"private": "PRIVATE-ROOT", "discovery": {"candidates": cards}}), encoding="utf-8")

    def run_job(self, **overrides):
        options = dict(interval=21600, max_runs=3, root=self.root, runner=self.runner,
            clock=self.clock.time, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        options.update(overrides)
        return cloud.run_cloud(self.intake, self.output, self.state, self.stop, **options)

    def checkpoint(self):
        return json.loads(self.state.read_text())

    def test_reserve_before_call_resume_and_finite_limit(self):
        def inspect_reservation(prompt):
            checkpoint = self.checkpoint()
            self.assertEqual(checkpoint["last_status"], "running")
            self.assertGreaterEqual(checkpoint["next_due"], self.clock.wall + 21600)
            return {"status": "response_received", "text": "Unverified data"}
        self.runner.side_effect = inspect_reservation
        first = self.run_job(once=True)
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["next_due"], self.clock.wall + 21600)
        self.assertEqual(self.run_job(once=True)["status"], "not_due")
        self.assertEqual(self.runner.call_count, 1)
        result = self.run_job()
        self.assertEqual(result["status"], "limit_reached")
        self.assertEqual(self.runner.call_count, 3)
        self.assertEqual(sum(self.clock.sleeps), 43200)
        self.assertTrue(all(0 < seconds <= 5 for seconds in self.clock.sleeps))
        self.assertEqual(self.run_job()["status"], "limit_reached")
        self.assertEqual(self.runner.call_count, 3)

    def test_malicious_private_fields_omitted_and_output_never_executed(self):
        item = dict(self.card, credentials="PRIVATE-KEY", private_path="PRIVATE-PATH", instructions="EXECUTE-SHELL")
        item["provenance"] = dict(item["provenance"], auth_token="PRIVATE-TOKEN")
        self.write_intake([item, dict(self.card, data_class="internal", name="PRIVATE-NAME"),
            dict(self.card, source_url="https://127.0.0.1/secret"),
            dict(self.card, source_url="https://huggingface.co/org/research?token=PRIVATE-URL")])
        marker = self.root / "unexpected-execution"
        malicious = f"__import__('pathlib').Path({str(marker)!r}).touch()"
        self.runner.return_value = {"status": "response_received", "text": malicious, "headers": "PRIVATE-HEADERS"}
        self.run_job(once=True)
        prompt = self.runner.call_args.args[0]
        self.assertIn("untrusted metadata", prompt)
        self.assertNotIn("PRIVATE", prompt)
        self.assertNotIn("EXECUTE-SHELL", prompt)
        self.assertLessEqual(len(prompt), 4000)
        record = json.loads((self.output / "cloud-001.json").read_text(encoding="utf-8"))
        self.assertFalse(record["validated"])
        self.assertFalse(record["execution_allowed"])
        self.assertEqual(record["text"], malicious)
        self.assertNotIn("PRIVATE", json.dumps(record))
        self.assertEqual(record["input_sha256"], hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        self.assertEqual(len(record["source_metadata"]), 1)
        self.assertFalse(marker.exists())

    def test_record_lists_only_the_cards_actually_sent(self):
        cards = [dict(self.card, id=f"catalog:huggingface_models:org/{index}", name="я" * 240) for index in range(12)]
        self.write_intake(cards)
        self.run_job(once=True)
        prompt = self.runner.call_args.args[0]
        actual = json.loads(prompt.split("BEGIN UNTRUSTED PUBLIC CARDS\n", 1)[1].split("\nEND UNTRUSTED", 1)[0])
        record = json.loads((self.output / "cloud-001.json").read_text(encoding="utf-8"))
        self.assertEqual(record["source_metadata"], actual)
        self.assertLess(len(actual), len(cards))
        self.assertLessEqual(len(prompt), 4000)

    def test_bounded_adapter_provenance_survives_without_private_extras(self):
        def observed(prompt):
            return {'status': 'response_received', 'text': 'Observed answer',
                    'model': cloud.PROVIDER_CONTRACT['model'],
                    'space_revision': cloud.PROVIDER_CONTRACT['revision'],
                    'config_sha256': 'c' * 64, 'event_id': 'e' * 32,
                    'requests': 1, 'request_count': 1, 'http_requests': 7,
                    'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                    'answer_sha256': hashlib.sha256(b'Observed answer').hexdigest(),
                    'unverified': True, 'web_search': False, 'thinking_budget': 1024,
                    'headers': {'Authorization': 'PRIVATE'}, 'raw_body': 'PRIVATE'}
        self.runner.side_effect = observed
        self.run_job(once=True)
        raw = (self.output / 'cloud-001.json').read_text()
        record = json.loads(raw)
        self.assertNotIn('PRIVATE', raw)
        self.assertEqual(record['provider_contract'], cloud.PROVIDER_CONTRACT)
        self.assertEqual(record['adapter_provenance']['config_sha256'], 'c' * 64)
        self.assertEqual(record['adapter_provenance']['event_id'], 'e' * 32)
        self.assertEqual(record['adapter_provenance']['request_count'], 1)
        self.assertEqual(record['answer_sha256'], hashlib.sha256(b'Observed answer').hexdigest())

    def test_inconsistent_adapter_provenance_is_not_saved_as_success(self):
        self.runner.return_value = {'status': 'response_received', 'text': 'Answer',
                                    'answer_sha256': '0' * 64}
        self.assertEqual(self.run_job(once=True)['status'], 'failed')
        self.assertFalse((self.output / 'cloud-001.json').exists())
        self.assertEqual(self.checkpoint()['attempts'], 1)
        with self.assertRaises(ValueError):
            cloud._adapter_provenance({'request_count': True})
        with self.assertRaises(ValueError):
            cloud._adapter_provenance({'event_id': 'PRIVATE' * 1000})

    def test_429_cooldown_survives_restart(self):
        self.runner.return_value = {"status": "rate_limited", "http_status": 429}
        result = self.run_job(once=True)
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["next_due"], self.clock.wall + 86400)
        self.clock.advance(21600)
        self.assertEqual(self.run_job(once=True)["status"], "not_due")
        self.clock.advance(64799)
        self.assertEqual(self.run_job(once=True)["status"], "not_due")
        self.assertEqual(self.runner.call_count, 1)
        self.clock.advance(1)
        self.runner.return_value = {"status": "response_received", "text": "Next explicit due attempt"}
        self.assertEqual(self.run_job(once=True)["attempts"], 2)
        self.assertEqual(self.runner.call_count, 2)

    def test_429_cooldown_is_saved_even_if_artifact_write_fails(self):
        self.runner.return_value = {"status": "rate_limited"}
        with patch.object(cloud, "_save_proposal", side_effect=OSError("PRIVATE-STORAGE-ERROR")):
            self.assertEqual(self.run_job(once=True)["status"], "failed")
        self.assertGreaterEqual(self.checkpoint()["next_due"], self.clock.wall + 86400)
        self.clock.advance(21600)
        self.assertEqual(self.run_job(once=True)["status"], "not_due")
        self.assertEqual(self.runner.call_count, 1)
        self.assertNotIn("PRIVATE", self.state.read_text())

    def test_other_provider_errors_stop_without_retry(self):
        self.runner.return_value = {"status": "access_denied", "message": "PRIVATE-RESPONSE"}
        result = self.run_job()
        self.assertEqual(result["status"], "access_denied")
        self.assertEqual(result["attempts"], 1)
        self.runner.assert_called_once()
        self.assertEqual(self.clock.sleeps, [])
        record = (self.output / "cloud-001.json").read_text()
        self.assertNotIn("PRIVATE", record)

    def test_dependency_and_unexpected_exception_stop_without_details(self):
        for error, status in ((ImportError("PRIVATE-MODULE"), "dependency_unavailable"),
                              (RuntimeError("PRIVATE-ERROR"), "failed")):
            with self.subTest(status=status):
                self.runner.side_effect = error
                result = self.run_job(once=True)
                self.assertEqual(result["status"], status)
                self.assertNotIn("PRIVATE", self.state.read_text())
                self.clock.advance(86400)
        self.assertEqual(self.runner.call_count, 2)
        self.assertEqual(self.checkpoint()["attempts"], 2)

    def test_crash_reserved_attempt_is_not_replayed(self):
        self.runner.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_job(once=True)
        self.assertEqual(self.checkpoint()["last_status"], "running")
        self.assertEqual(self.checkpoint()["attempts"], 1)
        self.runner.side_effect = None
        self.assertEqual(self.run_job(once=True)["status"], "not_due")
        self.assertEqual(self.checkpoint()["last_status"], "interrupted")
        self.assertEqual(self.runner.call_count, 1)

    def test_stop_before_post_and_during_wait(self):
        self.stop.touch()
        self.assertEqual(self.run_job()["status"], "stopped")
        self.runner.assert_not_called()
        self.stop.unlink()
        self.clock.on_sleep = self.stop.touch
        result = self.run_job()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.clock.sleeps, [5])

    def test_changed_identity_rejected_without_overwriting_state(self):
        self.run_job(once=True)
        original = self.state.read_bytes()
        for options in ({"max_runs": 4}, {"interval": 43200}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "changed"):
                self.run_job(**options)
        with patch.object(cloud, "TASKS", ("A changed approved task",)):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.run_job()
        self.assertEqual(self.state.read_bytes(), original)
        self.runner.assert_called_once()

    def test_invalid_limits_policy_and_overlap_before_network(self):
        for options in ({"interval": 21599}, {"interval": 86401}, {"max_runs": 29},
                        {"max_runs": 0}, {"max_runs": True}, {"once": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_job(**options)
        for stop in (self.state, self.intake, self.output / "cloud-001.json"):
            with self.subTest(path=stop.name), self.assertRaisesRegex(ValueError, "overlap"):
                cloud.run_cloud(self.intake, self.output, self.state, stop, root=self.root, runner=self.runner)
        (self.root / "config").mkdir()
        (self.root / "config" / "resource_policy.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.run_job()
        self.runner.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_fixed_intake_and_two_os_locks(self):
        for path in (Path(str(self.state) + ".lock"), self.output / ".cloud-review.lock"):
            with self.subTest(lock=path.name), _job_lock(path):
                with self.assertRaisesRegex(ValueError, "already running"):
                    self.run_job(once=True)
        other = self.root / "private.json"
        other.write_text(self.intake.read_text())
        with self.assertRaisesRegex(ValueError, "public discovery"):
            cloud.run_cloud(other, self.output, self.state, self.stop, root=self.root, runner=self.runner)
        self.runner.assert_not_called()

    def test_empty_metadata_and_bad_output_are_not_successes(self):
        self.write_intake([])
        self.assertEqual(self.run_job(once=True)["status"], "no_public_metadata")
        self.runner.assert_not_called()
        self.clock.advance(21600)
        self.write_intake([self.card])
        self.runner.return_value = {"status": "response_received", "text": "x" * (32 * 1024 + 1)}
        self.assertEqual(self.run_job(once=True)["status"], "failed")
        self.assertFalse((self.output / "cloud-002.json").exists())
        self.assertEqual(self.checkpoint()["attempts"], 2)

    def test_corrupt_checkpoint_rejected_without_replacement(self):
        self.run_job(once=True)
        checkpoint = self.checkpoint()
        for name, value in (("attempts", True), ("next_due", -1), ("last_status", []), ("schema_version", True)):
            with self.subTest(name=name):
                corrupted = dict(checkpoint, **{name: value})
                self.state.write_text(json.dumps(corrupted))
                before = self.state.read_bytes()
                with self.assertRaises(ValueError):
                    self.run_job(once=True)
                self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.runner.call_count, 1)

    def test_changed_adapter_contract_is_blocked_before_transport(self):
        from workbench.autonomy import qwen_space
        with patch.object(qwen_space, "REVISION", "changed-contract"), \
                patch.object(qwen_space, "call_qwen_space") as adapter:
            self.assertEqual(cloud._call("Public metadata only")["status"], "contract_changed")
            adapter.assert_not_called()

    def test_cli_defaults_and_keyboard_interrupt(self):
        args = ["--intake", str(self.intake), "--output-dir", str(self.output),
                "--state-file", str(self.state), "--stop-file", str(self.stop), "--once"]
        with patch.object(cloud, "run_cloud", return_value={"status": "not_due"}) as run, patch("builtins.print"):
            self.assertEqual(cloud.main(args), 0)
            self.assertEqual(run.call_args.kwargs["interval"], 21600)
            self.assertEqual(run.call_args.kwargs["max_runs"], 28)
        with patch.object(cloud, "run_cloud", side_effect=KeyboardInterrupt), patch("sys.stderr"):
            self.assertEqual(cloud.main(args), 130)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import subprocess

_spec = importlib.util.spec_from_file_location("council_health", Path(__file__).resolve().parents[1] / "scripts/council_health.py")
health = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(health)


class CouncilHealthTests(unittest.TestCase):
    def state(self, **updates):
        value = dict(attempts=1, successes=1, next_due=100000, provider_blocked=False,
                     pending=None, phase=1, failures=0,
                     journal=[dict(attempt=1, at=90000, status="response_received")])
        value.update(updates)
        return json.dumps(value).encode()

    def test_valid_history_is_waiting_not_guaranteed_live(self):
        result = health.assess(self.state(), 100100)
        self.assertEqual(result["status"], "scheduled_or_waiting")
        self.assertFalse(result["model_call_performed"])
        self.assertFalse(result["code_executed_from_ledger"])

    def test_pending_is_degraded_without_echoing_untrusted_values(self):
        result = health.assess(self.state(pending={"untrusted": "do not echo"}), 100100)
        self.assertEqual(result["reasons"], ["unfinished_reservation"])
        self.assertNotIn("do not echo", json.dumps(result))

    def test_provider_block_is_visible(self):
        self.assertIn("provider_blocked", health.assess(self.state(provider_blocked=True), 100100)["reasons"])

    def test_provider_cooldown_does_not_hide_latest_failure(self):
        result = health.assess(self.state(attempts=2, failures=1, next_due=180000,
            journal=[dict(attempt=2, at=100000, status="rate_limited")]), 100100)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["last_outcome"], "rate_limited")
        self.assertIn("last_attempt_failed_or_interrupted", result["reasons"])

    def test_arbitrarily_distant_next_due_cannot_claim_waiting(self):
        result = health.assess(self.state(next_due=100100 + 8 * 86400), 100100)
        self.assertEqual(result["status"], "degraded")
        self.assertIn("next_due_beyond_max_cooldown", result["reasons"])

    def test_untrusted_outcome_missing_history_or_future_event_fail_closed(self):
        for journal in ([], [dict(attempt=1, at=90000, status="secret-do-not-echo")],
                        [dict(attempt=1, at=200000, status="response_received")]):
            result = health.assess(self.state(journal=journal), 100100)
            self.assertEqual(result["reasons"], ["invalid_or_missing_ledger"])
            self.assertNotIn("secret-do-not-echo", json.dumps(result))

    def test_oversized_git_blob_is_rejected_before_blob_read(self):
        replies = [subprocess.CompletedProcess([], 0, b"blob\n", b""),
                   subprocess.CompletedProcess([], 0, b"65537\n", b"")]
        with patch.object(health.subprocess, "run", side_effect=replies) as run:
            with self.assertRaises(ValueError):
                health.git_read("ledger", "a" * 40 + ":" + health.STATE_PATH, 65536)
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all("-p" not in call.args[0] for call in run.call_args_list))

    def test_git_read_uses_blob_type_size_and_clean_environment(self):
        raw = self.state()
        replies = [subprocess.CompletedProcess([], 0, b"blob\n", b""),
                   subprocess.CompletedProcess([], 0, str(len(raw)).encode() + b"\n", b""),
                   subprocess.CompletedProcess([], 0, raw, b"")]
        with patch.dict(health.os.environ, {"GITHUB_TOKEN": "secret-do-not-echo"}), \
                patch.object(health.subprocess, "run", side_effect=replies) as run:
            self.assertEqual(health.git_read("ledger", "a" * 40 + ":" + health.STATE_PATH, 65536), raw)
            self.assertTrue(all("GITHUB_TOKEN" not in call.kwargs["env"] for call in run.call_args_list))

    def test_overdue_and_never_successful_are_reported(self):
        result = health.assess(self.state(successes=0), 200000)
        self.assertIn("overdue_more_than_18h", result["reasons"])
        self.assertIn("no_successful_model_response", result["reasons"])

    def test_invalid_data_fails_closed(self):
        for raw in (b"null", b"{", b"{}", b"x" * 65537, self.state(attempts=True),
                    self.state(successes=9), self.state(pending="bad"), self.state(failures=True),
                    self.state(next_due=10**12), b'{"attempts":1,"attempts":2}'):
            with self.subTest(raw=raw[:60]):
                self.assertEqual(health.assess(raw, 100100)["reasons"], ["invalid_or_missing_ledger"])


if __name__ == "__main__":
    unittest.main()


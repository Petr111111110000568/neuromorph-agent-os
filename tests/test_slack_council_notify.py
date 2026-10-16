"""No-network tests for optional Slack council cloud status reporting."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "slack_council_notify", Path(__file__).resolve().parents[1] / "scripts/slack_council_notify.py"
)
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)

FAKE_HOOK = "https://hooks.slack.com/services/T/B/test-only-token"


class SlackCouncilNotifyTests(unittest.TestCase):
    def test_no_secret_is_safe_no_network(self):
        with patch.dict(notify.os.environ, {"SLACK_WEBHOOK_URL": ""}), \
                patch.object(notify.urllib.request, "urlopen") as network, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(notify.main(["--report", "/missing/receipt.json"]), 0)
            network.assert_not_called()
            self.assertIn("skipped", output.getvalue())

    def test_only_slack_https_webhooks_are_allowed(self):
        self.assertTrue(notify.valid_webhook(FAKE_HOOK))
        self.assertTrue(notify.valid_webhook("https://hooks.slack-gov.com/services/T/B/X"))
        for candidate in ("http://hooks.slack.com/services/T/B/X",
                          "https://hooks.slack.com.evil.test/services/T/B/X",
                          "https://localhost/services/T/B/X",
                          "https://hooks.slack.com:8443/services/T/B/X",
                          "https://user@hooks.slack.com/services/T/B/X",
                          "https://hooks.slack.com/services/T/B/X?secret=1",
                          "", None):
            with self.subTest(candidate=candidate):
                self.assertFalse(notify.valid_webhook(candidate))

    def test_payload_discloses_no_untrusted_ledger_data(self):
        receipt = {
            "status": "degraded",
            "reasons": ["provider_blocked", "private-lab-secret", "provider_blocked"],
            "private": "DO-NOT-EXPOSE",
            "pending": {"chat": "DO-NOT-EXPOSE"},
        }
        body = json.dumps(notify.make_payload(receipt))
        self.assertIn("provider_blocked", body)
        self.assertIn("ledger only", body)
        self.assertNotIn("DO-NOT-EXPOSE", body)
        self.assertNotIn("private-lab-secret", body)

    def test_delivery_uses_post_json_without_credentials_in_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            path.write_text(json.dumps({
                "status": "scheduled_or_waiting",
                "reasons": [],
                "private": "DO-NOT-EXPOSE",
            }), encoding="utf-8")
            with patch.object(notify.urllib.request, "urlopen") as network:
                network.return_value.__enter__.return_value.status = 200
                notify.deliver(path, FAKE_HOOK)
                request = network.call_args.args[0]
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(network.call_args.kwargs["timeout"], 5)
                self.assertEqual(request.full_url, FAKE_HOOK)
                payload = json.loads(request.data)
                self.assertEqual(set(payload), {"text"})
                self.assertNotIn("DO-NOT-EXPOSE", payload["text"])
                self.assertNotIn("test-only-token", payload["text"])

    def test_failures_never_log_secret_url(self):
        with patch.dict(notify.os.environ, {"SLACK_WEBHOOK_URL": "https://evil.test/secret"}), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(notify.main(["--report", "/missing/receipt.json"]), 1)
            self.assertNotIn("evil.test", errors.getvalue())


if __name__ == "__main__":
    unittest.main()

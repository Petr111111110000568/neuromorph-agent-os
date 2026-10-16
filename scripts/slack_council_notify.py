"""Optional, read-only Slack delivery of a minimal cloud council receipt.

No webhook -> no network request. Never print the secret, raw receipt or response.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


ALLOWED_STATUSES = {"degraded", "scheduled_or_waiting"}
ALLOWED_REASONS = {
    "last_attempt_failed_or_interrupted",
    "provider_blocked",
    "unfinished_reservation",
    "no_successful_model_response",
    "next_due_beyond_max_cooldown",
    "overdue_more_than_18h",
    "invalid_or_missing_ledger",
    "ledger_checkout_or_read_failed",
}


def valid_webhook(value):
    try:
        parts = urllib.parse.urlsplit(value)
        return (
            parts.scheme == "https"
            and parts.hostname in {"hooks.slack.com", "hooks.slack-gov.com"}
            and parts.port is None
            and parts.username is None
            and parts.password is None
            and parts.path.startswith("/services/")
            and len(parts.path) > len("/services/")
            and not parts.query
            and not parts.fragment
        )
    except (TypeError, ValueError):
        return False


def make_payload(receipt):
    """Allowlist all emitted receipt-derived fields to avoid leaking ledger content."""
    if not isinstance(receipt, dict):
        raise ValueError("invalid receipt")
    status = receipt.get("status")
    if status not in ALLOWED_STATUSES:
        status = "unknown"
    raw_reasons = receipt.get("reasons")
    if not isinstance(raw_reasons, list) or len(raw_reasons) > 32:
        raw_reasons = []
    reasons = sorted({reason for reason in raw_reasons
                      if isinstance(reason, str) and reason in ALLOWED_REASONS})
    suffix = ", ".join(reasons) if reasons else "none"
    return {"text": (
        f"NeuroMorph council cloud receipt: {status}. "
        f"Reason codes: {suffix}. "
        "Scope: GitHub ledger only; no live model, PC or Slack-agent health checked."
    )}


def deliver(report_path, webhook, transport=urllib.request.urlopen):
    if not valid_webhook(webhook):
        raise ValueError("invalid webhook")
    raw = Path(report_path).read_bytes()
    if len(raw) > 65536:
        raise ValueError("oversized report")
    receipt = json.loads(raw)
    payload = make_payload(receipt)
    request = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with transport(request, timeout=5) as response:
        if response.status != 200:
            raise ValueError("unexpected response status")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook:
        print("Slack council receipt: skipped (no webhook secret configured)")
        return 0
    try:
        deliver(args.report, webhook)
    except (OSError, TypeError, ValueError, urllib.error.URLError):
        # No exception string: urllib errors can contain credential-bearing URLs.
        print("Slack council receipt: failed (details withheld)", file=sys.stderr)
        return 1
    print("Slack council receipt: sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

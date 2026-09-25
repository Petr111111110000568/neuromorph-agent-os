"""Pure supervisor checks; execute only in the authorized cloud test environment."""
from copy import deepcopy
import unittest

from workbench.autonomy.contributor_health import classify_event, plan_handoff


def public_task():
    return {"task_id": "research-001", "input_packet_sha256": "a" * 64,
            "data_class": "public_only", "phase": "review",
            "required_capabilities": ["source_review"], "handoff_hops": 0}


def health_snapshot():
    return [
        {"id": "qwen", "allowlisted": True, "provider_group": "qwen",
         "health": "cooldown", "capabilities": ["source_review"]},
        {"id": "deepseek", "allowlisted": True, "provider_group": "deepseek",
         "health": "healthy", "capabilities": ["source_review", "propose"],
         "dispatch_enabled": True, "zero_spend_verified": True,
         "execution_location": "explicitly_selected_cloud_runtime"},
    ]


class ContributorHealthTests(unittest.TestCase):
    def test_transient_recovery_stops_after_two_retries(self):
        for event in ("timeout", "transient"):
            with self.subTest(event=event):
                first = classify_event(event)
                second = classify_event(event, first["next_retry_count"])
                exhausted = classify_event(event, second["next_retry_count"])
                self.assertEqual([first["delay_seconds"], second["delay_seconds"]], [30, 120])
                self.assertTrue(first["retry_allowed"])
                self.assertTrue(second["retry_allowed"])
                self.assertFalse(exhausted["retry_allowed"])
                self.assertEqual(exhausted["status"], "retry_exhausted")
                self.assertEqual(exhausted["action"], "checkpoint_and_stop")
                self.assertFalse(exhausted["actions_executed"])

    def test_captcha_and_auth_require_user_without_secret_transfer(self):
        captcha = classify_event("captcha")
        self.assertEqual(captcha["status"], "needs_user_confirmation")
        self.assertTrue(captcha["requires_user"])
        self.assertFalse(captcha["automated_challenge_solving"])
        auth = classify_event("auth_lost")
        self.assertEqual(auth["status"], "needs_user_login")
        self.assertTrue(auth["requires_user"])
        self.assertFalse(auth["credential_transfer_allowed"])
        self.assertFalse(captcha["retry_allowed"])
        self.assertFalse(auth["retry_allowed"])

    def test_region_quota_payment_do_not_retry_or_bypass(self):
        region = classify_event("region_blocked")
        self.assertEqual(region["status"], "disabled_no_bypass")
        self.assertFalse(region["bypass_allowed"])
        quota = classify_event("quota", retry_after_seconds=3600)
        self.assertEqual(quota["status"], "cooldown")
        self.assertEqual(quota["delay_seconds"], 3600)
        self.assertFalse(quota["anonymous_rotation_allowed"])
        budget = classify_event("payment")
        self.assertEqual(budget["status"], "blocked_budget")
        self.assertFalse(budget["paid_fallback_allowed"])
        self.assertTrue(all(not item["retry_allowed"] for item in (region, quota, budget)))

    def test_unknown_event_needs_review_and_success_does_not_reset_attempts(self):
        self.assertEqual(classify_event("unknown")["status"], "needs_review")
        healthy = classify_event("healthy", retry_count=2)
        self.assertEqual(healthy["next_retry_count"], 2)
        self.assertFalse(healthy["checkpoint_required"])
        self.assertFalse(healthy["actions_executed"])

    def test_retry_and_delay_boundaries_reject_invalid_values(self):
        for value in (-1, 3, True, 1.5, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                classify_event("timeout", value)
        for value in (0, -1, 604801, True, float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                classify_event("quota", retry_after_seconds=value)
        with self.assertRaises(ValueError):
            classify_event("solve_captcha")

    def test_public_handoff_is_only_a_reference_proposal_and_preserves_inputs(self):
        task, contributors = public_task(), health_snapshot()
        original = deepcopy((task, contributors))
        result = plan_handoff(task, "qwen", "deepseek", contributors, event="quota")
        self.assertTrue(result["allowed"])
        self.assertTrue(result["controller_dispatch_required"])
        self.assertTrue(result["referenced_payload_validation_required"])
        self.assertFalse(result["actions_executed"])
        self.assertEqual(result["outcome_status"], "unverified")
        self.assertEqual(result["envelope"]["handoff_hops"], 1)
        self.assertEqual(result["envelope"]["input_packet_sha256"], "a" * 64)
        self.assertNotIn("payload", result["envelope"])
        self.assertEqual((task, contributors), original)

    def test_same_task_receipt_prevents_duplicate_delivery(self):
        args = (public_task(), "qwen", "deepseek", health_snapshot())
        first = plan_handoff(*args)
        self.assertEqual(first["handoff_id"], plan_handoff(*args)["handoff_id"])
        duplicate = plan_handoff(*args, delivered_ids=[first["handoff_id"]])
        self.assertFalse(duplicate["allowed"])
        self.assertEqual(duplicate["reason"], "already_delivered")
        changed = public_task()
        changed["input_packet_sha256"] = "b" * 64
        self.assertNotEqual(first["handoff_id"],
                            plan_handoff(changed, "qwen", "deepseek", health_snapshot())["handoff_id"])

    def test_private_or_extended_payload_is_rejected(self):
        for name, value in (("data_class", "private"), ("oauth_token", "never-transfer"),
                            ("payload", {"private_chat": "never-transfer"})):
            task = public_task()
            task[name] = value
            with self.subTest(field=name), self.assertRaises(ValueError):
                plan_handoff(task, "qwen", "deepseek", health_snapshot())

    def test_handoff_depth_is_finite(self):
        task = public_task()
        task["handoff_hops"] = 4
        self.assertEqual(plan_handoff(task, "qwen", "deepseek", health_snapshot())["reason"],
                         "handoff_limit_reached")
        task["handoff_hops"] = 5
        with self.assertRaises(ValueError):
            plan_handoff(task, "qwen", "deepseek", health_snapshot())

    def test_target_requires_allowlist_health_cloud_and_zero_spend(self):
        changes = [
            ("allowlisted", False, "target_not_allowlisted"),
            ("dispatch_enabled", False, "target_dispatch_disabled"),
            ("zero_spend_verified", False, "target_zero_spend_unverified"),
            ("health", "preflight_verified", "target_not_healthy"),
            ("execution_location", "local", "target_not_cloud"),
            ("capabilities", ["different_skill"], "target_missing_capability"),
        ]
        for field, value, reason in changes:
            contributors = health_snapshot()
            contributors[1][field] = value
            with self.subTest(field=field):
                result = plan_handoff(public_task(), "qwen", "deepseek", contributors)
                self.assertFalse(result["allowed"])
                self.assertEqual(result["reason"], reason)

    def test_same_provider_account_rotation_is_disallowed(self):
        contributors = health_snapshot()
        contributors[1]["provider_group"] = "qwen"
        for event in ("quota", "captcha", "auth_lost", "region_blocked"):
            with self.subTest(event=event):
                self.assertEqual(plan_handoff(public_task(), "qwen", "deepseek",
                                               contributors, event=event)["reason"],
                                 "same_provider_rotation_disallowed")

    def test_source_and_target_must_be_registered_distinct_participants(self):
        self.assertEqual(plan_handoff(public_task(), "unknown", "deepseek", health_snapshot())["reason"],
                         "source_not_allowlisted")
        self.assertEqual(plan_handoff(public_task(), "qwen", "unknown", health_snapshot())["reason"],
                         "target_not_allowlisted")
        self.assertEqual(plan_handoff(public_task(), "qwen", "qwen", health_snapshot())["reason"],
                         "same_participant")

    def test_duplicate_ids_and_bad_receipts_fail_closed(self):
        contributors = health_snapshot()
        contributors.append(deepcopy(contributors[1]))
        with self.assertRaises(ValueError):
            plan_handoff(public_task(), "qwen", "deepseek", contributors)
        with self.assertRaises(ValueError):
            plan_handoff(public_task(), "qwen", "deepseek", health_snapshot(),
                         delivered_ids=["not-a-hash"])

    def test_required_capabilities_and_digest_are_validated(self):
        for name, value in (("input_packet_sha256", "bad"),
                            ("required_capabilities", []),
                            ("required_capabilities", ["source_review", "source_review"]),
                            ("task_id", "../private"), ("phase", ["review"])):
            task = public_task()
            task[name] = value
            with self.subTest(field=name), self.assertRaises(ValueError):
                plan_handoff(task, "qwen", "deepseek", health_snapshot())


if __name__ == "__main__":
    unittest.main()



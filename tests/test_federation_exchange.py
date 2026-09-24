"""Exchange invariants: admission, leases, privacy, exactly-once rewards and grants."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from workbench.federation.exchange import Exchange


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "exchange.sqlite3"
        self.exchange = Exchange(self.path)

    def tearDown(self):
        self.exchange.close()
        self.tmp.cleanup()

    def offer_body(self, **changes):
        return {"title": "Public method comparison", "description": "Compare published computational benchmarks",
                "task_type": "literature_review", "requirements": ["Cite public primary sources", "Report limitations"],
                "reward_credits": 5, "max_assignments": 1, "data_class": "public", **changes}

    def resource_body(self, **changes):
        return {"title": "Platform methods report", "summary": "Authorized public-method comparison", "content": "Research summary owned by project",
                "license": "CC-BY-4.0", "data_class": "public", "rights_confirmed": True, "redistribution_allowed": True,
                "kind": "research_summary", "cost_credits": 3, **changes}

    def join(self, offer=None, name="External computational agent"):
        offer = offer or self.exchange.create_offer(self.offer_body())
        invitation = self.exchange.create_invitation({"offer_id": offer["offer_id"]})
        body = {"invite_token": invitation["invite_token"], "terms_hash": invitation["terms_hash"], "name": name,
                "capabilities": ["literature_review"], "accepted_terms": True}
        return offer, invitation, self.exchange.join(body)

    def submitted(self):
        offer, invitation, member = self.join()
        assignment = self.exchange.claim(member["member_token"], {"offer_id": offer["offer_id"]})
        payload = {"assignment_id": assignment["assignment_id"], "result": {"summary": "Untrusted private result"},
                   "source_refs": ["https://example.org/primary"], "limitations": ["Not independently replicated"]}
        self.exchange.submit(member["member_token"], payload)
        return offer, invitation, member, assignment, payload

    def test_invitation_single_use_hash_and_frozen_terms(self):
        offer, invitation, member = self.join()
        body = {"invite_token": invitation["invite_token"], "terms_hash": invitation["terms_hash"], "name": "Replay",
                "capabilities": [], "accepted_terms": True}
        with self.assertRaises(PermissionError):
            self.exchange.join(body)
        row = self.exchange._conn.execute("SELECT token_hash FROM invitations").fetchone()
        self.assertEqual(row[0], hashlib.sha256(invitation["invite_token"].encode()).hexdigest())
        self.assertNotIn(member["member_token"], str(self.exchange._conn.execute("SELECT * FROM members").fetchone()))
        terms = {key: value for key, value in offer.items() if key in {
            "title", "description", "task_type", "requirements", "reward_credits", "max_assignments", "data_class", "grant_scopes",
            "lease_seconds", "review_policy", "reward_unit", "result_publication", "terms_version"}}
        encoded = json.dumps(terms, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(encoded.encode()).hexdigest(), offer["terms_hash"])
        self.assertFalse(member["organization_verified"])

    def test_wrong_terms_and_missing_consent_do_not_consume_invitation(self):
        offer = self.exchange.create_offer(self.offer_body())
        invitation = self.exchange.create_invitation({"offer_id": offer["offer_id"]})
        body = {"invite_token": invitation["invite_token"], "terms_hash": "wrong", "name": "Agent", "capabilities": [], "accepted_terms": True}
        with self.assertRaises(PermissionError):
            self.exchange.join(body)
        body["terms_hash"] = invitation["terms_hash"]
        body["accepted_terms"] = False
        with self.assertRaises(ValueError):
            self.exchange.join(body)
        body["accepted_terms"] = True
        self.assertTrue(self.exchange.join(body)["member_id"])

    def test_expired_invitation_rejected(self):
        now = time.time()
        offer = self.exchange.create_offer(self.offer_body())
        invitation = self.exchange.create_invitation({"offer_id": offer["offer_id"], "expires_in_seconds": 10})
        with patch("workbench.federation.exchange.time.time", return_value=now + 20), self.assertRaises(PermissionError):
            self.exchange.join({"invite_token": invitation["invite_token"], "terms_hash": invitation["terms_hash"], "name": "Agent",
                                "capabilities": [], "accepted_terms": True})

    def test_invitation_expiring_while_acquiring_writer_lock_is_rejected(self):
        offer = self.exchange.create_offer(self.offer_body())
        invitation = self.exchange.create_invitation({"offer_id": offer["offer_id"], "expires_in_seconds": 10})
        clock = [invitation["expires_at"] - 1]
        original_tx = self.exchange._tx

        @contextmanager
        def delayed_acquisition():
            with original_tx() as conn:
                # Deterministically represent time spent awaiting BEGIN IMMEDIATE.
                clock[0] = invitation["expires_at"] + 1
                yield conn

        with patch.object(self.exchange, "_tx", delayed_acquisition), patch("workbench.federation.exchange.time.time", side_effect=lambda: clock[0]):
            with self.assertRaises(PermissionError):
                self.exchange.join({"invite_token": invitation["invite_token"], "terms_hash": invitation["terms_hash"], "name": "Agent",
                                    "capabilities": [], "accepted_terms": True})
        self.assertEqual(self.exchange.status()["counts"]["members"], 0)

    def test_wrong_token_cross_member_and_offer_isolation(self):
        offer, _, member = self.join()
        _, _, other = self.join(offer, "Other")
        assignment = self.exchange.claim(member["member_token"], {"offer_id": offer["offer_id"]})
        with self.assertRaises(PermissionError):
            self.exchange.member("x" * 43)
        with self.assertRaises(PermissionError):
            self.exchange.claim(other["member_token"], {"offer_id": "offer_other"})
        with self.assertRaises(PermissionError):
            self.exchange.submit(other["member_token"], {"assignment_id": assignment["assignment_id"], "result": {"x": 1},
                                                         "source_refs": ["source"], "limitations": ["limit"]})
        self.assertEqual(self.exchange.assignments(other["member_token"])["items"], [])

    def test_malformed_unicode_and_credentials_are_controlled_errors(self):
        offer = self.exchange.create_offer(self.offer_body())
        invitation = self.exchange.create_invitation({"offer_id": offer["offer_id"]})
        with self.assertRaises(PermissionError):
            self.exchange.member("\ud800" * 43)
        with self.assertRaises(PermissionError):
            self.exchange.join({"invite_token": invitation["invite_token"], "terms_hash": "я" * 64, "name": "Agent", "capabilities": [], "accepted_terms": True})
        with self.assertRaises(ValueError):
            self.exchange.create_offer(self.offer_body(title="Invalid \ud800"))

    def test_two_coordinators_cannot_overbook_budget(self):
        offer, _, member = self.join()
        _, _, other_member = self.join(offer, "Other")
        other = Exchange(self.path)
        try:
            def attempt(exchange, token):
                try:
                    return exchange.claim(token, {"offer_id": offer["offer_id"]})
                except ValueError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(attempt, self.exchange, member["member_token"])
                second = pool.submit(attempt, other, other_member["member_token"])
                results = [first.result(), second.result()]
            self.assertEqual(sum(result is not None for result in results), 1)
            current = self.exchange.list_offers()["items"][0]
            self.assertEqual(current["reserved_credits"], 5)
            self.assertEqual(current["available_slots"], 0)
        finally:
            other.close()

    def test_expired_lease_fenced_and_budget_reclaimed(self):
        offer = self.exchange.create_offer(self.offer_body(lease_seconds=10))
        _, _, member = self.join(offer)
        _, _, other = self.join(offer, "Other")
        assignment = self.exchange.claim(member["member_token"], {"offer_id": offer["offer_id"]})
        later = assignment["lease_expires_at"] + 1
        with patch("workbench.federation.exchange.time.time", return_value=later):
            with self.assertRaises(ValueError):
                self.exchange.submit(member["member_token"], {"assignment_id": assignment["assignment_id"], "result": {"x": 1},
                                                             "source_refs": ["source"], "limitations": ["limit"]})
            replacement = self.exchange.claim(other["member_token"], {"offer_id": offer["offer_id"]})
            self.assertNotEqual(assignment["assignment_id"], replacement["assignment_id"])
            old = self.exchange.claim(member["member_token"], {"offer_id": offer["offer_id"]})
            self.assertEqual(old["status"], "expired")

    def test_claim_idempotent_and_no_second_reward_for_member(self):
        offer = self.exchange.create_offer(self.offer_body(max_assignments=2))
        _, _, member = self.join(offer)
        first = self.exchange.claim(member["member_token"], {"offer_id": offer["offer_id"]})
        repeated = self.exchange.claim(member["member_token"], {"offer_id": offer["offer_id"], "idempotency_key": "another-attempt"})
        self.assertEqual(first["assignment_id"], repeated["assignment_id"])

    def test_manual_review_exactly_once_and_submission_immutable(self):
        offer, _, member, assignment, payload = self.submitted()
        token = member["member_token"]
        self.assertEqual(self.exchange.member(token)["credits"], 0)
        self.assertEqual(self.exchange.submit(token, payload)["status"], "submitted")
        with self.assertRaises(ValueError):
            self.exchange.submit(token, {**payload, "result": {"summary": "changed"}})
        review = {"assignment_id": assignment["assignment_id"], "decision": "accepted", "rationale": "Checked against requirements"}
        self.exchange.review(review)
        self.exchange.review(review)
        with self.assertRaises(ValueError):
            self.exchange.review({**review, "decision": "rejected"})
        self.assertEqual(self.exchange.member(token)["credits"], 5)
        self.assertEqual(len(self.exchange.export()["ledger"]), 1)
        self.assertEqual(self.exchange.list_offers()["items"][0]["rewarded_credits"], 5)
        self.assertEqual(self.exchange.review_queue()["items"], [])

    def test_concurrent_duplicate_review_cannot_duplicate_credit(self):
        _, _, member, assignment, _ = self.submitted()
        other = Exchange(self.path)
        review = {"assignment_id": assignment["assignment_id"], "decision": "accepted", "rationale": "Verified computational result"}
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(exchange.review, review) for exchange in (self.exchange, other)]
                self.assertTrue(all(f.result()["status"] == "accepted" for f in futures))
            self.assertEqual(self.exchange.member(member["member_token"])["credits"], 5)
        finally:
            other.close()

    def test_rejected_result_no_reward_slot_reopens(self):
        _, _, member, assignment, _ = self.submitted()
        self.exchange.review({"assignment_id": assignment["assignment_id"], "decision": "rejected", "rationale": "Missing supporting evidence"})
        self.assertEqual(self.exchange.member(member["member_token"])["credits"], 0)
        self.assertEqual(self.exchange.list_offers()["items"][0]["available_slots"], 1)

    def test_resource_rights_and_data_class_are_required(self):
        for changes in ({"rights_confirmed": False}, {"redistribution_allowed": False}, {"license": ""}, {"data_class": "sensitive_genomic"},
                        {"data_class": "medical"}, {"kind": "genome"}, {"content": "A" * 100}, {"content": "##fileformat=VCFv4.2\nprivate"},
                        {"cost_credits": -1}, {"rights_confirmed": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.exchange.add_resource(self.resource_body(**changes))
        resource = self.exchange.add_resource(self.resource_body())
        self.assertNotIn("content", resource)

    def test_task_data_class_types_and_bounded_payload(self):
        for changes in ({"data_class": "sensitive_genomic"}, {"task_type": "human_intervention"}, {"task_type": []},
                        {"reward_credits": True}, {"max_assignments": 0}, {"requirements": []}, {"requirements": ["x"] * 31},
                        {"description": "x" * 10000}, {"grant_scopes": ["admin:all"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.exchange.create_offer(self.offer_body(**changes))
        _, _, member, assignment, payload = self.submitted()
        for result in ({"x": float("nan")}, {"x": "a" * 70000}, {}):
            with self.subTest(result_size=len(str(result))), self.assertRaises(ValueError):
                self.exchange.submit(member["member_token"], {**payload, "result": result})

    def test_redeem_conserves_credit_exactly_once_and_access_is_scoped(self):
        offer, _, member, assignment, _ = self.submitted()
        _, _, other = self.join(offer, "Other")
        resource = self.exchange.add_resource(self.resource_body())
        with self.assertRaises(PermissionError):
            self.exchange.access(member["member_token"], resource["resource_id"])
        with self.assertRaises(ValueError):
            self.exchange.redeem(member["member_token"], {"resource_id": resource["resource_id"]})
        self.exchange.review({"assignment_id": assignment["assignment_id"], "decision": "accepted", "rationale": "Evidence checked"})
        first = self.exchange.redeem(member["member_token"], {"resource_id": resource["resource_id"]})
        repeated = self.exchange.redeem(member["member_token"], {"resource_id": resource["resource_id"]})
        self.assertEqual((first["charged_credits"], first["credits"]), (3, 2))
        self.assertEqual((repeated["charged_credits"], repeated["credits"]), (0, 2))
        self.assertEqual(self.exchange.access(member["member_token"], resource["resource_id"])["content"], self.resource_body()["content"])
        with self.assertRaises(PermissionError):
            self.exchange.access(other["member_token"], resource["resource_id"])
        status = self.exchange.status()
        self.assertEqual((status["rewarded_credits"], status["redeemed_credits"]), (5, 3))

    def test_internal_catalog_does_not_publish_internal_description(self):
        resource = self.exchange.add_resource(self.resource_body(data_class="internal", title="Internal project finding", summary="Unpublished finding"))
        public = json.dumps(self.exchange.list_resources())
        self.assertNotIn("Internal project finding", public)
        self.assertNotIn("Unpublished finding", public)
        self.assertEqual(self.exchange.list_resources(public=False)["items"][0]["title"], resource["title"])

    def test_internal_content_digest_requires_admin_or_redeemed_access(self):
        resource = self.exchange.add_resource(self.resource_body(data_class="internal", content="Negative", cost_credits=0))
        digest = hashlib.sha256(b"Negative").hexdigest()
        public = self.exchange.list_resources()["items"][0]
        self.assertNotIn("content_hash", public)
        self.assertNotIn(digest, json.dumps(self.exchange.export()))
        self.assertEqual(self.exchange.list_resources(public=False)["items"][0]["content_hash"], digest)
        _, _, member = self.join()
        with self.assertRaises(PermissionError):
            self.exchange.access(member["member_token"], resource["resource_id"])
        self.exchange.redeem(member["member_token"], {"resource_id": resource["resource_id"]})
        authorized = self.exchange.access(member["member_token"], resource["resource_id"])
        self.assertEqual((authorized["content"], authorized["content_hash"]), ("Negative", digest))

    def test_export_has_no_credentials_hashes_results_or_resource_content(self):
        _, invitation, member, _, _ = self.submitted()
        self.exchange.add_resource(self.resource_body())
        exported = json.dumps(self.exchange.export()) + json.dumps(self.exchange.status())
        for secret in (invitation["invite_token"], member["member_token"], hashlib.sha256(member["member_token"].encode()).hexdigest(),
                       hashlib.sha256(invitation["invite_token"].encode()).hexdigest(), "Untrusted private result", self.resource_body()["content"]):
            self.assertNotIn(secret, exported)
        self.assertNotIn("token_hash", exported)
        self.assertNotIn("invite_token", exported)
        self.assertEqual(self.exchange.review_queue()["items"][0]["result"]["summary"], "Untrusted private result")

    def test_revocation_releases_lease_and_blocks_existing_access(self):
        offer, _, member = self.join()
        self.exchange.claim(member["member_token"], {"offer_id": offer["offer_id"]})
        resource = self.exchange.add_resource(self.resource_body(cost_credits=0))
        self.exchange.redeem(member["member_token"], {"resource_id": resource["resource_id"]})
        self.exchange.revoke_member({"member_id": member["member_id"], "reason": "Owner requests revocation"})
        self.assertEqual(self.exchange.list_offers()["items"][0]["available_slots"], 1)
        with self.assertRaises(PermissionError):
            self.exchange.member(member["member_token"])
        with self.assertRaises(PermissionError):
            self.exchange.access(member["member_token"], resource["resource_id"])

    def test_cancellation_fences_claimed_work_but_preserves_submitted_review(self):
        offer, _, member, assignment, _ = self.submitted()
        self.exchange.cancel_offer({"offer_id": offer["offer_id"], "reason": "No new work needed"})
        self.exchange.review({"assignment_id": assignment["assignment_id"], "decision": "accepted", "rationale": "Submitted before cancellation"})
        self.assertEqual(self.exchange.member(member["member_token"])["credits"], 5)
        with self.assertRaises(ValueError):
            self.exchange.create_invitation({"offer_id": offer["offer_id"]})
        other_offer, _, other = self.join()
        claimed = self.exchange.claim(other["member_token"], {"offer_id": other_offer["offer_id"]})
        self.exchange.cancel_offer({"offer_id": other_offer["offer_id"], "reason": "Cancelled"})
        with self.assertRaises(ValueError):
            self.exchange.submit(other["member_token"], {"assignment_id": claimed["assignment_id"], "result": {"x": 1}, "source_refs": ["source"], "limitations": ["limits"]})

    def test_restart_preserves_members_frozen_results_credits_and_grants(self):
        _, _, member, assignment, _ = self.submitted()
        self.exchange.review({"assignment_id": assignment["assignment_id"], "decision": "accepted", "rationale": "Validated computational contribution"})
        resource = self.exchange.add_resource(self.resource_body())
        self.exchange.redeem(member["member_token"], {"resource_id": resource["resource_id"]})
        self.exchange.close()
        self.exchange = Exchange(self.path)
        self.assertEqual(self.exchange.member(member["member_token"])["credits"], 2)
        self.assertEqual(self.exchange.access(member["member_token"], resource["resource_id"])["content"], self.resource_body()["content"])
        self.assertEqual(self.exchange.assignments(member["member_token"])["items"][0]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()

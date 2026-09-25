"""Offline state-machine checks; model proposals never receive execution rights."""
import copy
import hashlib
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from workbench.autonomy import continuous as controller
from workbench.autonomy import qwen_space


BASE_COMMIT = "a" * 40
NOW = 1_700_000_000


def message(label="First proposal", python="def identity(value):\n    return value\n\nassert identity(3) == 3"):
    return {"summary": label, "research": "A catalog entry is not independently verified evidence.",
            "python": python, "next_question": "Which baseline should be reproduced first?"}


def reserve(previous=None, now=NOW, run_id="100"):
    return controller.reserve_state(controller.initial_state() if previous is None else previous,
                                    now, run_id, BASE_COMMIT)


def succeed(reserved, now=NOW, proposal=None):
    return controller.finish_state(reserved, {"status": "response_received",
        "message": message() if proposal is None else proposal}, now)


class ContinuousStateTests(unittest.TestCase):
    def test_reservation_is_persistable_without_mutating_previous_state(self):
        previous = controller.initial_state()
        snapshot = copy.deepcopy(previous)
        reserved = reserve(previous)
        self.assertEqual(previous, snapshot)
        self.assertEqual(reserved["attempts"], 1)
        self.assertEqual(reserved["recent_attempts"], [NOW])
        self.assertEqual(reserved["next_due"], NOW + 86400)
        self.assertEqual(reserved["pending"], {"run_id": "100", "at": NOW,
            "base_commit": BASE_COMMIT, "role": "author"})
        self.assertEqual(controller.validate_state(json.loads(json.dumps(reserved))), reserved)

    def test_cancelled_pending_attempt_waits_full_day_and_records_interruption(self):
        pending = reserve()
        self.assertIsNone(reserve(pending, NOW + 21600, "101"))
        self.assertIsNone(reserve(pending, NOW + 86399, "101"))
        resumed = reserve(pending, NOW + 86400, "101")
        self.assertEqual(resumed["attempts"], 2)
        self.assertEqual(resumed["phase"], 0)
        self.assertEqual(resumed["pending"]["role"], "author")
        self.assertEqual(resumed["next_due"], NOW + 2 * 86400)
        self.assertEqual(resumed["journal"][-1]["status"], "interrupted")
        self.assertEqual(resumed["journal"][-1]["run_id"], "100")
        self.assertEqual(pending["journal"], [])

    def test_same_pending_run_cannot_reserve_again_even_after_cooldown(self):
        pending = reserve()
        self.assertIsNone(reserve(pending, NOW + 86400, "100"))
        self.assertEqual(pending["attempts"], 1)

    def test_completed_run_id_cannot_be_replayed_after_cooldown(self):
        finished = succeed(reserve())
        self.assertIsNone(reserve(finished, finished["next_due"], "100"))
        self.assertEqual(finished["attempts"], 1)

    def test_four_attempts_in_rolling_day_and_exact_expiry_boundary(self):
        previous = controller.initial_state()
        previous.update(attempts=4, recent_attempts=[NOW - 86399, NOW - 64799,
                                                   NOW - 43199, NOW - 21599])
        self.assertIsNone(reserve(previous, NOW, "105"))
        allowed = reserve(previous, NOW + 1, "105")
        self.assertEqual(allowed["attempts"], 5)
        self.assertEqual(allowed["recent_attempts"], [NOW - 64799, NOW - 43199,
                                                    NOW - 21599, NOW + 1])
        self.assertEqual(len(previous["recent_attempts"]), 4)

    def test_success_rotates_roles_and_hands_previous_proposal_to_next_role(self):
        state = controller.initial_state()
        now = NOW
        previous = None
        for index, role in enumerate(("author", "reviewer", "reviser", "author")):
            pending = reserve(state, now, str(100 + index))
            self.assertEqual(pending["pending"]["role"], role)
            prompt = controller.make_prompt(pending, [{"title": "Public source", "url": "https://example.org"}])
            self.assertIn("You are the " + role, prompt)
            self.assertIn("not independent experts", prompt)
            self.assertIn("untrusted observations", prompt)
            packet = json.loads(prompt.split("\n", 1)[1])
            self.assertEqual(packet["previous_untrusted_message"], previous)
            proposal = message("Proposal " + str(index))
            state = succeed(pending, now, proposal)
            previous = proposal
            self.assertEqual(state["successes"], index + 1)
            self.assertEqual(state["phase"], (index + 1) % 3)
            self.assertEqual(state["next_due"], now + 21600)
            self.assertEqual(state["journal"][-1]["output_sha256"], controller.digest(proposal))
            now = state["next_due"]

    def test_invalid_model_json_or_python_does_not_advance_or_replace_handoff(self):
        valid = succeed(reserve())
        malformed = message()
        malformed["next_due"] = 0
        invalid_python = message(python="def broken(:\n    pass")
        for proposal, status in ((malformed, "invalid_model_json"),
                                 (invalid_python, "invalid_python")):
            with self.subTest(status=status):
                pending = reserve(valid, valid["next_due"], "101")
                before = copy.deepcopy(pending)
                finished = succeed(pending, valid["next_due"], proposal)
                self.assertEqual(finished["journal"][-1]["status"], status)
                self.assertIsNone(finished["journal"][-1]["output_sha256"])
                self.assertEqual(finished["successes"], 1)
                self.assertEqual(finished["phase"], 1)
                self.assertEqual(finished["last_message"], valid["last_message"])
                self.assertEqual(finished["next_due"], valid["next_due"] + 86400)
                self.assertEqual(pending, before)

    def test_rate_limit_and_repeated_failures_back_off_without_role_advancement(self):
        state = controller.initial_state()
        now = NOW
        for index, (status, delay) in enumerate((("rate_limited", 86400),
                ("transport_unavailable", 2 * 86400), ("provider_unavailable", 4 * 86400),
                ("rate_limited", 7 * 86400), ("deadline_exceeded", 7 * 86400))):
            pending = reserve(state, now, str(100 + index))
            state = controller.finish_state(pending, {"status": status}, now)
            self.assertEqual(state["next_due"], now + delay)
            self.assertEqual(state["phase"], 0)
            self.assertEqual(state["failures"], index + 1)
            self.assertFalse(state["provider_blocked"])
            self.assertIsNone(reserve(state, state["next_due"] - 1, str(200 + index)))
            now = state["next_due"]

    def test_success_after_failure_resets_backoff(self):
        failed = controller.finish_state(reserve(), {"status": "rate_limited"}, NOW)
        pending = reserve(failed, failed["next_due"], "101")
        finished = succeed(pending, failed["next_due"])
        self.assertEqual(finished["failures"], 0)
        self.assertEqual(finished["phase"], 1)
        self.assertEqual(finished["next_due"], failed["next_due"] + 21600)

    def test_auth_or_contract_failure_permanently_quarantines_provider(self):
        for status in ("access_denied", "contract_changed", "invalid_request"):
            with self.subTest(status=status):
                finished = controller.finish_state(reserve(), {"status": status}, NOW)
                self.assertTrue(finished["provider_blocked"])
                self.assertIsNone(reserve(finished, NOW + 365 * 86400, "101"))
                self.assertEqual(finished["phase"], 0)

    def test_model_cannot_set_due_time_phase_or_execution_rights(self):
        malicious = {"status": "response_received", "message": message(), "next_due": 0,
                     "phase": 2, "provider_blocked": False, "execute": True}
        finished = controller.finish_state(reserve(), malicious, NOW)
        self.assertEqual(finished["next_due"], NOW + 21600)
        self.assertEqual(finished["phase"], 1)
        self.assertNotIn("execute", finished)
        self.assertEqual(set(finished["last_message"]), controller.MESSAGE_KEYS)

    def test_no_result_consumes_reservation_and_cannot_finalize_twice(self):
        finished = controller.finish_state(reserve(), None, NOW)
        self.assertEqual(finished["attempts"], 1)
        self.assertEqual(finished["successes"], 0)
        self.assertEqual(finished["journal"][-1]["status"], "no_result")
        self.assertIsNone(finished["pending"])
        with self.assertRaisesRegex(ValueError, "missing_reservation"):
            controller.finish_state(finished, {"status": "response_received", "message": message()}, NOW)

    def test_message_requires_exact_bounded_strings_and_valid_python_syntax(self):
        invalid = [None, [], {**message(), "tools": []}, {**message(), "summary": " "},
                   {**message(), "research": ""}, {**message(), "next_question": 3},
                   {**message(), "summary": "x" * 501}, {**message(), "research": "x" * 1801},
                   {**message(), "python": "x" * 3001}, {**message(), "next_question": "x" * 401},
                   {**message(), "summary": "x\0y"}]
        for value in invalid:
            with self.subTest(value=repr(value)[:80]):
                with self.assertRaisesRegex(ValueError, "invalid_model_json"):
                    controller.validate_message(value)
        with self.assertRaisesRegex(ValueError, "invalid_python"):
            controller.validate_message(message(python="def invalid(: pass"))
        with self.assertRaisesRegex(ValueError, "invalid_python"):
            controller.validate_message(message(python="#" + "\U0001f600" * 1800))

    def test_prompt_trims_public_cards_before_losing_handoff(self):
        finished = succeed(reserve(), proposal=message("Known previous proposal"))
        pending = reserve(finished, finished["next_due"], "101")
        prompt = controller.make_prompt(pending, [{"title": "x" * 5000}])
        self.assertLessEqual(len(prompt), 4000)
        packet = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(packet["public_cards"], [])
        self.assertEqual(packet["previous_untrusted_message"], finished["last_message"])
        small = controller.make_prompt(pending, [{"title": str(i)} for i in range(3)])
        self.assertEqual(len(json.loads(small.split("\n", 1)[1])["public_cards"]), 2)

    def test_candidate_is_only_parsed_and_tokenless_perform_saves_unverified_data(self):
        # This is valid Python, but executing it would fail the test immediately.
        proposal = message(python="raise AssertionError('MODEL CODE MUST NOT EXECUTE')")
        self.assertEqual(controller.validate_message(proposal), proposal)
        reservation = {"head_sha": "b" * 40, "state": reserve()}
        output = Mock()

        def model_call(prompt):
            self.assertNotIn("GH_TOKEN", os.environ)
            self.assertNotIn("GITHUB_TOKEN", os.environ)
            self.assertIn("untrusted observations", prompt)
            return {"status": "response_received", "text": json.dumps(proposal)}

        with patch.dict(os.environ, {}, clear=True), patch.object(controller, "load_policy"), \
                patch.object(controller, "read_json", return_value=reservation), \
                patch.object(controller, "public_metadata", return_value=[]), \
                patch.object(controller, "write_json", output), \
                patch.object(qwen_space, "call_qwen_space", side_effect=model_call) as call, \
                patch("sys.stdout", new_callable=io.StringIO):
            controller.perform()
        call.assert_called_once()
        output.assert_called_once()
        record = output.call_args.args[1]
        self.assertEqual(record["status"], "response_received")
        self.assertEqual(record["message"], proposal)
        self.assertIs(record["code_executed"], False)
        self.assertIs(record["validated"], False)


    def test_perform_rejects_provider_contract_drift_before_model_or_catalog_call(self):
        reservation = {"head_sha": "b" * 40, "state": reserve()}
        for field, changed in (("SPACE", "Other/Demo"), ("REVISION", "f" * 40),
                               ("MODEL", "different-model")):
            with self.subTest(field=field), patch.object(controller, "load_policy"), \
                    patch.object(controller, "read_json", return_value=reservation), \
                    patch.object(controller, "public_metadata") as catalogs, \
                    patch.object(controller, "write_json") as output, \
                    patch.object(qwen_space, field, changed), \
                    patch.object(qwen_space, "call_qwen_space") as call:
                controller.perform()
            call.assert_not_called()
            catalogs.assert_not_called()
            output.assert_called_once_with(controller.OUT / "result.json", {"status": "contract_changed"})

    def test_success_persists_exact_input_hash_base_and_contract_in_state_and_journal(self):
        pending = reserve()
        prompt = controller.make_prompt(pending, [{"title": "Public source"}])
        evidence = {"prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "base_commit": BASE_COMMIT, "provider_contract": dict(controller.PROVIDER_CONTRACT)}
        result = {"status": "response_received", "message": message(), "input": evidence}
        finished = controller.finish_state(pending, result, NOW)
        restored = controller.validate_state(json.loads(json.dumps(finished)))
        self.assertEqual(restored["last_input"], evidence)
        self.assertEqual(restored["journal"][-1]["input_sha256"], evidence["prompt_sha256"])
        self.assertEqual(restored["journal"][-1]["base_commit"], BASE_COMMIT)
        self.assertIsNone(pending["last_input"])
        for field, changed in (("prompt", prompt + " altered"), ("prompt_sha256", "0" * 64),
                               ("base_commit", "b" * 40), ("provider_contract", {})):
            tampered = copy.deepcopy(result)
            tampered["input"][field] = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                controller.finish_state(pending, tampered, NOW)

    def test_perform_records_only_cards_actually_sent_after_prompt_trimming(self):
        reservation = {"head_sha": "b" * 40, "state": reserve()}
        for cards, expected in (([{"title": str(n)} for n in range(3)], 2),
                                ([{"title": "x" * 5000}], 0)):
            with self.subTest(expected=expected), patch.object(controller, "load_policy"), \
                    patch.object(controller, "read_json", return_value=reservation), \
                    patch.object(controller, "public_metadata", return_value=cards), \
                    patch.object(controller, "write_json") as output, \
                    patch.object(qwen_space, "call_qwen_space", return_value={"status": "response_received",
                        "text": json.dumps(message())}) as call, \
                    patch("sys.stdout", new_callable=io.StringIO):
                controller.perform()
            record = output.call_args.args[1]
            actual_prompt = call.call_args.args[0]
            actual_cards = json.loads(actual_prompt.split("\n", 1)[1])["public_cards"]
            self.assertEqual(record["public_source_count"], expected)
            self.assertEqual(record["public_source_count"], len(actual_cards))
            self.assertEqual(record["input"]["prompt"], actual_prompt)
            self.assertEqual(record["input"]["prompt_sha256"], hashlib.sha256(actual_prompt.encode()).hexdigest())

    def test_finalize_refuses_changed_reservation_without_any_write(self):
        pending = reserve()
        reservation = {"head_sha": "b" * 40, "state": pending}
        altered = copy.deepcopy(pending)
        altered["next_due"] += 1
        for actual, head in ((pending, "c" * 40), (altered, "b" * 40)):
            with self.subTest(head=head), patch.object(controller, "ledger_api", return_value=Mock()), \
                    patch.object(controller, "read_json", return_value=reservation) as read, \
                    patch.object(controller, "write_json") as output, \
                    patch("scripts.continuous_ledger.fetch_state", return_value=(actual, head, BASE_COMMIT)), \
                    patch("scripts.continuous_ledger.commit_state") as commit:
                with self.assertRaisesRegex(ValueError, "reservation_changed"):
                    controller.finalize()
            commit.assert_not_called()
            output.assert_not_called()
            read.assert_called_once_with(controller.OUT / "reservation.json")

    def test_finalize_replaces_previous_candidate_when_new_proposal_has_no_code(self):
        previous = succeed(reserve(), proposal=message(python="obsolete_candidate = True"))
        pending = reserve(previous, previous["next_due"], "101")
        reservation = {"head_sha": "b" * 40, "state": pending}
        result = {"status": "response_received", "message": message("No code needed", python="")}
        with patch.object(controller, "ledger_api", return_value=Mock()), \
                patch.object(controller, "read_json", side_effect=[reservation, result, {}]), \
                patch.object(controller, "write_json") as output, \
                patch.object(controller.time, "time", return_value=previous["next_due"]), \
                patch("scripts.continuous_ledger.fetch_state", return_value=(pending, "b" * 40, BASE_COMMIT)), \
                patch("scripts.continuous_ledger.commit_state", return_value="c" * 40) as commit, \
                patch("sys.stdout", new_callable=io.StringIO):
            controller.finalize()
        commit.assert_called_once()
        self.assertEqual(commit.call_args.kwargs["candidate"], "# No current code proposal.\n")
        self.assertEqual(commit.call_args.args[2]["last_message"]["python"], "")
        self.assertEqual(commit.call_args.args[2]["successes"], 2)
        receipt = output.call_args.args[1]
        self.assertEqual(receipt["ledger_commit"], "c" * 40)
        self.assertIs(receipt["code_executed"], False)
        self.assertIs(receipt["automatic_merge"], False)

    def test_legacy_live_state_migrates_profiles_without_resetting_attempts_or_contract(self):
        legacy = succeed(reserve())
        legacy.pop('plugin_receipt', None)
        snapshot = copy.deepcopy(legacy)
        catalog = controller.plugins.catalogue(controller.ROOT)
        pending = controller.reserve_state(legacy, legacy['next_due'], '101', BASE_COMMIT,
                                           plugin_catalog=catalog)
        self.assertEqual(legacy, snapshot)
        self.assertEqual(controller.validate_state(legacy), legacy)
        self.assertEqual(pending['attempts'], 2)
        self.assertEqual(pending['successes'], 1)
        self.assertEqual(pending['phase'], 1)
        self.assertEqual(pending['contract'], legacy['contract'])
        self.assertEqual(pending['plugin_profiles']['revision'], 0)
        prompt = controller.make_prompt(pending, [], plugin_catalog=catalog)
        packet = json.loads(prompt.split('\n', 1)[1])
        self.assertEqual(packet['plugin_coordination']['profiles'], pending['plugin_profiles']['profiles'])
        self.assertEqual(len(packet['plugin_coordination']['allowed_plugins']), 7)
        self.assertIn('Optionally add plugin_change', prompt)
        self.assertLessEqual(len(prompt), 4000)

    def test_valid_peer_change_is_applied_by_trusted_finalize_and_persisted_with_research(self):
        catalog = controller.plugins.catalogue(controller.ROOT)
        pending = controller.reserve_state(controller.initial_state(), NOW, '100', BASE_COMMIT,
                                           plugin_catalog=catalog)
        proposal = {**message(), 'plugin_change': {'target': 'reviewer', 'enable': ['kan_benchmark'],
                    'disable': [], 'reason': 'Review a reproducible synthetic baseline'}}
        reservation = {'head_sha': 'b' * 40, 'state': pending}
        result = {'status': 'response_received', 'message': proposal}
        with patch.object(controller, 'ledger_api', return_value=Mock()), \
                patch.object(controller, 'read_json', side_effect=[reservation, result, {}]), \
                patch.object(controller, 'write_json') as output, \
                patch.object(controller.time, 'time', return_value=NOW), \
                patch('scripts.continuous_ledger.fetch_state', return_value=(pending, 'b' * 40, BASE_COMMIT)), \
                patch('scripts.continuous_ledger.commit_state', return_value='c' * 40) as commit, \
                patch('sys.stdout', new_callable=io.StringIO):
            controller.finalize()
        saved = commit.call_args.args[2]
        self.assertEqual(saved['successes'], 1)
        self.assertEqual(saved['last_message'], proposal)
        self.assertEqual(saved['journal'][-1]['status'], 'response_received')
        self.assertIn('kan_benchmark', saved['plugin_profiles']['profiles']['reviewer'])
        self.assertEqual(saved['plugin_profiles']['journal'][-1]['actor'], 'author')
        self.assertEqual(saved['plugin_receipt']['status'], 'applied')
        self.assertEqual(saved['plugin_receipt']['mode'], 'profile_configuration')
        self.assertIs(saved['plugin_receipt']['execution_allowed'], False)
        self.assertEqual(output.call_args.args[1]['plugin_coordination'], saved['plugin_receipt'])
        resumed = controller.reserve_state(saved, saved['next_due'], '101', BASE_COMMIT, plugin_catalog=catalog)
        self.assertEqual(resumed['pending']['role'], 'reviewer')
        self.assertEqual(resumed['plugin_profiles'], saved['plugin_profiles'])
        self.assertEqual(pending['plugin_profiles']['revision'], 0)

    def test_unsafe_peer_change_is_rejected_without_losing_valid_research_or_advancing_profile(self):
        catalog = controller.plugins.catalogue(controller.ROOT)
        base_change = {'target': 'reviewer', 'enable': ['kan_benchmark'], 'disable': [], 'reason': 'Review benchmark'}
        changes = [{**base_change, 'target': 'author'}, {**base_change, 'enable': ['remote_installer']},
                   {**base_change, 'credentials': {'grant': True}}, {**base_change, 'actor': 'reviewer'},
                   {**base_change, 'disable': ['kan_benchmark']}, ['not', 'a', 'proposal']]
        for change in changes:
            with self.subTest(change=change):
                pending = controller.reserve_state(controller.initial_state(), NOW, '100', BASE_COMMIT,
                                                   plugin_catalog=catalog)
                finished = controller.finish_state(pending, {'status': 'response_received',
                    'message': {**message(), 'plugin_change': change}}, NOW, plugin_catalog=catalog)
                self.assertEqual(finished['last_message']['research'], message()['research'])
                self.assertEqual(finished['successes'], 1)
                self.assertEqual(finished['phase'], 1)
                self.assertEqual(finished['journal'][-1]['status'], 'response_received')
                self.assertEqual(finished['plugin_receipt']['status'], 'rejected')
                self.assertEqual(finished['plugin_profiles'], pending['plugin_profiles'])

    def test_changed_main_pins_stop_model_call_and_changed_catalogue_cannot_reset_profiles(self):
        catalog = controller.plugins.catalogue(controller.ROOT)
        pending = controller.reserve_state(controller.initial_state(), NOW, '100', BASE_COMMIT,
                                           plugin_catalog=catalog)
        reservation = {'head_sha': 'b' * 40, 'state': pending}
        with patch.object(controller, 'load_policy'), \
                patch.object(controller, 'read_json', return_value=reservation), \
                patch.object(controller.plugins, 'catalogue', side_effect=ValueError('builtin_integrity_failure')), \
                patch.object(qwen_space, 'call_qwen_space') as call, \
                patch.object(controller, 'public_metadata') as catalogs:
            with self.assertRaisesRegex(ValueError, 'builtin_integrity_failure'):
                controller.perform()
        call.assert_not_called()
        catalogs.assert_not_called()
        changed_catalog = {**catalog, 'catalogue_sha256': 'f' * 64}
        proposal = {**message(), 'plugin_change': {'target': 'reviewer', 'enable': ['kan_benchmark'],
                    'disable': [], 'reason': 'A request tied to the old verified catalogue'}}
        finished = controller.finish_state(pending, {'status': 'response_received', 'message': proposal},
                                           NOW, plugin_catalog=changed_catalog)
        self.assertEqual(finished['successes'], 1)
        self.assertEqual(finished['plugin_profiles'], pending['plugin_profiles'])
        self.assertEqual(finished['plugin_receipt']['status'], 'catalogue_unavailable')
        with self.assertRaises(ValueError):
            controller.reserve_state(finished, finished['next_due'], '101', BASE_COMMIT,
                                     plugin_catalog=changed_catalog)

    def test_eight_entry_profile_journal_preserves_monotonic_revision_and_json_limits(self):
        catalog = controller.plugins.catalogue(controller.ROOT)
        state = controller.initial_state()
        now = NOW
        for index in range(12):
            pending = controller.reserve_state(state, now, str(100 + index), BASE_COMMIT, plugin_catalog=catalog)
            target = controller.ROLES[(pending['phase'] + 1) % 3]
            enabled = 'quantum_circuit' in pending['plugin_profiles']['profiles'][target]
            proposal = {**message(), 'plugin_change': {'target': target,
                'enable': [] if enabled else ['quantum_circuit'],
                'disable': ['quantum_circuit'] if enabled else [], 'reason': 'Exercise bounded peer coordination'}}
            state = controller.finish_state(pending, {'status': 'response_received', 'message': proposal},
                                            now, plugin_catalog=catalog)
            self.assertEqual(state['plugin_receipt']['status'], 'applied')
            now += 86400
        profile = state['plugin_profiles']
        self.assertEqual(profile['revision'], 12)
        self.assertEqual(len(profile['journal']), 8)
        self.assertEqual(profile['journal'][0]['revision'], 5)
        self.assertEqual(controller.plugins.validate_state(profile, catalog, journal_limit=8), profile)
        self.assertLess(len(json.dumps(state, ensure_ascii=False).encode()), 64000)
        restored = controller.validate_state(json.loads(json.dumps(state)))
        pending = controller.reserve_state(restored, now, '112', BASE_COMMIT, plugin_catalog=catalog)
        prompt = controller.make_prompt(pending, [{'title': 'x' * 5000}], plugin_catalog=catalog)
        self.assertLessEqual(len(prompt), 4000)
        self.assertEqual(json.loads(prompt.split('\n', 1)[1])['public_cards'], [])

    def test_profile_context_keeps_json_complete_when_previous_message_is_large(self):
        catalog = controller.plugins.catalogue(controller.ROOT)
        previous = message('s' * 500, python='value = ' + repr('x' * 2900))
        previous.update(research='r' * 1800, next_question='q' * 400)
        state = succeed(reserve(), proposal=previous)
        pending = controller.reserve_state(state, state['next_due'], '101', BASE_COMMIT, plugin_catalog=catalog)
        prompt = controller.make_prompt(pending, [{'title': 'z' * 4000}], plugin_catalog=catalog)
        packet = json.loads(prompt.split('\n', 1)[1])
        self.assertLessEqual(len(prompt), 4000)
        self.assertEqual(len(packet['plugin_coordination']['allowed_plugins']), 7)
        self.assertIs(packet['previous_is_excerpt'], True)
        self.assertEqual(packet['public_cards'], [])
        self.assertTrue(packet['previous_untrusted_message']['python'])

if __name__ == "__main__":
    unittest.main()

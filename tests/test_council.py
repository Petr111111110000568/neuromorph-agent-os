import copy
import unittest

from workbench.autonomy import council


class CouncilTests(unittest.TestCase):
    def proposal(self, state, proposal_id='security_role', member_id='security_reviewer',
                 kind='create_member'):
        value = {'id': proposal_id, 'kind': kind, 'expected_revision': state['revision'],
            'question': 'Which provenance and access checks should this role review?',
            'reason': 'Review the existing public proposal without new capabilities.',
            'security': {key: {'passed': True, 'evidence': 'Existing fixed guards remain enforced.'}
                         for key in council.SECURITY_KEYS}}
        if kind != 'meeting':
            value.update(member_id=member_id, focus='Inspect provenance and access boundaries.',
                         adapter=council.ADAPTER)
        return value

    def admit(self, state, now, proposal_id='security_role', member_id='security_reviewer'):
        value, _ = council.receive_proposal(state, 'author',
            self.proposal(state, proposal_id, member_id), now)
        value, _ = council.vote(value, 'reviewer', proposal_id, 'approve', now)
        return council.vote(value, 'reviser', proposal_id, 'approve', now)

    def test_two_distinct_other_roles_admit_same_backend_without_input_mutation(self):
        state = council.initial_state()
        before = copy.deepcopy(state)
        result, receipt = self.admit(state, 10)
        self.assertEqual(state, before)
        self.assertEqual(receipt['status'], 'member_admitted')
        self.assertEqual(result['members']['security_reviewer']['adapter'], council.ADAPTER)
        self.assertEqual(result['members']['security_reviewer']['approvers'], ['reviewer', 'reviser'])
        self.assertEqual(result['policy'], before['policy'])
        self.assertEqual(council.members(result), ('author', 'reviewer', 'reviser', 'security_reviewer'))

    def test_unauthorized_self_and_duplicate_votes_fail_closed(self):
        state = council.initial_state()
        state, _ = council.receive_proposal(state, 'author', self.proposal(state), 1)
        for actor in ('intruder', 'author'):
            with self.assertRaises(ValueError):
                council.vote(state, actor, 'security_role', 'approve', 2)
        state, _ = council.vote(state, 'reviewer', 'security_role', 'approve', 2)
        before = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            council.vote(state, 'reviewer', 'security_role', 'approve', 3)
        self.assertEqual(state, before)
        self.assertNotIn('security_reviewer', state['members'])

    def test_stale_revisions_and_backwards_clock_rejected(self):
        state = council.initial_state(10)
        stale = self.proposal(state)
        state, _ = council.receive_proposal(state, 'author', stale, 10)
        stale['id'] = 'another'
        with self.assertRaises(ValueError):
            council.receive_proposal(state, 'reviewer', stale, 11)
        with self.assertRaises(ValueError):
            council.vote(state, 'reviewer', 'security_role', 'approve', 11, expected_revision=0)
        with self.assertRaises(ValueError):
            council.vote(state, 'reviewer', 'security_role', 'approve', 9)

    def test_expiry_at_seven_days_prevents_activation_and_releases_pending_slot(self):
        state = council.initial_state()
        state, _ = council.receive_proposal(state, 'author', self.proposal(state), 10)
        state, _ = council.vote(state, 'reviewer', 'security_role', 'approve', 11)
        result, receipt = council.vote(state, 'reviser', 'security_role', 'approve', 10 + council.TTL)
        self.assertEqual(receipt['status'], 'expired')
        self.assertEqual(result['pending'], {})
        self.assertEqual(len(result['members']), 3)
        council.check_schema(result)

    def test_pending_limit_duplicate_ids_and_target_members(self):
        state = council.initial_state()
        state, _ = council.receive_proposal(state, 'author', self.proposal(state), 1)
        for proposal in (self.proposal(state), self.proposal(state, 'another')):
            with self.assertRaises(ValueError):
                council.receive_proposal(state, 'author', proposal, 2)
        for index in range(3):
            proposal = self.proposal(state, 'meeting_' + str(index), kind='meeting')
            state, _ = council.receive_proposal(state, 'author', proposal, 2)
        with self.assertRaises(ValueError):
            council.receive_proposal(state, 'author', self.proposal(state, 'overflow', kind='meeting'), 3)
        self.assertEqual(len(state['pending']), 4)

    def test_daily_admission_cooldown_does_not_consume_decisive_vote(self):
        state, _ = self.admit(council.initial_state(), 10)
        state, _ = council.receive_proposal(state, 'author',
            self.proposal(state, 'source_role', 'source_checker'), 20)
        state, _ = council.vote(state, 'reviewer', 'source_role', 'approve', 20)
        result, receipt = council.vote(state, 'reviser', 'source_role', 'approve', 21)
        self.assertEqual(receipt['status'], 'cooldown')
        self.assertEqual(result, state)
        result, receipt = council.vote(result, 'reviser', 'source_role', 'approve', 10 + council.DAY)
        self.assertEqual(receipt['status'], 'member_admitted')
        self.assertEqual(len(result['members']), 5)

    def test_member_cap_preserved_even_with_valid_votes(self):
        state = council.initial_state()
        for index in range(3):
            state, _ = self.admit(state, 10 + council.DAY * index,
                                 'proposal_' + str(index), 'role_' + str(index))
        result, receipt = council.receive_proposal(state, 'author',
            self.proposal(state, 'overflow', 'overflow_role'), 10 + 3 * council.DAY)
        self.assertEqual(receipt['status'], 'member_limit')
        self.assertEqual(len(result['members']), 6)
        self.assertNotIn('overflow', result['pending'])

    def test_external_url_and_unapproved_adapter_never_admitted(self):
        state = council.initial_state()
        for adapter in ('deepseek', 'https://example.com/agent'):
            proposal = self.proposal(state)
            proposal['adapter'] = adapter
            result, receipt = council.receive_proposal(state, 'author', proposal, 1)
            self.assertEqual(receipt['status'], 'unsupported_adapter')
            self.assertEqual(result, state)

    def test_all_five_evidence_checks_required_and_guards_not_model_options(self):
        state = council.initial_state()
        for key in council.SECURITY_KEYS:
            for modification in ('missing', 'failed', 'empty', 'numeric'):
                proposal = self.proposal(state)
                if modification == 'missing':
                    del proposal['security'][key]
                elif modification == 'failed':
                    proposal['security'][key]['passed'] = False
                elif modification == 'empty':
                    proposal['security'][key]['evidence'] = ''
                else:
                    proposal['security'][key]['passed'] = 1
                with self.subTest(key=key, modification=modification), self.assertRaises(ValueError):
                    council.receive_proposal(state, 'author', proposal, 1)
        proposal = self.proposal(state)
        proposal['paid_calls_allowed'] = True
        result, receipt = council.apply_action(state, 'author',
            {'operation': 'propose', 'proposal': proposal}, 1)
        self.assertEqual(receipt['status'], 'rejected')
        self.assertEqual(result, state)

    def test_policy_journal_revision_and_secrets_fail_closed(self):
        state, _ = self.admit(council.initial_state(), 1)
        for field, value in [('revision', 4), ('policy', {**state['policy'], 'execution_allowed': True})]:
            corrupted = copy.deepcopy(state)
            corrupted[field] = value
            with self.assertRaises(ValueError):
                council.apply_action(corrupted, 'author', {}, 2)
        proposal = self.proposal(state, 'secret_proposal', kind='meeting')
        proposal['reason'] = 'hf_' + 'X' * 30
        result, receipt = council.apply_action(state, 'author',
            {'operation': 'propose', 'proposal': proposal}, 2)
        self.assertEqual(result, state)
        self.assertNotIn('hf_', str(receipt))

    def test_meeting_and_rejection_do_not_add_members_or_grant_tools(self):
        state = council.initial_state()
        state, _ = council.receive_proposal(state, 'author',
            self.proposal(state, 'review_meeting', kind='meeting'), 1)
        state, _ = council.vote(state, 'reviewer', 'review_meeting', 'approve', 2)
        state, receipt = council.vote(state, 'reviser', 'review_meeting', 'approve', 3)
        self.assertEqual(receipt['status'], 'meeting_approved')
        self.assertEqual(len(state['members']), 3)
        state, _ = council.receive_proposal(state, 'author', self.proposal(state), 4)
        state, receipt = council.vote(state, 'reviewer', 'security_role', 'reject', 5)
        self.assertEqual(receipt['status'], 'rejected')
        self.assertEqual(state['pending'], {})
        self.assertFalse(state['policy']['execution_allowed'])

    def test_new_member_cannot_vote_in_an_earlier_electorate_and_journal_bounded(self):
        state = council.initial_state()
        state, _ = council.receive_proposal(state, 'author',
            self.proposal(state, 'earlier_meeting', kind='meeting'), 1)
        state, _ = self.admit(state, 2)
        with self.assertRaises(ValueError):
            council.vote(state, 'security_reviewer', 'earlier_meeting', 'approve', 3)
        for index in range(6):
            proposal_id = 'short_meeting_' + str(index)
            state, _ = council.receive_proposal(state, 'author',
                self.proposal(state, proposal_id, kind='meeting'), 4 + index)
            state, _ = council.vote(state, 'reviewer', proposal_id, 'reject', 4 + index)
        self.assertEqual(len(state['journal']), 8)
        council.check_schema(state)


if __name__ == '__main__':
    unittest.main()


"""Offline Actions ledger/checkpoint tests; GitHub API calls are mocked."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench.autonomy import cloud_actions as actions


def settings():
    return {'schema_version': 1, 'zero_budget_confirmed': True,
        'budget_evidence': {'checked_at': '2026-09-25', 'product': 'Actions', 'budget_usd': 0, 'stop_usage': True},
        'starts_at': '2026-09-25T18:30:00Z', 'expires_at': '2026-10-02T18:30:00Z',
        'max_workflow_runs': 28, 'max_model_attempts': 28, 'interval_seconds': 21600}


class CloudActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for name, path in {'CONTROL': self.base, 'INCOMING': self.base / 'incoming',
                'BUNDLE': self.base / 'bundle', 'REVIEWS': self.base / 'reviews',
                'STATE': self.base / 'reviews/state.json', 'INTAKE': self.base / 'discovery/cycle.json'}.items():
            context = patch.object(actions, name, path)
            context.start()
            self.addCleanup(context.stop)
        self.environment = {'GITHUB_REPOSITORY': actions.REPOSITORY, 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_RUN_ID': '42', 'GITHUB_RUN_NUMBER': '1', 'GITHUB_SHA': 'a' * 40,
            'CLOUD_DEFAULT_BRANCH': 'main', 'GITHUB_OUTPUT': str(self.base / 'outputs.txt')}
        self.env = patch.dict(actions.os.environ, self.environment)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = patch.object(actions, '_settings', return_value=(settings(), 'b' * 64))
        self.config.start()
        self.addCleanup(self.config.stop)
        self.clock = patch.object(actions.time, 'time', return_value=actions._utc('2026-09-25T19:00:00Z'))
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def current(self, number=1, run_id=42):
        return {'id': run_id, 'run_number': number, 'run_attempt': 1, 'workflow_id': 7,
            'head_branch': 'main', 'head_sha': 'a' * 40, 'path': actions.WORKFLOW,
            'event': 'workflow_dispatch', 'repository': {'private': False}, 'status': 'completed'}

    def selection(self):
        return {'schema_version': 1, 'settings_sha256': 'b' * 64, 'repository': actions.REPOSITORY,
            'workflow_id': 7, 'run_id': 43, 'run_number': 2, 'head_sha': 'a' * 40,
            'previous_run_id': 42, 'previous_run_number': 1}

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_config_fixed_limits_window_and_zero_budget(self):
        self.assertEqual(actions.validate_config(settings()), settings())
        for name, value in (('max_workflow_runs', 29), ('max_model_attempts', True),
                            ('interval_seconds', 60), ('expires_at', '2026-10-03T18:30:00Z')):
            with self.subTest(name=name), self.assertRaises(ValueError):
                actions.validate_config(dict(settings(), **{name: value}))
        invalid = deepcopy(settings())
        invalid['budget_evidence']['stop_usage'] = False
        with self.assertRaises(ValueError):
            actions.validate_config(invalid)

    def test_disabled_budget_or_expiry_never_reads_github(self):
        with patch.object(actions, '_settings', return_value=(dict(settings(), zero_budget_confirmed=False), 'b' * 64)), \
                patch.object(actions, '_api') as api:
            actions.select()
            api.assert_not_called()
        with patch.object(actions.time, 'time', return_value=actions._utc('2026-10-03T00:00:00Z')), \
                patch.object(actions, '_api') as api:
            actions.select()
            api.assert_not_called()

    def test_first_run_has_no_automatic_prior_state(self):
        current = self.current()
        with patch.object(actions, '_api', side_effect=[current, {'total_count': 1, 'workflow_runs': [current]}]):
            actions.select()
        selected = json.loads((self.base / 'selection.json').read_text())
        self.assertIsNone(selected['previous_run_id'])
        self.assertIn('active=true', (self.base / 'outputs.txt').read_text())

    def test_missing_previous_artifact_fails_closed_without_reset(self):
        previous, current = self.current(), self.current(2, 43)
        with patch.dict(actions.os.environ, GITHUB_RUN_ID='43', GITHUB_RUN_NUMBER='2'), \
                patch.object(actions, '_api', side_effect=[current,
                    {'total_count': 2, 'workflow_runs': [current, previous]}, {'artifacts': []}]), \
                self.assertRaisesRegex(ValueError, 'checkpoint is absent'):
            actions.select()
        self.assertNotIn('active=true', (self.base / 'outputs.txt').read_text())

    def test_rerun_rejected_before_github_reads(self):
        with patch.dict(actions.os.environ, GITHUB_RUN_ATTEMPT='2'), patch.object(actions, '_api') as api, \
                self.assertRaisesRegex(ValueError, 'rerun'):
            actions.select()
        api.assert_not_called()

    def test_restore_requires_exact_run_provenance_and_file_digest(self):
        selected = self.selection()
        self.write(self.base / 'selection.json', selected)
        state = {'schema_version': 1, 'job_id': 'c' * 64, 'attempts': 1,
                 'next_due': 1790440000, 'last_status': 'response_received'}
        state_path = self.base / 'incoming/cloud-state.json'
        self.write(state_path, state)
        prior = dict(selected, run_id=42, run_number=1, previous_run_id=None, previous_run_number=None,
                     halted=False, state_sha256=hashlib.sha256(state_path.read_bytes()).hexdigest(), report_sha256=None)
        self.write(self.base / 'incoming/checkpoint.json', prior)
        actions.prepare()
        self.assertEqual((self.base / 'reviews/state.json').read_bytes(), state_path.read_bytes())
        state_path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            actions.prepare()

    def test_transient_outage_consumes_attempt_and_persists_one_day_cooldown(self):
        self.write(self.base / 'prepared.json', {'halted': False})
        for status in ('provider_unavailable', 'transport_unavailable'):
            with self.subTest(status=status):
                actions.STATE.unlink(missing_ok=True)
                def command(arguments, seconds, log):
                    if 'workbench.autonomy.cloud_review' in arguments:
                        self.write(actions.STATE, {'schema_version': 1, 'job_id': 'c' * 64,
                            'attempts': 1, 'next_due': actions.time.time() + 21600, 'last_status': status})
                        return 2
                    return 0
                with patch.object(actions, '_command', side_effect=command) as run, patch.object(actions, 'load_policy'):
                    actions.execute()
                self.assertEqual(run.call_count, 2)
                checkpoint = json.loads(actions.STATE.read_text())
                self.assertEqual(checkpoint['attempts'], 1)
                self.assertEqual(checkpoint['next_due'], actions.time.time() + 86400)
                self.assertFalse(json.loads((self.base / 'execution.json').read_text())['halted'])

    def test_pack_rejects_counter_advancing_more_than_one_attempt(self):
        self.write(self.base / 'selection.json', self.selection())
        self.write(self.base / 'prepared.json', {'halted': False})
        self.write(self.base / 'incoming/cloud-state.json', {'attempts': 1})
        self.write(self.base / 'reviews/state.json', {'attempts': 4})
        with self.assertRaisesRegex(ValueError, 'one reserved attempt'):
            actions.pack()


if __name__ == '__main__':
    unittest.main()


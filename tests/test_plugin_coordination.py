"""Cloud-only test suite for pinned, data-only peer profile coordination."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from workbench.autonomy import plugin_coordination as coordination


class PluginCoordinationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for folder in ('data', 'config', 'workbench'):
            (self.root / folder).mkdir()
        for name in (*coordination.PINNED, 'data/builtin_pins.json',
                     'config/plugin_coordination.json', 'config/resource_policy.json'):
            shutil.copyfile(coordination.ROOT / name, self.root / name)
        self.config = json.loads((self.root / 'config/plugin_coordination.json').read_text(encoding='utf-8'))

    def config_update(self, change):
        value = copy.deepcopy(self.config)
        change(value)
        (self.root / 'config/plugin_coordination.json').write_text(json.dumps(value), encoding='utf-8')
        return coordination.catalogue(self.root)

    def request(self, state, catalog, enable=None, disable=None):
        return coordination.proposal(state, catalog, 'author', 'reviewer',
            ['quantum_circuit'] if enable is None else enable, [] if disable is None else disable,
            'Compare a reproducible abstract benchmark')

    def test_catalogue_reads_all_existing_pins_without_importing_plugin_code(self):
        # A pinned fixture contains a top-level exception: AST inspection must not execute it.
        source = self.root / 'workbench/plugins.py'
        source.write_bytes(source.read_bytes() + b"\nraise AssertionError('NEVER IMPORT THIS FIXTURE')\n")
        pins = json.loads((self.root / 'data/builtin_pins.json').read_text(encoding='utf-8'))
        pins['files']['workbench/plugins.py'] = hashlib.sha256(source.read_bytes()).hexdigest()
        (self.root / 'data/builtin_pins.json').write_text(json.dumps(pins), encoding='utf-8')
        catalog = coordination.catalogue(self.root)
        self.assertEqual(len(catalog['config']['catalogue']), 7)
        self.assertEqual(set(catalog['pins']), set(coordination.PINNED))
        self.assertEqual(catalog['config']['catalogue']['kan_benchmark']['version'], '0.8.0')

    def test_changed_pinned_code_is_rejected_before_profiles_are_created(self):
        for name in coordination.PINNED:
            path = self.root / name
            original = path.read_bytes()
            try:
                path.write_bytes(original + b'\n# unexpected code drift\n')
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'builtin_integrity_failure'):
                    coordination.catalogue(self.root)
            finally:
                path.write_bytes(original)
        self.assertFalse((self.root / 'runtime').exists())

    def test_remote_plugins_permissions_and_nonzero_policy_cannot_be_admitted(self):
        cases = [lambda c: c['catalogue'].update(remote={'entrypoint': 'https://example.org/arbitrary.py'}),
                 lambda c: c.update(network_allowed=True), lambda c: c.update(credentials_allowed=True),
                 lambda c: c.update(paid_calls_allowed=True), lambda c: c.update(install_url='https://example.org')]
        for change in cases:
            with self.subTest(change=repr(change)), self.assertRaises(ValueError):
                self.config_update(change)
        (self.root / 'config/plugin_coordination.json').write_text(json.dumps(self.config), encoding='utf-8')
        policy = json.loads((self.root / 'config/resource_policy.json').read_text(encoding='utf-8'))
        policy['daily_spend_limit_usd'] = 1
        (self.root / 'config/resource_policy.json').write_text(json.dumps(policy), encoding='utf-8')
        with self.assertRaises(ValueError):
            coordination.catalogue(self.root)

    def test_peer_proposal_evaluation_and_apply_are_separate_pure_transitions(self):
        catalog = coordination.catalogue(self.root)
        state = coordination.initial_state(catalog)
        original = copy.deepcopy(state)
        request = self.request(state, catalog)
        plan = coordination.evaluate(state, catalog, request)
        self.assertEqual(state, original)
        self.assertIs(plan['configuration_only'], True)
        self.assertIs(plan['execution_allowed'], False)
        updated = coordination.apply_plan(state, catalog, request, plan['plan_sha256'])
        self.assertEqual(state, original)
        self.assertIn('quantum_circuit', updated['profiles']['reviewer'])
        self.assertEqual(updated['profiles']['author'], state['profiles']['author'])
        self.assertEqual(updated['journal'][0]['actor'], 'author')
        self.assertEqual(updated['revision'], 1)

    def test_self_remote_ambiguous_and_extra_credentials_proposals_are_rejected(self):
        catalog = coordination.catalogue(self.root)
        state = coordination.initial_state(catalog)
        valid = self.request(state, catalog)
        changes = ({'target': 'author'}, {'enable': ['https://example.org/code.py']},
                   {'enable': ['quantum_circuit'], 'disable': ['quantum_circuit']},
                   {'credentials': {'token': 'not-accepted'}}, {'approved': True},
                   {'enable': ['quantum_circuit', 'quantum_circuit']})
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                coordination.evaluate(state, catalog, {**valid, **changed})

    def test_stale_revision_or_changed_reviewed_plan_cannot_apply(self):
        catalog = coordination.catalogue(self.root)
        state = coordination.initial_state(catalog)
        request = self.request(state, catalog)
        plan = coordination.evaluate(state, catalog, request)
        with self.assertRaisesRegex(ValueError, 'reviewed_plan_changed'):
            coordination.apply_plan(state, catalog, request, '0' * 64)
        changed_request = {**request, 'enable': ['kan_benchmark']}
        with self.assertRaisesRegex(ValueError, 'reviewed_plan_changed'):
            coordination.apply_plan(state, catalog, changed_request, plan['plan_sha256'])
        updated = coordination.apply_plan(state, catalog, request, plan['plan_sha256'])
        with self.assertRaisesRegex(ValueError, 'stale_or_invalid_proposal'):
            coordination.apply_plan(updated, catalog, request, plan['plan_sha256'])

    def test_compatibility_dependencies_conflicts_and_resource_limits(self):
        changes = [lambda c: c['catalogue']['quantum_circuit'].update(roles=['author']),
                   lambda c: c['catalogue']['quantum_circuit'].update(requires=['regression_benchmark']),
                   lambda c: c['catalogue']['quantum_circuit'].update(conflicts=['legacy_topology']),
                   lambda c: c['resources'].update(max_enabled=1),
                   lambda c: c['resources'].update(cpu_seconds=6),
                   lambda c: c['resources'].update(memory_mb=512)]
        for change in changes:
            catalog = self.config_update(change)
            state = coordination.initial_state(catalog)
            with self.subTest(change=repr(change)), self.assertRaises(ValueError):
                self.request(state, catalog)
        catalog = self.config_update(lambda c: c['catalogue']['quantum_circuit'].update(requires=['regression_benchmark']))
        request = self.request(coordination.initial_state(catalog), catalog,
                               enable=['regression_benchmark', 'quantum_circuit'])
        self.assertEqual(len(request['enable']), 2)

    def test_atomic_apply_and_rollback_persist_one_state_and_journal(self):
        state = coordination.transact(self.root, 'init')
        catalog = coordination.catalogue(self.root)
        request = self.request(state, catalog)
        plan = coordination.evaluate(state, catalog, request)
        applied = coordination.transact(self.root, 'apply', request, plan['plan_sha256'])
        path = self.root / 'runtime/plugin-coordination/state.json'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), applied)
        rolled_back = coordination.transact(self.root, 'rollback', expected=1)
        self.assertEqual(rolled_back['profiles'], state['profiles'])
        self.assertEqual(rolled_back['revision'], 2)
        self.assertEqual([x['operation'] for x in rolled_back['journal']], ['apply', 'rollback'])
        self.assertEqual(rolled_back['journal'][1]['previous_sha256'], applied['journal'][0]['entry_sha256'])
        with self.assertRaises(ValueError):
            coordination.transact(self.root, 'rollback', expected=1)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), rolled_back)

    def test_failed_replace_preserves_old_profiles_and_journal_and_cleans_temporary_file(self):
        state = coordination.transact(self.root, 'init')
        catalog = coordination.catalogue(self.root)
        request = self.request(state, catalog)
        plan = coordination.evaluate(state, catalog, request)
        path = self.root / 'runtime/plugin-coordination/state.json'
        original = path.read_bytes()
        with patch.object(coordination.os, 'replace', side_effect=OSError('fixture storage error')):
            with self.assertRaises(OSError):
                coordination.transact(self.root, 'apply', request, plan['plan_sha256'])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob('state.json.tmp-*')), [])

    def test_concurrent_controller_cannot_write_locked_state(self):
        state = coordination.transact(self.root, 'init')
        catalog = coordination.catalogue(self.root)
        request = self.request(state, catalog)
        plan = coordination.evaluate(state, catalog, request)
        path = self.root / 'runtime/plugin-coordination/state.json'
        with coordination._job_lock(path.with_suffix('.lock')):
            with self.assertRaises(ValueError):
                coordination.transact(self.root, 'apply', request, plan['plan_sha256'])
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), state)

    def test_changed_catalogue_or_corrupt_journal_is_rejected(self):
        catalog = coordination.catalogue(self.root)
        state = coordination.initial_state(catalog)
        request = self.request(state, catalog)
        plan = coordination.evaluate(state, catalog, request)
        applied = coordination.apply_plan(state, catalog, request, plan['plan_sha256'])
        corrupt = copy.deepcopy(applied)
        corrupt['journal'][0]['after'] = []
        with self.assertRaisesRegex(ValueError, 'profile_journal_integrity'):
            coordination.validate_state(corrupt, catalog)
        changed = self.config_update(lambda c: c['catalogue']['quantum_circuit'].update(roles=['author', 'reviewer']))
        with self.assertRaisesRegex(ValueError, 'profile_catalogue_changed'):
            coordination.validate_state(applied, changed)

    def test_journal_is_bounded_and_old_revision_never_becomes_replayable(self):
        catalog = coordination.catalogue(self.root)
        state = coordination.initial_state(catalog)
        first = self.request(state, catalog)
        first_plan = coordination.evaluate(state, catalog, first)
        for index in range(70):
            request = self.request(state, catalog, enable=['quantum_circuit'] if index % 2 == 0 else [],
                                   disable=[] if index % 2 == 0 else ['quantum_circuit'])
            plan = coordination.evaluate(state, catalog, request)
            state = coordination.apply_plan(state, catalog, request, plan['plan_sha256'])
        self.assertEqual(state['revision'], 70)
        self.assertEqual(len(state['journal']), 64)
        self.assertEqual(state['journal'][0]['revision'], 7)
        with self.assertRaises(ValueError):
            coordination.apply_plan(state, catalog, first, first_plan['plan_sha256'])
        self.assertLess(len(json.dumps(state).encode()), coordination.LIMIT)

    def test_trusted_bootstrap_is_idempotent_and_admission_does_not_execute_code(self):
        with patch('subprocess.Popen', side_effect=AssertionError('No program execution')) as launch:
            receipt = coordination.bootstrap(self.root)
            again = coordination.bootstrap(self.root)
        launch.assert_not_called()
        self.assertEqual(len(receipt['changes']), 3)
        self.assertEqual(again['changes'], [])
        self.assertEqual(receipt['model_calls'], 0)
        self.assertIs(receipt['installed_remote_code'], False)
        self.assertIs(receipt['execution_allowed'], False)
        self.assertTrue(all(len(enabled) == 7 for enabled in receipt['profiles'].values()))
        admitted = coordination.require_enabled('reviewer', 'kan_benchmark', self.root)
        self.assertEqual(admitted['plugin_id'], 'kan_benchmark')
        self.assertIs(admitted['execution_allowed'], False)
        with self.assertRaises(ValueError):
            coordination.require_enabled('reviewer', 'arbitrary_remote_plugin', self.root)

    def test_missing_state_is_not_silently_initialized_by_admission(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            coordination.require_enabled('reviewer', 'kan_benchmark', self.root)
        self.assertFalse((self.root / 'runtime/plugin-coordination/state.json').exists())

    def test_cli_bootstrap_returns_data_receipt_without_model_or_executor(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            status = coordination.main(['bootstrap', '--root', str(self.root)])
        receipt = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertIs(receipt['configuration_only'], True)
        self.assertIs(receipt['execution_allowed'], False)
        self.assertEqual(receipt['model_calls'], 0)


if __name__ == '__main__':
    unittest.main()

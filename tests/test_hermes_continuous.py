"""Continuous reservation/finalization with the optional Hermes transport.

Only provider/SDK I/O and GitHub are simulated; the controller writes and reads
its actual checkpoints. These tests make no remote calls or subprocesses.
"""
import copy
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workbench.autonomy import continuous as controller, qwen_space
from workbench.harnesses import hermes_qwen


NOW = 1_700_000_000
BASE = 'a' * 40
MESSAGE = {'summary': 'Transport review', 'research': 'This is an unverified test contribution.',
           'python': '', 'next_question': 'Which evidence should be checked next?'}


class HermesContinuousTests(unittest.TestCase):
    @contextmanager
    def cycle(self, answer):
        ledger = {'state': controller.initial_state(), 'head': BASE, 'commits': 0}

        def fetch(_api):
            return copy.deepcopy(ledger['state']), ledger['head'], BASE

        def commit(_api, expected, state, report, candidate=None):
            self.assertEqual(expected, ledger['head'])
            ledger['commits'] += 1
            ledger['state'] = copy.deepcopy(state)
            ledger['head'] = ('b' if ledger['commits'] == 1 else 'c') * 40
            return ledger['head']

        def provider(prompt):
            # An actual durable write has already happened before SDK inference.
            self.assertEqual(ledger['commits'], 1)
            self.assertEqual(ledger['state']['attempts'], 1)
            self.assertEqual(ledger['state']['pending']['run_id'], '100')
            checkpoint = controller.read_json(controller.OUT / 'result.json')
            self.assertEqual(checkpoint['status'], 'no_result')
            self.assertEqual(checkpoint['input']['prompt'], prompt)
            return copy.deepcopy(answer)

        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            output = Path(folder)
            output_file = output / 'github-output.txt'
            output_file.write_text('', encoding='utf-8')
            stack.enter_context(patch.object(controller, 'OUT', output))
            stack.enter_context(patch.object(controller, 'load_policy'))
            stack.enter_context(patch.object(controller, 'ledger_api', return_value=Mock()))
            stack.enter_context(patch.object(controller, 'public_metadata', return_value=[]))
            stack.enter_context(patch.object(controller.time, 'time', return_value=NOW))
            stack.enter_context(patch.object(controller, 'brief_now',
                return_value=datetime(2026, 9, 26, tzinfo=timezone.utc)))
            stack.enter_context(patch('scripts.continuous_ledger.fetch_state', side_effect=fetch))
            stack.enter_context(patch('scripts.continuous_ledger.commit_state', side_effect=commit))
            stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
            # Confined to these tests; existing direct-Qwen tests keep their default.
            stack.enter_context(patch.dict(os.environ, {'NEUROMORPH_HARNESS': 'hermes',
                'GITHUB_RUN_ID': '100', 'GITHUB_SHA': BASE, 'GITHUB_OUTPUT': str(output_file)}))
            call = stack.enter_context(patch.object(qwen_space, 'call_qwen_space', side_effect=provider))
            controller.reserve()
            self.assertIn('active=true', output_file.read_text(encoding='utf-8'))
            yield ledger, call

    def bridge(self, prompt, *, provider, output_dir, fixture=False):
        self.assertEqual(output_dir, controller.OUT / 'hermes')
        guard = hermes_qwen.ReservedQwenTurn(prompt, None if fixture else provider)
        try:
            text = guard.complete({'model': qwen_space.MODEL,
                'messages': [{'role': 'user', 'content': prompt}]})
        except ValueError:
            text = 'An SDK result cannot convert a failed provider into success.'
        return hermes_qwen.finish(guard, {'status': 'exited', 'returncode': 0},
                                  {'status': 'response_received', 'text': text})

    def assert_spent_failure(self, ledger, status):
        saved = ledger['state']
        self.assertEqual(ledger['commits'], 2)
        self.assertEqual(saved['attempts'], 1)
        self.assertEqual(saved['recent_attempts'], [NOW])
        self.assertEqual(saved['successes'], 0)
        self.assertEqual(saved['phase'], 0)
        self.assertIsNone(saved['pending'])
        self.assertEqual(saved['journal'][-1]['status'], status)
        self.assertEqual(saved['next_due'], NOW + 86400)

    def test_live_transport_uses_one_pre_reserved_attempt_and_advances_once(self):
        with self.cycle({'status': 'response_received', 'text': json.dumps(MESSAGE)}) as (ledger, call), \
                patch.object(hermes_qwen, 'run', side_effect=self.bridge) as transport:
            controller.perform()
            record = controller.read_json(controller.OUT / 'result.json')
            self.assertEqual(record['message'], MESSAGE)
            self.assertEqual(record['harness_receipt']['provider_adapter_calls'], 1)
            self.assertTrue(record['harness_receipt']['accepted'])
            controller.finalize()
            call.assert_called_once()
            transport.assert_called_once()
            self.assertEqual(ledger['commits'], 2)
            self.assertEqual(ledger['state']['attempts'], 1)
            self.assertEqual(ledger['state']['successes'], 1)
            self.assertEqual(ledger['state']['phase'], 1)
            self.assertEqual(ledger['state']['next_due'], NOW + 21600)

    def test_provider_rate_limit_is_not_replaced_by_sdk_text_or_fallback(self):
        with self.cycle({'status': 'rate_limited'}) as (ledger, call), \
                patch.object(hermes_qwen, 'run', side_effect=self.bridge):
            controller.perform()
            controller.finalize()
            call.assert_called_once()
            self.assert_spent_failure(ledger, 'rate_limited')

    def test_protocol_fixture_cannot_be_counted_as_research_success(self):
        def fixture_bridge(prompt, **kwargs):
            return self.bridge(prompt, fixture=True, **kwargs)

        with self.cycle({'status': 'response_received', 'text': json.dumps(MESSAGE)}) as (ledger, call), \
                patch.object(hermes_qwen, 'run', side_effect=fixture_bridge):
            controller.perform()
            record = controller.read_json(controller.OUT / 'result.json')
            self.assertIsNone(record['message'])
            self.assertFalse(record['harness_receipt']['live'])
            controller.finalize()
            call.assert_not_called()
            self.assert_spent_failure(ledger, 'failed')

    def test_harness_exception_after_delivery_keeps_checkpoint_and_spent_attempt(self):
        def interrupted(prompt, *, provider, output_dir):
            provider(prompt)
            raise RuntimeError('injected harness interruption')

        with self.cycle({'status': 'response_received', 'text': json.dumps(MESSAGE)}) as (ledger, call), \
                patch.object(hermes_qwen, 'run', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'injected harness interruption'):
                controller.perform()
            record = controller.read_json(controller.OUT / 'result.json')
            self.assertEqual(record['status'], 'no_result')
            self.assertIn('brief_admission', record)
            self.assertNotIn('injected harness interruption', json.dumps(record))
            controller.finalize()
            call.assert_called_once()
            self.assert_spent_failure(ledger, 'no_result')
            self.assertEqual(ledger['state']['brief_admission'], record['brief_admission'])
            self.assertEqual(ledger['state']['journal'][-1]['input_sha256'], record['prompt_sha256'])


if __name__ == '__main__':
    unittest.main()

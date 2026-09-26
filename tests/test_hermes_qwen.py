import json
import threading
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from workbench.harnesses import hermes_qwen as adapter
from workbench.harnesses.free_gateway import server_for

PROMPT = 'HERMES-001 public research task'


def payload(**changes):
    return {'model': adapter.MODEL, 'messages': [{'role': 'system', 'content': 'Harness context'},
            {'role': 'user', 'content': PROMPT}], **changes}


class HermesQwenTests(unittest.TestCase):
    def test_exact_prompt_only_and_no_second_adapter_call(self):
        provider = Mock(return_value={'status': 'response_received', 'text': 'Actual provider result'})
        guard = adapter.ReservedQwenTurn(PROMPT, provider)
        self.assertEqual(guard.complete(payload()), 'Actual provider result')
        provider.assert_called_once_with(PROMPT)
        with self.assertRaisesRegex(ValueError, 'already_attempted'):
            guard.complete(payload())
        provider.assert_called_once()

    def test_scope_model_tools_secret_and_history_substitution_are_rejected(self):
        changes = [{'model': 'paid-model'}, {'tools': [{'type': 'function'}]},
            {'messages': [{'role': 'user', 'content': PROMPT + ' changed'}]},
            {'messages': [{'role': 'user', 'content': PROMPT}, {'role': 'user', 'content': PROMPT}]},
            {'messages': [{'role': 'assistant', 'content': PROMPT}]},
            {'messages': [{'role': 'user', 'content': PROMPT, 'name': 'injected'}]},
            {'messages': [{'role': 'system', 'content': 'sk-' + 's' * 20}, {'role': 'user', 'content': PROMPT}]}]
        for change in changes:
            provider = Mock()
            guard = adapter.ReservedQwenTurn(PROMPT, provider)
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.complete(payload(**change))
            provider.assert_not_called()

    def test_rate_limit_is_not_replaced_by_sdk_cached_answer(self):
        guard = adapter.ReservedQwenTurn(PROMPT, Mock(return_value={'status': 'rate_limited'}))
        with self.assertRaises(ValueError):
            guard.complete(payload())
        result = adapter.finish(guard, {'status': 'exited', 'returncode': 0},
            {'status': 'response_received', 'text': 'Stale answer'})
        self.assertEqual(result['status'], 'rate_limited')
        self.assertFalse(result['harness_receipt']['accepted'])
        guard.provider.assert_called_once()

    def test_output_requires_successful_sdk_and_identical_actual_text(self):
        for process, text in [({'status': 'timeout', 'returncode': -9}, 'Real result'),
                              ({'status': 'exited', 'returncode': 0}, 'Substituted result')]:
            guard = adapter.ReservedQwenTurn(PROMPT, Mock(return_value={'status': 'response_received', 'text': 'Real result'}))
            guard.complete(payload())
            result = adapter.finish(guard, process, {'status': 'response_received', 'text': text})
            self.assertEqual(result['status'], 'failed')
            self.assertNotIn('text', result)

    def test_fixture_is_explicitly_not_live_inference(self):
        guard = adapter.ReservedQwenTurn(PROMPT)
        text = guard.complete(payload())
        result = adapter.finish(guard, {'status': 'exited', 'returncode': 0}, {'status': 'response_received', 'text': text})
        self.assertEqual(result['status'], 'protocol_fixture_received')
        self.assertEqual(result['harness_receipt']['provider_adapter_calls'], 0)
        self.assertFalse(result['harness_receipt']['live'])

    def test_callback_exception_and_malformed_result_are_fixed_failures(self):
        for provider, expected in [(Mock(side_effect=RuntimeError('PRIVATE_ERROR_BODY')), 'request_failed'),
                                   (Mock(return_value=[]), 'invalid_response')]:
            guard = adapter.ReservedQwenTurn(PROMPT, provider)
            with self.assertRaisesRegex(ValueError, 'provider_failed'):
                guard.complete(payload())
            with self.assertRaisesRegex(ValueError, 'already_attempted'):
                guard.complete(payload())
            provider.assert_called_once()
            self.assertEqual(guard.result['status'], expected)
            self.assertNotIn('PRIVATE_ERROR_BODY', json.dumps(guard.receipt))

    def test_loopback_returns_qwen_alias_and_rejects_repeated_request(self):
        guard = adapter.ReservedQwenTurn(PROMPT)
        server = server_for(guard, model=adapter.MODEL)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        sender = build_opener(ProxyHandler({})).open
        request = Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions', data=json.dumps(payload()).encode())
        try:
            with sender(request, timeout=2) as response:
                result = json.load(response)
            self.assertEqual(result['model'], adapter.MODEL)
            self.assertEqual(result['choices'][0]['message']['content'], adapter.FIXTURE)
            with self.assertRaises(HTTPError):
                sender(request, timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()

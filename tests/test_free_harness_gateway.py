import io
import json
import threading
import unittest
from email.message import Message
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from workbench.harnesses import free_gateway as gateway
from scripts.run_dsh_free_review import accepted_result


def request(**updates):
    value = {'model': gateway.MODEL, 'messages': [{'role': 'user', 'content': 'TASK-001 public review'}]}
    value.update(updates)
    return value


class Response(io.BytesIO):
    status = 200

    def __init__(self, message=None, finish='stop'):
        super().__init__(json.dumps({'choices': [{'finish_reason': finish,
            'message': message or {'role': 'assistant', 'content': 'Public review.'}}]}).encode())
        self.headers = Message()
        self.headers['Content-Type'] = 'application/json'

    def geturl(self):
        return gateway.ENDPOINT


class FreeHarnessGatewayTests(unittest.TestCase):
    def test_live_carries_no_user_credential_and_one_reservation(self):
        sender, reserve = Mock(return_value=Response()), Mock()
        guard = gateway.OneShot('TASK-001', live=True, transport=sender, reserve=reserve)
        self.assertEqual(guard.complete(request()), 'Public review.')
        with self.assertRaisesRegex(ValueError, 'already_attempted'):
            guard.complete(request())
        reserve.assert_called_once_with()
        sender.assert_called_once()
        outbound = sender.call_args.args[0]
        self.assertEqual(outbound.full_url, gateway.ENDPOINT)
        self.assertNotIn('Authorization', dict(outbound.header_items()))
        body = json.loads(outbound.data)
        self.assertEqual(body['model'], 'mimo-v2.5-free')
        self.assertFalse(body['stream'])
        self.assertEqual(body['max_tokens'], gateway.MAX_TOKENS)
        self.assertEqual(guard.receipt['upstream_requests'], 1)

    def test_paid_models_tools_and_scope_substitution_never_send(self):
        changes = [{'model': 'deepseek-v4-pro'}, {'tools': [{'type': 'function'}]},
                   {'messages': [{'role': 'tool', 'content': 'TASK-001'}]},
                   {'messages': [{'role': 'user', 'content': 'wrong task'}]},
                   {'messages': [{'role': 'user', 'content': 'TASK-001 sk-' + 's' * 20}]},
                   {'messages': [{'role': 'user', 'content': ['TASK-001']}]},
                   {'messages': [{'role': 'user', 'content': 'TASK-001', 'name': 'injected'}]}]
        for change in changes:
            with self.subTest(change=change):
                sender, reserve = Mock(), Mock()
                guard = gateway.OneShot('TASK-001', True, sender, reserve)
                with self.assertRaises(ValueError):
                    guard.complete(request(**change))
                sender.assert_not_called()
                reserve.assert_not_called()

    def test_failed_or_missing_reservation_prevents_outbound(self):
        sender = Mock()
        for reserve in (None, Mock(side_effect=FileExistsError('already reserved'))):
            guard = gateway.OneShot('TASK-001', True, sender, reserve)
            with self.assertRaises((ValueError, FileExistsError)):
                guard.complete(request())
            self.assertEqual(guard.receipt['upstream_requests'], 0)
        sender.assert_not_called()

    def test_rate_limit_is_not_retried_and_no_raw_error_leaks(self):
        sender = Mock(side_effect=HTTPError(gateway.ENDPOINT, 429, 'SECRET BODY', {}, None))
        guard = gateway.OneShot('TASK-001', True, sender, Mock())
        with self.assertRaisesRegex(ValueError, '^remote_http_429$'):
            guard.complete(request())
        with self.assertRaises(ValueError):
            guard.complete(request())
        sender.assert_called_once()
        self.assertNotIn('SECRET BODY', json.dumps(guard.receipt))
        self.assertFalse(guard.receipt['response_received'])

    def test_tool_output_and_incomplete_output_never_return_to_harness(self):
        for reply in (Response({'content': 'run this', 'tool_calls': [{'id': 'x'}]}),
                      Response(finish='length'), Response({'content': 'sk-' + 's' * 20})):
            guard = gateway.OneShot('TASK-001', True, Mock(return_value=reply), Mock())
            with self.assertRaises(ValueError):
                guard.complete(request())
            self.assertNotIn('output_text', guard.receipt)

    def test_actual_loopback_sse_fixture_and_second_request_rejected(self):
        guard = gateway.OneShot('TASK-001')
        server = gateway.server_for(guard)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:' + str(server.server_port) + '/v1/chat/completions'
        sender = build_opener(ProxyHandler({})).open
        try:
            body = json.dumps(request(stream=True)).encode()
            with sender(Request(url, data=body), timeout=2) as response:
                text = response.read().decode()
            self.assertIn('Protocol fixture: TASK-001', text)
            self.assertIn('data: [DONE]', text)
            with self.assertRaises(HTTPError):
                sender(Request(url, data=body), timeout=2)
            self.assertEqual(guard.receipt['upstream_requests'], 0)
            self.assertFalse(guard.receipt['response_received'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_malformed_json_shapes_leave_structured_failure(self):
        for value in ([], {'choices': [None]}, {'choices': [{'message': None}]},
                      {'choices': [{'message': [], 'finish_reason': 'stop'}]}):
            reply = Response()
            reply.seek(0)
            reply.truncate()
            reply.write(json.dumps(value).encode())
            reply.seek(0)
            guard = gateway.OneShot('TASK-001', True, Mock(return_value=reply), Mock())
            with self.assertRaises(ValueError):
                guard.complete(request())
            self.assertNotEqual(guard.receipt['status'], 'request_started')
            self.assertFalse(guard.receipt['response_received'])

    def test_runner_failure_is_not_success_even_after_good_response(self):
        result = {'status': 'response_received', 'harness_process': {'status': 'exited', 'returncode': 0}}
        self.assertTrue(accepted_result(result, True))
        for status in ('timeout', 'output_limit'):
            result['harness_process']['status'] = status
            self.assertFalse(accepted_result(result, True))


if __name__ == '__main__':
    unittest.main()

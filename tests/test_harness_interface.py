"""Exercise the actual loopback surface and its durable result boundary."""
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from workbench.service import Service
from workbench.server import make_server
from workbench.mcp_server import serve_stdio

ROOT = Path(__file__).resolve().parents[1]


class HarnessInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(ROOT, Path(self.tmp.name) / 'workbench.sqlite3')
        self.server = make_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.close()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        h = {'Content-Type': 'application/json'} if body is not None else {}
        h.update(headers or {})
        connection.request('POST' if body is not None else 'GET', path,
                           json.dumps(body) if body is not None else None, h)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw)

    def test_permission_from_server_only_and_run_saved(self):
        with patch.dict(os.environ, {'AUTONOMY_ALLOW_MODEL_CALLS': 'false'}):
            with patch('workbench.harnesses.HarnessRegistry.run', return_value={
                'harness_id': 'unreal', 'status': 'blocked_model_calls', 'output_text': ''}) as run:
                code, result = self.request('/api/harnesses/run', {'harness_id': 'unreal', 'prompt': 'private-question-marker'})
                self.assertEqual(code, 200)
                self.assertFalse(run.call_args.kwargs['allow_model_calls'])
                self.assertEqual(result['status'], 'blocked_model_calls')
                self.assertNotIn('private-question-marker', json.dumps(result))
                self.assertEqual(self.request('/api/harnesses/runs')[1]['items'][0]['id'], result['id'])
                self.assertTrue(self.service.audit()['valid'])
        with patch.dict(os.environ, {'AUTONOMY_ALLOW_MODEL_CALLS': 'true'}):
            with patch('workbench.harnesses.HarnessRegistry.run', return_value={
                'harness_id': 'unreal', 'status': 'completed', 'output_text': 'synthetic-answer'}) as run:
                self.assertEqual(self.request('/api/harnesses/run', {'harness_id': 'unreal', 'prompt': 'public question'})[0], 200)
                self.assertTrue(run.call_args.kwargs['allow_model_calls'])

    def test_rejects_path_environment_url_and_browser_permission_overrides(self):
        with patch('workbench.harnesses.HarnessRegistry.run') as run:
            for key, value in [('allow_model_calls', True), ('environment', {'OPENAI_API_KEY': 'x'}),
                               ('protocol_test_base_url', 'http://127.0.0.1:1234'), ('executable', '/bin/sh'),
                               ('workspace', '/tmp')]:
                code, _ = self.request('/api/harnesses/run', {'harness_id': 'unreal', 'prompt': 'x', key: value})
                self.assertEqual(code, 400, key)
            for timeout in (True, 0, 61, 1.1, '2'):
                self.assertEqual(self.request('/api/harnesses/run', {
                    'harness_id': 'unreal', 'prompt': 'x', 'timeout_seconds': timeout})[0], 400)
            run.assert_not_called()

    def test_cross_origin_block_and_single_concurrent_job(self):
        with patch('workbench.harnesses.HarnessRegistry.run') as run:
            self.assertEqual(self.request('/api/harnesses/run', {'harness_id': 'unreal', 'prompt': 'x'},
                {'Origin': 'https://external.invalid'})[0], 403)
            self.service._harness_slot.acquire()
            try:
                self.assertEqual(self.request('/api/harnesses/run', {'harness_id': 'unreal', 'prompt': 'x'})[0], 429)
            finally:
                self.service._harness_slot.release()
            run.assert_not_called()

    def test_read_status_does_not_execute_or_expose_credentials(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'do-not-output-this-secret', 'AUTONOMY_ALLOW_MODEL_CALLS': 'false'}):
            code, result = self.request('/api/harnesses')
            self.assertEqual(code, 200)
            self.assertFalse(result['model_calls_enabled'])
            self.assertNotIn('do-not-output-this-secret', json.dumps(result))
            self.assertIn('harnesses', result)

    def test_mcp_status_and_override_rejection(self):
        frames = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'harness_status', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'run_harness_task', 'arguments': {
                'harness_id': 'unreal', 'prompt': 'x', 'allow_model_calls': True}}},
        ]
        out = io.StringIO()
        with patch('workbench.harnesses.HarnessRegistry.run') as run:
            serve_stdio(self.service, io.StringIO('\n'.join(json.dumps(f) for f in frames) + '\n'), out)
            replies = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertIn('harnesses', json.loads(replies[1]['result']['content'][0]['text']))
            self.assertTrue(replies[2]['result']['isError'])
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()

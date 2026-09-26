"""Pure/fake Hermes admission tests; run only in cloud CI, never call a model."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from scripts import bootstrap_hermes as bootstrap
from scripts import hermes_no_tools as profile


class FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools = []
        self.valid_tool_names = set()
        self.compression_enabled = False
        self.client = SimpleNamespace(max_retries=2)
        self._client_kwargs = {}
        self.closed = False
        self.prompt = None
        self.result = {'completed': True, 'final_response': '  Exact answer.\n',
                       'interrupted': False, 'partial': False}

    def run_conversation(self, prompt):
        self.prompt = prompt
        return self.result

    def close(self):
        self.closed = True


class HermesProfileTests(unittest.TestCase):
    def test_plan_has_no_side_effects_and_never_invokes_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = mock.Mock(side_effect=AssertionError('must not execute'))
            downloader = mock.Mock(side_effect=AssertionError('must not download'))
            result = bootstrap.bootstrap(root=root, runner=runner, downloader=downloader)
            self.assertEqual(result['status'], 'planned')
            self.assertEqual(result['model_calls'], 0)
            self.assertTrue(result['python'].endswith('venv/bin/python')
                            or result['python'].endswith('venv\\bin\\python'))
            self.assertFalse((root / 'runtime').exists())
            runner.assert_not_called()
            downloader.assert_not_called()

    def test_execute_requires_cloud_before_writing_or_installing(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            with self.assertRaisesRegex(bootstrap.HermesError, 'cloud_execution_required'):
                bootstrap.bootstrap(root=root, execute=True)
            self.assertFalse((root / 'runtime').exists())

    def test_source_pins_cover_exact_bytes_and_fail_on_tamper(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            file = source / 'run_agent.py'
            file.write_bytes(b'# inert fixture\n')
            manifest = {'files': {'run_agent.py': hashlib.sha256(file.read_bytes()).hexdigest()}}
            bootstrap.verify_source(source, manifest)
            file.write_bytes(b'# different fixture\n')
            with self.assertRaisesRegex(bootstrap.HermesError, 'source_digest_mismatch'):
                bootstrap.verify_source(source, manifest)

    def test_manifest_rejects_foreign_repository_and_install_inference(self):
        original = bootstrap.load_manifest()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'manifest.json'
            for field, value in [('repository', 'https://example.com/evil.git'),
                                 ('install_inference_calls', True), ('commit', '0' * 40)]:
                changed = copy.deepcopy(original)
                changed[field] = value
                path.write_text(json.dumps(changed), encoding='utf-8')
                with self.subTest(field=field), self.assertRaises(bootstrap.HermesError):
                    bootstrap.load_manifest(path)

    def test_loopback_endpoint_and_paths_are_narrow(self):
        self.assertEqual(profile.loopback_url('http://127.0.0.1:8765/v1'), 8765)
        for value in ('https://127.0.0.1:8765/v1', 'http://localhost:8765/v1',
                      'http://127.0.0.1:8765/v1?secret=x', 'http://127.0.0.1:70000/v1',
                      'http://127.0.0.1:80/v1', 'http://evil.example:8765/v1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                profile.loopback_url(value)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(bootstrap.HermesError):
                bootstrap.install_path('../outside', Path(temp))

    def test_profile_disables_auxiliary_defaults_and_credentials_do_not_leak(self):
        config = profile.profile('http://127.0.0.1:8765/v1')
        self.assertEqual(config['model']['default'], profile.MODEL)
        self.assertEqual(config['fallback_providers'], [])
        self.assertEqual(config['platform_toolsets']['cli'], [])
        self.assertFalse(config['compression']['enabled'])
        self.assertFalse(config['auxiliary']['title_generation']['model_upgrade_enabled'])
        self.assertFalse(config['auxiliary']['background_review']['enabled'])
        self.assertFalse(config['security']['allow_lazy_installs'])
        self.assertFalse(config['agent']['environment_probe'])
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {
                'OPENAI_API_KEY': 'secret', 'HERMES_LAZY_INSTALL_TARGET': '/escape',
                'HTTPS_PROXY': 'http://secret', 'PYTHONPATH': '/injected'}):
            home = Path(temp) / 'fresh'
            env = profile.fresh_environment(home)
            for key in ('OPENAI_API_KEY', 'HERMES_LAZY_INSTALL_TARGET', 'HTTPS_PROXY', 'PYTHONPATH'):
                self.assertNotIn(key, env)
            self.assertEqual(env['HERMES_HOME'], str(home))
            with self.assertRaisesRegex(bootstrap.HermesError, 'fresh_home_required'):
                profile.fresh_environment(home)

    def test_sdk_receives_exact_prompt_and_keeps_exact_response(self):
        agents = []

        def factory(**kwargs):
            agent = FakeAgent(**kwargs)
            agents.append(agent)
            return agent

        prompt = 'Публичный вопрос.\nБез нормализации. '
        result = profile.run_sdk(prompt, 'http://127.0.0.1:8765/v1', Path('/inert'), factory)
        agent = agents[0]
        self.assertEqual(agent.prompt, prompt)
        self.assertEqual(result['text'], '  Exact answer.\n')
        self.assertEqual(result['status'], 'response_received')
        self.assertEqual(agent.kwargs['enabled_toolsets'], [])
        self.assertEqual(agent.kwargs['max_iterations'], 1)
        self.assertTrue(agent.kwargs['skip_memory'])
        self.assertTrue(agent.kwargs['skip_background_review'])
        self.assertTrue(agent.kwargs['skip_context_files'])
        self.assertEqual(agent.client.max_retries, 0)
        self.assertEqual(agent._client_kwargs['max_retries'], 0)
        self.assertIsNone(agent.kwargs['session_db'])
        self.assertIsNone(agent.kwargs['fallback_model'])
        self.assertTrue(agent.closed)

    def test_unexpected_tools_or_compression_stop_before_model(self):
        for field, value in [('tools', [{'function': {'name': 'terminal'}}]),
                             ('compression_enabled', True)]:
            agent = FakeAgent()
            setattr(agent, field, value)
            with self.subTest(field=field), self.assertRaises(bootstrap.HermesError):
                profile.run_sdk('public', 'http://127.0.0.1:8765/v1', Path('/inert'),
                                lambda **kwargs: agent)
            self.assertIsNone(agent.prompt)
            self.assertTrue(agent.closed)

    def test_partial_empty_and_oversized_answers_are_not_success(self):
        for result in ({'completed': False, 'final_response': 'partial'},
                       {'completed': True, 'partial': True, 'final_response': 'partial'},
                       {'completed': True, 'final_response': ''},
                       {'completed': True, 'final_response': 'a' * (profile.MAX_TEXT + 1)}):
            agent = FakeAgent()
            agent.result = result
            with self.subTest(result_keys=list(result)), self.assertRaises(bootstrap.HermesError):
                profile.run_sdk('public', 'http://127.0.0.1:8765/v1', Path('/inert'),
                                lambda **kwargs: agent)
            self.assertTrue(agent.closed)

    def test_python_io_guard_allows_only_one_loopback_destination(self):
        guard = profile.audit_guard(8765)
        guard('socket.connect', (None, ('127.0.0.1', 8765)))
        guard('socket.getaddrinfo', ('127.0.0.1', 8765, 0, 0, 0))
        for event, args in [('socket.connect', (None, ('1.1.1.1', 443))),
                            ('socket.connect', (None, ('127.0.0.1', 9999))),
                            ('socket.getaddrinfo', ('api.example.com', 443, 0, 0, 0)),
                            ('subprocess.Popen', ('sh',)), ('os.system', ('echo x',))]:
            with self.subTest(event=event), self.assertRaises(PermissionError):
                guard(event, args)

    def test_prompt_bytes_and_log_memory_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'prompt.txt'
            path.write_text('я' * 4000, encoding='utf-8')
            self.assertEqual(len(profile.read_prompt(path)), 4000)
            path.write_text('я' * 4001, encoding='utf-8')
            with self.assertRaises(bootstrap.HermesError):
                profile.read_prompt(path)
            path.write_bytes(b'\xff')
            with self.assertRaisesRegex(bootstrap.HermesError, 'invalid_prompt_encoding'):
                profile.read_prompt(path)
        sink = profile.BoundedSink()
        sink.write('x' * 65536)
        with self.assertRaisesRegex(bootstrap.HermesError, 'sdk_log_limit'):
            sink.write('x')

    def test_frozen_sync_and_editable_build_use_their_supported_uv_interfaces(self):
        sync, editable = bootstrap.install_commands('/uv', '/source', '/venv/bin/python',
                                                     '/constraints.txt', '/python')
        self.assertEqual(sync[1], 'sync')
        self.assertIn('--frozen', sync)
        self.assertIn('--no-install-project', sync)
        self.assertNotIn('--build-constraint', sync)
        self.assertEqual(editable[1:3], ['pip', 'install'])
        self.assertIn('--no-deps', editable)
        self.assertEqual(editable[editable.index('--build-constraint') + 1], '/constraints.txt')
        self.assertEqual(editable[-2:], ['-e', '/source'])
        self.assertNotIn('--extra', sync + editable)

    def test_diagnostic_never_exposes_exception_message_or_source_line(self):
        try:
            raise RuntimeError('sk-secret-value private prompt /home/private/account')
        except RuntimeError as exc:
            result = profile.safe_failure(exc)
        serialized = json.dumps(result)
        self.assertEqual(result['failure_kind'], 'RuntimeError')
        self.assertEqual(result['reason'], 'sdk_failed')
        self.assertTrue(result['frames'])
        self.assertTrue(all(set(frame) == {'file', 'line'} for frame in result['frames']))
        self.assertNotIn('sk-secret-value', serialized)
        self.assertNotIn('private prompt', serialized)
        self.assertNotIn('/home/private', serialized)
    def test_uv_extraction_never_uses_archive_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = root / 'uv.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('../escape', b'ignored')
                archive.writestr('uv-0.12.19.data/scripts/uv', b'inert-native-fixture')
            bootstrap.extract_uv(wheel, root / 'uv')
            self.assertEqual((root / 'uv').read_bytes(), b'inert-native-fixture')
            self.assertFalse((root.parent / 'escape').exists())
            with zipfile.ZipFile(wheel, 'w') as archive:
                link = zipfile.ZipInfo('uv-0.12.19.data/scripts/uv')
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, b'/outside')
            with self.assertRaisesRegex(bootstrap.HermesError, 'uv_archive_rejected'):
                bootstrap.extract_uv(wheel, root / 'other')


if __name__ == '__main__':
    unittest.main()

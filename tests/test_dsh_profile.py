"""Pure profile construction checks; execute in authorized cloud CI only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import dsh_profile as profile


class DshProfileTests(unittest.TestCase):
    def options(self):
        return {'port': 12345, 'prompt': 'Review the public evidence contract.',
                'session_root': str(Path(tempfile.gettempdir()).resolve() / 'srf-sessions')}

    def test_no_inherited_bundle_or_executable_tool_producer(self):
        patch = profile.profile_patch(**self.options())
        rows = patch[0]['insert']
        self.assertEqual(len(rows), 12)
        self.assertEqual({r['id']: r['name'] for r in rows}, profile.PLUGIN_NAMES)
        self.assertEqual(next(r for r in rows if r['id'] == 'tools')['config'], {'mode': 'native'})
        forbidden = ('tool-', 'bash', 'pwsh', 'subagent', 'web-', 'telemetry', 'compaction', 'title', 'retry', 'mcp')
        self.assertFalse(any(fragment in row['name'] for row in rows for fragment in forbidden))

    def test_endpoint_and_request_cap_are_explicit_not_catalog_fallback(self):
        rows = {r['id']: r for r in profile.profile_patch(**self.options())[0]['insert']}
        route = rows['llm-pi-ai']['config']['providers'][profile.PROVIDER]
        self.assertEqual(route['baseURL'], 'http://127.0.0.1:12345/v1')
        self.assertEqual(route['models'][0]['id'], 'mimo-v2.5-free')
        self.assertEqual(route['models'][0]['maxTokens'], 1024)
        self.assertEqual(route['retryPolicy'], {'mode': 'normal', 'maxRetries': 0})
        self.assertNotIn('defaultMaxTokens', route)
        self.assertEqual(rows['session-persistence-jsonl']['config']['compression'], 'none')

    def test_untrusted_patch_cannot_add_tools_change_target_or_enable_retry(self):
        for change in ('plugin', 'endpoint', 'retry', 'mode', 'bool_cap'):
            with self.subTest(change=change):
                options = self.options()
                patch = profile.profile_patch(**options)
                rows = {r['id']: r for r in patch[0]['insert']}
                route = rows['llm-pi-ai']['config']['providers'][profile.PROVIDER]
                if change == 'plugin':
                    patch[0]['insert'].append({'id': 'shell', 'name': '@deepseek-ai/dsh-tool-bash'})
                elif change == 'endpoint':
                    route['baseURL'] = 'https://api.deepseek.com'
                elif change == 'retry':
                    route['retryPolicy']['maxRetries'] = 1
                elif change == 'mode':
                    rows['tools']['config']['mode'] = 'ptc'
                else:
                    route['models'][0]['maxTokens'] = True
                with self.assertRaises(ValueError):
                    profile.validate_patch(patch, **options)

    def test_bounds_and_malformed_prompt_fail_before_filesystem_changes(self):
        for override in ({'port': True}, {'port': 0}, {'port': 65536}, {'max_tokens': True},
                         {'max_tokens': 1025}, {'prompt': ' '}, {'prompt': '\ud800'},
                         {'prompt': 'x' * 8193}, {'prompt': 'x\x00y'}, {'session_root': 'relative'}):
            with self.subTest(override=repr(override)):
                options = self.options() | override
                with self.assertRaises(ValueError):
                    profile.profile_patch(**options)

    def test_config_data_cannot_inject_yaml_rows_or_shell_arguments(self):
        options = self.options() | {'prompt': 'Text\n- insert: [shell]\n!!js process.exit() $(touch fake)'}
        patch = profile.profile_patch(**options)
        decoded = json.loads(json.dumps(patch))
        profile.validate_patch(decoded, **options)
        self.assertEqual(len(decoded[0]['insert']), 12)
        self.assertEqual(decoded[0]['insert'][-1]['config']['task'], options['prompt'])
        self.assertEqual(profile.command('node', '/tmp/dsh bin.js'),
                         ['node', '/tmp/dsh bin.js', '--profile', 'srf-review'])

    def test_new_home_has_no_bundles_credentials_or_home_override(self):
        with tempfile.TemporaryDirectory() as parent:
            home = Path(parent) / 'new-home'
            receipt = profile.prepare_profile(home, port=12345, prompt='Review only supplied text.')
            profile_dir = home / 'profiles' / profile.PROFILE
            manifest = json.loads((profile_dir / 'package.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['dsh']['profile']['bundles'], [])
            self.assertEqual(manifest['dependencies'], {})
            self.assertEqual(set(receipt['environment']),
                             {'DSH_HOME', 'DSH_TELEMETRY_DISABLED', profile.LOOPBACK_ENV_KEY})
            self.assertTrue(Path(receipt['cwd']).is_dir())
            self.assertFalse((home / 'cordis.patch.yml').exists())
            self.assertFalse((home / '.credentials.yaml').exists())
            self.assertFalse((Path(receipt['cwd']) / '.env').exists())

    def test_existing_home_is_refused_and_preserved(self):
        with tempfile.TemporaryDirectory() as parent:
            home = Path(parent) / 'existing'
            home.mkdir()
            sentinel = home / 'cordis.patch.yml'
            sentinel.write_text('existing trusted file', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                profile.prepare_profile(home, port=12345, prompt='A public question')
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'existing trusted file')
            self.assertFalse((home / 'profiles').exists())

    def test_inspection_command_does_not_submit_task(self):
        self.assertEqual(profile.command('node', '/tmp/bin.js', inspect_only=True),
                         ['node', '/tmp/bin.js', '--profile', 'srf-review', '--dump-config'])
        with self.assertRaises(ValueError):
            profile.command('node', '/tmp/bin.js', inspect_only=1)


if __name__ == '__main__':
    unittest.main()

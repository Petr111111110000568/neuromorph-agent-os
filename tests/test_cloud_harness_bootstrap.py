"""Fake installer tests plus bounded subprocess checks; execute in cloud only."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts import bootstrap_cloud_harnesses as bootstrap


class CloudHarnessBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.config = bootstrap.read_json(bootstrap.CONFIG, 32 * 1024)
        self.config['packages'] = self.config['packages'][:1]
        self.package = self.config['packages'][0]
        self.archive = self.make_archive()
        self.package['integrity'] = 'sha512-' + base64.b64encode(hashlib.sha512(self.archive).digest()).decode('ascii')
        self.calls = []

    def make_archive(self, hostile=None):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            metadata = json.dumps({'name': self.package['name'], 'version': self.package['version']}).encode()
            member = tarfile.TarInfo('package/package.json')
            member.size = len(metadata)
            archive.addfile(member, io.BytesIO(metadata))
            if hostile:
                member = tarfile.TarInfo(hostile)
                if hostile.endswith('link'):
                    member.type = tarfile.SYMTYPE
                    member.linkname = '/outside'
                else:
                    member.size = 1
                archive.addfile(member, io.BytesIO(b'x') if member.isfile() else None)
        return buffer.getvalue()

    def fetcher(self, package, destination, timeout):
        self.assertGreater(timeout, 0)
        destination.write_bytes(self.archive)
        return len(self.archive)

    def runner(self, argv, cwd, env, timeout):
        self.calls.append((argv, cwd, env))
        self.assertGreater(timeout, 0)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('NODE_OPTIONS', env)
        if argv[1:] == ['--version']:
            return {'status': 'ok', 'output': 'v24.8.0', 'returncode': 0}
        if len(argv) > 1 and argv[1] == 'install':
            self.assertIn('--ignore-scripts', argv)
            self.assertIn('--no-audit', argv)
            self.assertIn('--no-fund', argv)
            self.assertNotIn('-g', argv)
            prefix = Path(argv[argv.index('--prefix') + 1])
            entry = prefix / 'node_modules' / self.package['name'] / self.package['entry']
            entry.parent.mkdir(parents=True)
            entry.write_text('// inert fixture; never executed\n', encoding='utf-8')
            lock = {'lockfileVersion': 3, 'packages': {'node_modules/' + self.package['name']:
                    {'version': self.package['version'], 'integrity': self.package['integrity']}}}
            (prefix / 'package-lock.json').write_text(json.dumps(lock), encoding='utf-8')
            return {'status': 'ok', 'output': 'fake install', 'returncode': 0}
        self.assertIn(argv[-1], ('--version', '--help'))
        return {'status': 'ok', 'output': self.package['version'] if argv[-1] == '--version' else 'Usage: dsh [options]',
                'returncode': 0}

    def execute(self, root, runner=None, environment=None):
        with mock.patch.object(bootstrap.platform, 'system', return_value='Linux'), \
             mock.patch.object(bootstrap.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(bootstrap.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name):
            return bootstrap.bootstrap(self.config, 'runtime/cloud-harnesses', True, repository_root=root,
                    environment=environment or {'GITHUB_ACTIONS': 'true', 'OPENAI_API_KEY': 'private-value',
                                                'NODE_OPTIONS': '--require=evil.js'},
                    runner=runner or self.runner, fetcher=self.fetcher)

    def test_plan_has_no_runtime_network_or_filesystem_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forbidden = mock.Mock(side_effect=AssertionError('plan executed'))
            receipt = bootstrap.bootstrap(self.config, 'runtime/cloud-harnesses', repository_root=root,
                                          runner=forbidden, fetcher=forbidden)
            self.assertEqual(receipt['status'], 'plan_only')
            self.assertEqual(receipt['model_calls'], 0)
            self.assertEqual(list(root.iterdir()), [])
            forbidden.assert_not_called()

    def test_real_metadata_config_has_four_exact_packages_no_model_authority(self):
        config = bootstrap.validate_config(bootstrap.read_json(bootstrap.CONFIG, 32 * 1024))
        self.assertEqual({p['id'] for p in config['packages']}, set(bootstrap.ALLOWED))
        self.assertFalse(config['model_calls_allowed'])
        changed = copy.deepcopy(config)
        changed['model_calls_allowed'] = True
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.validate_config(changed)
        changed = copy.deepcopy(config)
        changed['packages'][0]['tarball'] = 'https://example.com/redirect.tgz'
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.validate_config(changed)
        changed = copy.deepcopy(config)
        changed['packages'][0]['entry'] = '../../private'
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.validate_config(changed)

    def test_execute_requires_cloud_and_rejects_path_escape_and_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(bootstrap.BootstrapError, 'cloud_execution_required'):
                bootstrap.bootstrap(self.config, 'runtime/cloud-harnesses', True, repository_root=root,
                                    environment={}, runner=self.runner, fetcher=self.fetcher)
            for path in ('../cloud-harnesses', 'runtime/../cloud-harnesses', '/tmp/cloud-harnesses'):
                with self.subTest(path=path), self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.output_path(path, root)
            output = root / 'runtime' / 'cloud-harnesses'
            output.mkdir(parents=True)
            (output / 'keep.txt').write_text('keep', encoding='utf-8')
            with self.assertRaisesRegex(bootstrap.BootstrapError, 'fresh_output_directory_required'):
                self.execute(root)
            self.assertEqual((output / 'keep.txt').read_text(encoding='utf-8'), 'keep')
            self.assertEqual(self.calls, [])

    def test_success_records_pin_lock_entry_and_only_help_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = self.execute(root)
            self.assertEqual(receipt['status'], 'completed')
            item = receipt['packages'][0]
            self.assertEqual(item['status'], 'installed_smoke_passed')
            self.assertEqual([r['flag'] for r in item['smoke']], ['--version', '--help'])
            prefix = root / 'runtime/cloud-harnesses/packages/dsh'
            lock_bytes = (prefix / 'package-lock.json').read_bytes()
            self.assertEqual(item['lock_sha256'], hashlib.sha256(lock_bytes).hexdigest())
            self.assertEqual(Path(item['entry_path']), prefix / 'node_modules/@deepseek-ai/dsh/lib/bin.js')
            stored = json.loads((root / 'runtime/cloud-harnesses/receipt.json').read_text(encoding='utf-8'))
            self.assertEqual(stored, receipt)
            self.assertEqual(stored['model_calls'], 0)
            self.assertNotIn('private-value', json.dumps(stored))
            self.assertNotIn('evil.js', json.dumps(stored))
            self.assertEqual(len(self.calls), 4)  # node check, install, version, help.

    def test_colab_marker_accepted_and_old_or_odd_node_rejected(self):
        for node_version in ('v20.20.0', 'v22.18.0', 'v23.0.0'):
            with self.subTest(version=node_version), tempfile.TemporaryDirectory() as directory:
                runner = mock.Mock(return_value={'status': 'ok', 'output': node_version, 'returncode': 0})
                receipt = self.execute(Path(directory), runner=runner, environment={'COLAB_RELEASE_TAG': 'test'})
                self.assertEqual(receipt['status'], 'failed')
                self.assertEqual(receipt['reason'], 'node_version_unsupported')
                self.assertEqual(runner.call_count, 1)

    def test_archive_tamper_stops_before_npm_and_preserves_failure_receipt(self):
        self.archive += b'tamper'
        with tempfile.TemporaryDirectory() as directory:
            receipt = self.execute(Path(directory))
            self.assertEqual(receipt['status'], 'completed_with_failures')
            self.assertEqual(receipt['packages'][0]['reason'], 'archive_integrity_mismatch')
            self.assertEqual(len(self.calls), 1)

    def test_archive_symlink_and_traversal_are_rejected_even_with_matching_digest(self):
        for hostile in ('package/../../outside', 'package/unsafe-link', '/outside'):
            with self.subTest(member=hostile), tempfile.TemporaryDirectory() as directory:
                raw = self.make_archive(hostile)
                self.package['integrity'] = 'sha512-' + base64.b64encode(hashlib.sha512(raw).digest()).decode()
                path = Path(directory) / 'package.tgz'
                path.write_bytes(raw)
                with self.assertRaisesRegex(bootstrap.BootstrapError, 'archive_path_rejected'):
                    bootstrap.check_archive(path, self.package)

    def test_existing_ancestor_credentials_and_symlink_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('PRIVATE=do-not-read', encoding='utf-8')
            with self.assertRaisesRegex(bootstrap.BootstrapError, 'ancestor_config_rejected'):
                self.execute(root)
            self.assertEqual(self.calls, [])
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            try:
                (root / 'runtime').symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                if getattr(exc, 'winerror', None) == 1314:
                    self.skipTest('Windows symlink privilege unavailable')
                raise
            with self.assertRaisesRegex(bootstrap.BootstrapError, 'symlink_path_rejected'):
                bootstrap.output_path('runtime/cloud-harnesses', root)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_failed_install_and_false_successful_version_do_not_claim_tested(self):
        for fault in ('install', 'version'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                def runner(argv, cwd, env, timeout):
                    if fault == 'install' and argv[1] == 'install':
                        return {'status': 'timeout', 'output': '', 'returncode': -9}
                    if fault == 'version' and len(argv) == 3 and argv[-1] == '--version':
                        return {'status': 'ok', 'output': '0.0.0', 'returncode': 0}
                    return self.runner(argv, cwd, env, timeout)
                receipt = self.execute(Path(directory), runner=runner)
                self.assertEqual(receipt['status'], 'completed_with_failures')
                self.assertEqual(receipt['packages'][0]['status'], 'failed')
                self.assertEqual(receipt['packages'][0]['reason'], 'npm_install_failed' if fault == 'install' else 'smoke_version_mismatch')

    @unittest.skipUnless(os.name == 'posix', 'POSIX cloud process-group test')
    def test_subprocess_output_and_timeout_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONIOENCODING': 'utf-8'}
            output = bootstrap.run_bounded([sys.executable, '-c', 'import sys; sys.stdout.write("x" * 1000000)'],
                                           directory, env, 3)
            self.assertEqual(output['status'], 'output_limit')
            self.assertLessEqual(output['output_bytes'], bootstrap.MAX_OUTPUT)
            self.assertLessEqual(len(output['output']), 8192)
            timed = bootstrap.run_bounded([sys.executable, '-c', 'import time; time.sleep(5)'], directory, env, 0.1)
            self.assertEqual(timed['status'], 'timeout')


if __name__ == '__main__':
    unittest.main()

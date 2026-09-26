import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'scripts/sync_research_storage.py'
SPEC = importlib.util.spec_from_file_location('sync_research_storage_tested', MODULE)
storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage)
COMMIT, PARENT, NEXT = ('a' * 40, 'b' * 40, 'c' * 40)
TOKEN = 'hf_' + 'A' * 32


def sample(commit=COMMIT, source_ref='main'):
    return storage.make_snapshot(commit, source_ref, {
        'README.md': b'Public research project.\n',
        'docs/research/model.tex': b'\\[ y = x^2 \\]\n',
        'workbench/experiments/example.py': b'def identity(value):\n    return value\n',
    })


class FakeHF:
    def __init__(self, old=None):
        self.old = old
        self.calls = []
        self.mode = 'regular'
        self.ignored = False
        self.denied = None
        self.private = False

    def request(self, method, path, body=None, content_type='application/json'):
        self.calls.append((method, path, body, content_type))
        if self.denied and path.endswith(self.denied[0]):
            raise storage.StorageError('hf_http_error', self.denied[1])
        if path == storage.API + '/revision/main':
            return {'sha': PARENT, 'private': self.private}
        if '/raw/' in path:
            if self.old is None:
                raise storage.StorageError('hf_http_error', 404)
            return self.old
        if path == storage.API + '/preupload/main':
            return {'files': [{'path': entry['path'], 'uploadMode': self.mode, 'shouldIgnore': self.ignored}
                              for entry in json.loads(body)['files']]}
        if path == storage.API + '/commit/main':
            return {'commitOid': NEXT, 'commitUrl': 'https://huggingface.co/datasets/' + storage.DATASET + '/commit/' + NEXT}
        raise AssertionError((method, path))


class StorageSnapshotTests(unittest.TestCase):
    def test_only_reviewed_synthetic_evidence_pack_is_exported(self):
        root = 'data/experiments/evidence_memory/'
        files = {root + name: b'Public synthetic fixture.\n' for name in
                 ('sources.json', 'registry.json', 'cases.json', 'manifest.json', 'LICENSE.md')}
        snapshot = storage.make_snapshot(COMMIT, 'main', files)
        self.assertEqual(snapshot['manifest']['source_file_count'], len(files))
        for path, raw in files.items():
            self.assertEqual(snapshot['files']['snapshots/main/files/' + path], raw)
        for path in (root + 'credentials.json', root + 'private/cases.json',
                     'data/experiments/another_project/cases.json',
                     'runtime/evidence-memory/report.json'):
            with self.subTest(path=path):
                with self.assertRaises(storage.StorageError):
                    storage.make_snapshot(COMMIT, 'main', {path: b'private fixture'})

    def test_allowlist_includes_math_research_and_experiments(self):
        allowed = ('README.md', 'docs/research/x.md', 'docs/research/x.tex',
                   'docs/research-2026-09-25/summary.md', 'docs/contributions/continuous-state.json',
                   'docs/CORTICAL_MODEL_RU.md', 'docs/CORTICAL_RESEARCH_RU.md',
                   'workbench/experiments/continuous_candidate.py')
        for path in allowed:
            with self.subTest(path=path):
                self.assertTrue(storage.allowed_path(path))

    def test_private_runtime_workflow_and_traversal_paths_are_rejected(self):
        forbidden = ('../README.md', '/README.md', 'docs/../README.md', 'docs/research//x.md',
                     '.env', 'runtime/credentials.json', '.github/workflows/evil.yml',
                     'workbench/autonomy/continuous.py', 'workbench/experiments/__init__.py',
                     'docs/research/key.pem', 'docs/research/nested/secret.md')
        for path in forbidden:
            with self.subTest(path=path):
                self.assertFalse(storage.allowed_path(path))
                with self.assertRaises(storage.StorageError):
                    storage.make_snapshot(COMMIT, 'main', {path: b'data'})

    def test_manifest_is_deterministic_and_content_digest_ignores_unrelated_commit(self):
        first, repeated = sample(), sample('d' * 40)
        self.assertEqual(first['manifest']['snapshot_sha256'], repeated['manifest']['snapshot_sha256'])
        self.assertNotEqual(first['manifest']['source_commit'], repeated['manifest']['source_commit'])
        self.assertEqual(first, sample())
        self.assertFalse(first['manifest']['generated_code_executed'])
        self.assertTrue(all(path.startswith('snapshots/main/') for path in first['files']))
        self.assertTrue(all(path.startswith('snapshots/continuous/') for path in sample(source_ref='autonomy/continuous')['files']))

    def test_count_file_and_total_size_limits(self):
        with self.assertRaises(storage.StorageError):
            storage.make_snapshot(COMMIT, 'main', {'README.md': b'x' * (storage.MAX_FILE_BYTES + 1)})
        with self.assertRaises(storage.StorageError):
            storage.make_snapshot(COMMIT, 'main', {f'docs/research/r{n}.md': b'x' for n in range(65)})
        with self.assertRaises(storage.StorageError):
            storage.make_snapshot(COMMIT, 'main', {f'docs/research/r{n}.md': b'x' * storage.MAX_FILE_BYTES for n in range(21)})

    def test_secret_and_binary_rejected_without_reflecting_secret(self):
        for raw in (TOKEN.encode(), b'-----BEGIN OPENSSH PRIVATE KEY-----', b'binary\0value', b'\xff', b'custom-private-value'):
            with self.subTest(length=len(raw)):
                with self.assertRaises(storage.StorageError) as result:
                    storage.make_snapshot(COMMIT, 'main', {'README.md': raw}, ('custom-private-value',))
                self.assertNotIn(TOKEN, str(result.exception))
                self.assertNotIn('custom-private-value', str(result.exception))

    def test_collect_uses_verified_git_blobs_not_working_files(self):
        raw = b'Committed public content.\n'
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        calls = []

        def reader(repo, args, cap):
            calls.append(args)
            if args[:2] == ['config', '--get']:
                return storage.SOURCE_URL.encode() + b'.git\n'
            if args[0] == 'rev-parse':
                return COMMIT.encode() + b'\n'
            if args[0] == 'ls-tree':
                return ('100644 blob ' + blob + ' ' + str(len(raw)) + '\tREADME.md\0').encode()
            if args[:2] == ['cat-file', 'blob']:
                return raw
            raise AssertionError(args)

        snapshot = storage.collect_snapshot(Path('nonexistent-working-tree'), 'main', reader=reader)
        self.assertEqual(snapshot['files']['snapshots/main/files/README.md'], raw)
        self.assertEqual(calls[-1], ['cat-file', 'blob', blob])

    def test_collect_rejects_symlink_and_wrong_blob_hash(self):
        def reader(mode, content):
            def fake(repo, args, cap):
                if args[0] == 'config':
                    return storage.SOURCE_URL.encode()
                if args[0] == 'rev-parse':
                    return COMMIT.encode()
                if args[0] == 'ls-tree':
                    return (mode + ' blob ' + 'd' * 40 + ' 4\tREADME.md\0').encode()
                return content
            return fake
        for mode, content in [('120000', b'file'), ('100644', b'file')]:
            with self.assertRaises(storage.StorageError):
                storage.collect_snapshot(Path('.'), 'main', reader=reader(mode, content))


class StorageUploadTests(unittest.TestCase):
    def test_absent_credentials_do_not_construct_client_or_issue_http(self):
        with patch.object(storage, 'HFClient') as client:
            receipt = storage.sync_snapshot(sample(), '')
        client.assert_not_called()
        self.assertEqual(receipt['status'], 'credentials_required')
        self.assertFalse(receipt['uploaded'])

    def test_unchanged_snapshot_issues_only_gets(self):
        previous = sample()
        api = FakeHF(previous['manifest'])
        receipt = storage.sync_snapshot(sample('d' * 40), TOKEN, api)
        self.assertEqual(receipt['status'], 'unchanged')
        self.assertEqual(receipt['archived_source_commit'], COMMIT)
        self.assertTrue(all(call[0] == 'GET' for call in api.calls))

    def test_single_commit_uses_cas_and_only_fixed_snapshot_paths(self):
        snapshot = sample()
        api = FakeHF()
        receipt = storage.sync_snapshot(snapshot, TOKEN, api)
        self.assertEqual(receipt['status'], 'synchronized')
        self.assertEqual(receipt['hf_commit'], NEXT)
        commits = [call for call in api.calls if call[1].endswith('/commit/main')]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0][3], 'application/x-ndjson')
        rows = [json.loads(line) for line in commits[0][2].splitlines()]
        self.assertEqual(rows[0]['value']['parentCommit'], PARENT)
        decoded = {row['value']['path']: base64.b64decode(row['value']['content']) for row in rows[1:]}
        self.assertEqual(decoded, snapshot['files'])
        self.assertNotIn(TOKEN, commits[0][2].decode())
        self.assertNotIn(TOKEN, json.dumps(receipt))

    def test_lfs_or_ignored_files_stop_before_commit(self):
        for mode, ignored in [('lfs', False), ('regular', True)]:
            api = FakeHF()
            api.mode, api.ignored = mode, ignored
            receipt = storage.sync_snapshot(sample(), TOKEN, api)
            self.assertEqual(receipt['reason'], 'regular_upload_contract_required')
            self.assertFalse(any(call[1].endswith('/commit/main') for call in api.calls))

    def test_conflict_and_permission_failures_never_retry_or_expose_token(self):
        for status, expected in [(409, 'concurrent_update'), (403, 'credentials_rejected'), (503, 'storage_unavailable')]:
            api = FakeHF()
            api.denied = ('/commit/main', status)
            receipt = storage.sync_snapshot(sample(), TOKEN, api)
            self.assertEqual(receipt['status'], expected)
            self.assertFalse(receipt['uploaded'])
            self.assertEqual(sum(call[1].endswith('/commit/main') for call in api.calls), 1)
            self.assertNotIn(TOKEN, json.dumps(receipt))

    def test_private_dataset_has_no_upload(self):
        api = FakeHF()
        api.private = True
        receipt = storage.sync_snapshot(sample(), TOKEN, api)
        self.assertEqual(receipt['reason'], 'public_dataset_required')
        self.assertTrue(all(call[0] == 'GET' for call in api.calls))

    def test_redirect_and_arbitrary_endpoint_are_denied(self):
        with self.assertRaisesRegex(storage.StorageError, 'redirect_refused'):
            storage.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.test/')
        client = storage.HFClient(TOKEN)
        for path in ('https://elsewhere.test/', '//elsewhere.test/', '/api/models/other/repo/commit/main', storage.API + '/commit/main?token=bad'):
            with self.assertRaisesRegex(storage.StorageError, 'request_scope_rejected'):
                client.request('POST', path, b'{}')


if __name__ == '__main__':
    unittest.main()

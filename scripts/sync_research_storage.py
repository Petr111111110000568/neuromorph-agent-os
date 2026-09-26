"""Bounded public Git snapshots to one fixed Hugging Face dataset.

Run only in an authorized cloud checkout. Git objects are read as data; exported
Python is never imported or executed. Missing write credentials are optional.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

REPOSITORY = 'Petr111111110000568/neuromorph-agent-os'
SOURCE_URL = 'https://github.com/' + REPOSITORY
DATASET = 'Kto-to/neuromorph-agent-contributions'
ORIGIN = 'https://huggingface.co'
API = '/api/datasets/' + DATASET
MAX_FILES = 64
MAX_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_JSON_BYTES = 256 * 1024
STREAMS = {'main': 'main', 'autonomy/continuous': 'continuous'}
SECRET = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:hf_|sk-)[A-Za-z0-9_-]{16,}|'
                    r'\bgh[pousr]_[A-Za-z0-9_]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}')


class StorageError(ValueError):
    def __init__(self, reason, status=None):
        self.reason = reason
        self.status = status
        super().__init__(reason)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise StorageError('redirect_refused', code)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(',', ':')) + '\n').encode('utf-8')


def strict_json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_JSON_BYTES:
        raise StorageError('invalid_json_size')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise StorageError('duplicate_json_key')
            result[key] = value
        return result

    def constant(_):
        raise StorageError('nonfinite_json')

    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
        if len(encoded(value)) > MAX_JSON_BYTES:
            raise StorageError('invalid_json_size')
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise StorageError('invalid_json') from None


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{40}', value):
        raise StorageError('invalid_commit')
    return value


def allowed_path(path):
    if (not isinstance(path, str) or len(path) > 220 or not re.fullmatch(r'[A-Za-z0-9_./-]+', path)
            or str(PurePosixPath(path)) != path or PurePosixPath(path).is_absolute()
            or any(part.startswith('.') or part.startswith('__') for part in path.split('/'))):
        return False
    if path in {'README.md', 'docs/EVIDENCE_MEMORY_EXPERIMENT_RU.md'}:
        return True
    # Only this reviewed public synthetic pack, never arbitrary project datasets.
    if path in {'data/experiments/evidence_memory/' + name for name in
                ('sources.json', 'registry.json', 'cases.json', 'manifest.json', 'LICENSE.md')}:
        return True
    if re.fullmatch(r'docs/(?:research(?:-[0-9]{4}-[0-9]{2}-[0-9]{2})?|contributions)/[A-Za-z0-9_-]+\.(?:md|tex)', path):
        return True
    if path == 'docs/contributions/continuous-state.json':
        return True
    if re.fullmatch(r'docs/[A-Z0-9_]*(?:RESEARCH|MODEL|SCIENCE_ROADMAP)[A-Z0-9_]*\.(?:md|tex)', path):
        return True
    return bool(re.fullmatch(r'workbench/experiments/[A-Za-z0-9_-]+\.py', path))


def check_content(raw, known_tokens=()):
    if type(raw) is not bytes or len(raw) > MAX_FILE_BYTES:
        raise StorageError('file_size_limit')
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise StorageError('non_text_file') from None
    if '\0' in text or SECRET.search(text) or any(token and token in text for token in known_tokens):
        raise StorageError('secret_or_binary_content_rejected')
    return raw


def make_snapshot(source_commit, source_ref, files, known_tokens=()):
    sha(source_commit)
    if source_ref not in STREAMS or type(files) is not dict or not 1 <= len(files) <= MAX_FILES:
        raise StorageError('invalid_snapshot_scope')
    entries, content, total = [], {}, 0
    prefix = 'snapshots/' + STREAMS[source_ref] + '/'
    for path, raw in sorted(files.items()):
        if not allowed_path(path):
            raise StorageError('path_not_allowed')
        raw = check_content(raw, known_tokens)
        total += len(raw)
        if total > MAX_BYTES:
            raise StorageError('snapshot_size_limit')
        entries.append({'path': path, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        content[prefix + 'files/' + path] = raw
    identity = {'source_repository': SOURCE_URL, 'source_ref': source_ref, 'files': entries}
    manifest = dict(identity, schema_version=1, source_commit=source_commit,
        snapshot_sha256=hashlib.sha256(encoded(identity)).hexdigest(), dataset=DATASET,
        source_file_count=len(entries), source_bytes=total, generated_code_executed=False,
        scientific_validation='not_asserted', selection='bounded_public_research_allowlist_v1')
    manifest_path = prefix + 'manifest.json'
    content[manifest_path] = encoded(manifest)
    return {'manifest': manifest, 'manifest_path': manifest_path, 'files': content}


def git_read(repo, args, cap):
    # Strip cloud credentials and user Git overrides before any Git subprocess.
    environment = {'PATH': os.environ.get('PATH', os.defpath), 'LANG': 'C.UTF-8',
                   'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                   'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0'}
    for key in ('SystemRoot', 'WINDIR'):
        if key in os.environ:
            environment[key] = os.environ[key]
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(['git', '-C', str(repo), *args], stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.DEVNULL, env=environment, timeout=25, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise StorageError('git_read_failed') from None
        if result.returncode:
            raise StorageError('git_read_failed')
        output.seek(0)
        raw = output.read(cap + 1)
    if len(raw) > cap:
        raise StorageError('git_output_limit')
    return raw


def collect_snapshot(repo, source_ref, known_tokens=(), reader=git_read):
    origin = reader(repo, ['config', '--get', 'remote.origin.url'], 512).decode('utf-8').strip()
    if origin not in (SOURCE_URL, SOURCE_URL + '.git', 'git@github.com:' + REPOSITORY + '.git'):
        raise StorageError('unexpected_source_repository')
    commit = sha(reader(repo, ['rev-parse', '--verify', 'HEAD'], 64).decode('ascii').strip())
    tree = reader(repo, ['ls-tree', '-rz', '--long', '--full-tree', commit], 1024 * 1024)
    files = {}
    for record in tree.split(b'\0'):
        if not record:
            continue
        try:
            metadata, path_raw = record.split(b'\t', 1)
            mode, kind, blob, size = metadata.decode('ascii').split()
            path = path_raw.decode('utf-8')
        except (ValueError, UnicodeError):
            raise StorageError('invalid_git_tree') from None
        if not allowed_path(path):
            continue
        if mode not in ('100644', '100755') or kind != 'blob' or not size.isdigit() or int(size) > MAX_FILE_BYTES:
            raise StorageError('unsafe_git_entry')
        if path in files or len(files) >= MAX_FILES:
            raise StorageError('source_file_count_limit')
        raw = reader(repo, ['cat-file', 'blob', sha(blob)], MAX_FILE_BYTES)
        actual = hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
        if len(raw) != int(size) or actual != blob:
            raise StorageError('source_blob_integrity_mismatch')
        files[path] = raw
        if sum(len(data) for data in files.values()) > MAX_BYTES:
            raise StorageError('snapshot_size_limit')
    return make_snapshot(commit, source_ref, files, known_tokens)


class HFClient:
    def __init__(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'hf_[A-Za-z0-9]{16,200}', token):
            raise StorageError('invalid_write_token')
        self._token = token
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None, content_type='application/json'):
        allowed = (method == 'GET' and (path == API + '/revision/main' or re.fullmatch(
            r'/datasets/' + re.escape(DATASET) + r'/raw/[a-f0-9]{40}/snapshots/(?:main|continuous)/manifest\.json', path)))
        allowed = allowed or (method == 'POST' and path in (API + '/preupload/main', API + '/commit/main'))
        if not allowed or (body is not None and (not isinstance(body, bytes) or len(body) > 8 * 1024 * 1024)):
            raise StorageError('request_scope_rejected')
        url = ORIGIN + path
        headers = {'Accept': 'application/json', 'Content-Type': content_type,
                   'Accept-Encoding': 'identity', 'User-Agent': 'Meta-Harness-Research-Storage/1'}
        if method == 'POST':
            headers['Authorization'] = 'Bearer ' + self._token
        request = Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=30) as response:
                if response.geturl() != url or response.status not in (200, 201):
                    raise StorageError('unexpected_hf_response')
                raw = response.read(MAX_JSON_BYTES + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            raise StorageError('hf_http_error', status) from None
        except (URLError, TimeoutError, OSError):
            raise StorageError('hf_transport_unavailable') from None
        return strict_json(raw)


def sync_snapshot(snapshot, token, client=None):
    manifest = snapshot['manifest']
    receipt = {'schema_version': 1, 'dataset': DATASET, 'source_commit': manifest['source_commit'],
        'source_ref': manifest['source_ref'], 'snapshot_sha256': manifest['snapshot_sha256'],
        'source_file_count': manifest['source_file_count'], 'uploaded': False,
        'generated_code_executed': False, 'automatic_retry': False}
    if not token:
        return dict(receipt, status='credentials_required', required_secret='HF_WRITE_TOKEN')
    try:
        client = client or HFClient(token)
        info = client.request('GET', API + '/revision/main')
        if not isinstance(info, dict) or info.get('private') is not False:
            raise StorageError('public_dataset_required')
        parent = sha(info.get('sha'))
        try:
            old = client.request('GET', '/datasets/' + DATASET + '/raw/' + parent + '/' + snapshot['manifest_path'])
        except StorageError as error:
            if error.status != 404:
                raise
            old = None
        if (isinstance(old, dict) and old.get('schema_version') == 1 and old.get('dataset') == DATASET
                and old.get('source_repository') == SOURCE_URL and old.get('source_ref') == manifest['source_ref']
                and old.get('files') == manifest['files'] and old.get('snapshot_sha256') == manifest['snapshot_sha256']):
            return dict(receipt, status='unchanged', hf_commit=parent,
                        archived_source_commit=sha(old.get('source_commit')))
        content = snapshot['files']
        modes = client.request('POST', API + '/preupload/main', encoded({'files': [
            {'path': path, 'size': len(raw), 'sample': base64.b64encode(raw[:512]).decode('ascii')}
            for path, raw in sorted(content.items())]}))
        rows = modes.get('files') if isinstance(modes, dict) else None
        if (not isinstance(rows, list) or len(rows) != len(content) or any(type(item) is not dict for item in rows)
                or any(not isinstance(item.get('path'), str) for item in rows)
                or {item['path'] for item in rows} != set(content)
                or any(item.get('uploadMode') != 'regular' or item.get('shouldIgnore') is not False for item in rows)):
            raise StorageError('regular_upload_contract_required')
        records = [{'key': 'header', 'value': {'summary': 'Sync public research ' + manifest['source_commit'][:12],
            'description': 'Bounded public files; no model execution or scientific validation.', 'parentCommit': parent}}]
        records.extend({'key': 'file', 'value': {'path': path, 'encoding': 'base64',
            'content': base64.b64encode(raw).decode('ascii')}} for path, raw in sorted(content.items()))
        result = client.request('POST', API + '/commit/main', b''.join(encoded(item) for item in records),
                                content_type='application/x-ndjson')
        commit = sha(result.get('commitOid') if isinstance(result, dict) else None)
        return dict(receipt, status='synchronized', uploaded=True, hf_commit=commit)
    except StorageError as error:
        status = ('credentials_rejected' if error.status in (401, 403) else 'concurrent_update'
                  if error.status == 409 else 'storage_unavailable')
        return dict(receipt, status=status, reason=error.reason, http_status=error.status)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-ref', choices=tuple(STREAMS), required=True)
    parser.add_argument('--repo-dir', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=Path('runtime/research-storage/receipt.json'))
    args = parser.parse_args(argv)
    token = os.environ.get('HF_WRITE_TOKEN', '')
    code = 0
    try:
        if os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or os.environ.get('GITHUB_ACTIONS') != 'true':
            raise StorageError('cloud_github_context_required')
        snapshot = collect_snapshot(args.repo_dir, args.source_ref, known_tokens=(token,))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.with_name('snapshot_manifest.json').write_bytes(encoded(snapshot['manifest']))
        receipt = sync_snapshot(snapshot, token)
    except (StorageError, OSError, ValueError, UnicodeError, RecursionError):
        receipt = {'status': 'snapshot_rejected', 'uploaded': False, 'generated_code_executed': False,
                   'reason': 'invalid_public_snapshot_or_cloud_context'}
        code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded(receipt))
    print(json.dumps(receipt, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())

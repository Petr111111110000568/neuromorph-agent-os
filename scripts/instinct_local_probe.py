"""Pinned Instinct AI LocalEngine subset, executed only in a cloud fixture.

This independent package is NOT an established SDK of instinct.com. No LLM,
auto backend, pip hook, model download, credentials or production routing.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import types
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WHEEL_URL = 'https://files.pythonhosted.org/packages/34/4d/bd943c8d7d54f776c33c12e5f898e0737920e5c81c565a2d4013b68c4a64/instinct_ai-0.2.0-py3-none-any.whl'
WHEEL_SHA = 'b0f84e23a3525cddc04afb43bdccd204d6b6354410b4892e8cd86d4b8d220a4c'
MODULES = ('reflex.primitives', 'reflex.backends.base', 'reflex.backends.local')
CAP = 1_048_576
CASES = (
    ('literal_route', 'code change', 'code'),
    ('synonym_route', 'git commit', 'code'),
    ('search_route', 'web search', 'search'),
    ('negated_request', 'Do not execute code; only search', 'search'),
    ('russian_request', 'Найди научные статьи в интернете', 'search'),
    ('no_route_evidence', 'hello', 'abstain'),
)


def require_cloud():
    if sys.platform != 'linux' or not (os.environ.get('GITHUB_ACTIONS') == 'true'
                                      or os.environ.get('COLAB_RELEASE_TAG')):
        raise ValueError('authorized_linux_cloud_required')


def reference(text):
    """Transparent exact-token reference; abstains when evidence ties/is absent."""
    tokens = set(re.findall(r'\w+', text.lower()))
    candidates = [word for word in ('code', 'search', 'math') if word in tokens]
    return candidates[0] if len(candidates) == 1 else 'abstain'


def inspect_wheel(raw):
    if type(raw) is not bytes or len(raw) > CAP or hashlib.sha256(raw).hexdigest() != WHEEL_SHA:
        raise ValueError('wheel_digest_mismatch')
    sources = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        for name in MODULES:
            path = name.replace('.', '/') + '.py'
            if names.count(path) != 1:
                raise ValueError('wheel_member_missing_or_duplicate')
            info = archive.getinfo(path)
            if info.file_size > 65536 or info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('wheel_member_invalid')
            sources[name] = archive.read(info).decode('utf-8')
    return sources


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('download_redirect_refused')


def download():
    # Fixed public artifact only. Environment proxies and auth are not inherited.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(WHEEL_URL, headers={'User-Agent': 'neuromorph-public-fixture'}),
                     timeout=30) as response:
        if response.geturl() != WHEEL_URL:
            raise ValueError('download_origin_changed')
        raw = response.read(CAP + 1)
    inspect_wheel(raw)
    return raw


def deny_effects(event, args):
    if event.startswith(('socket.', 'subprocess.')) or event in {'os.system', 'os.posix_spawn', 'os.fork'}:
        raise RuntimeError('fixture_external_effect_refused')


def evaluate_subset(raw):
    """Child process only: load reviewed modules without package initializers."""
    sources = inspect_wheel(raw)
    for name in ('reflex', 'reflex.backends'):
        package = types.ModuleType(name)
        package.__path__ = []
        sys.modules[name] = package
    for name, source in sources.items():
        module = types.ModuleType(name)
        module.__package__ = name.rpartition('.')[0]
        sys.modules[name] = module
        exec(compile(source, '<pinned-wheel:' + name + '>', 'exec'), module.__dict__)
    engine = sys.modules['reflex.backends.local'].LocalEngine()
    choice = sys.modules['reflex.primitives'].Choice
    rows = []
    for case_id, text, expected in CASES:
        result = engine.evaluate(text, {'route': choice('Choose a route', ['code', 'search', 'math'])})
        rows.append({'id': case_id, 'input': text, 'expected_fixture_label': expected,
                     'local_heuristic': result['route'].selected, 'reference': reference(text)})
    return {'status': 'fixture_completed', 'package': 'instinct-ai', 'version': '0.2.0',
            'wheel_sha256': WHEEL_SHA, 'backend': 'explicit_local_heuristic',
            'loaded_modules': list(MODULES), 'remote_model_calls': 0, 'credentials_used': False,
            'automatic_routing': False, 'scientific_validation': False,
            'cases': rows, 'case_count': len(rows),
            'local_matches': sum(row['local_heuristic'] == row['expected_fixture_label'] for row in rows),
            'reference_matches': sum(row['reference'] == row['expected_fixture_label'] for row in rows)}


def fresh_output(value):
    raw = Path(value)
    if not raw.is_absolute():
        raw = ROOT / raw
    if raw.exists() or any(p.is_symlink() for p in (raw, *raw.parents)):
        raise ValueError('fresh_non_symlink_output_required')
    resolved = raw.resolve()
    runtime = (ROOT / 'runtime').resolve()
    if not resolved.is_relative_to(runtime) or resolved == runtime:
        raise ValueError('output_must_be_beneath_runtime')
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output-dir', default='runtime/instinct-probe')
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        require_cloud()
        with Path(args.worker).open('rb') as stream:
            raw = stream.read(CAP + 1)
        sys.addaudithook(deny_effects)
        print(json.dumps(evaluate_subset(raw), ensure_ascii=False))
        return 0
    if not args.execute:
        print(json.dumps({'status': 'plan_only', 'wheel_url': WHEEL_URL, 'wheel_sha256': WHEEL_SHA,
                          'modules': list(MODULES), 'remote_model_calls': 0}))
        return 0
    require_cloud()
    output = fresh_output(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    receipt = {'status': 'failed', 'remote_model_calls': 0, 'automatic_routing': False}
    code = 1
    try:
        wheel = output / 'instinct.whl'
        wheel.write_bytes(download())
        environment = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'HOME': str(output),
                       'GITHUB_ACTIONS': 'true', 'PYTHONDONTWRITEBYTECODE': '1'}
        child = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--worker', str(wheel)],
                               cwd=output, env=environment, capture_output=True, timeout=20, check=False)
        if child.returncode != 0 or len(child.stdout) > 16384:
            raise ValueError('fixture_child_failed')
        receipt = json.loads(child.stdout)
        if receipt.get('status') != 'fixture_completed' or receipt.get('remote_model_calls') != 0:
            raise ValueError('invalid_child_receipt')
        code = 0
    except Exception:
        # No response bodies, environment values or third-party tracebacks.
        receipt = {'status': 'failed', 'reason': 'fixture_unavailable', 'remote_model_calls': 0,
                   'automatic_routing': False}
    (output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('INSTINCT_RECEIPT=' + json.dumps(receipt, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())

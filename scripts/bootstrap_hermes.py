"""Pinned Hermes source installation; plan-only unless --execute in Linux cloud.

No model/auth/setup/gateway is invoked. The accepted checkout and operator manifest
are the trust root; this is not a signature or a sandbox for third-party builds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import sys
import time
import urllib.request
import zipfile

try:
    from scripts.bootstrap_cloud_harnesses import run_bounded, read_json, NoRedirect
except ModuleNotFoundError:
    from bootstrap_cloud_harnesses import run_bounded, read_json, NoRedirect

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config' / 'hermes_harness.json'
COMMIT = 'f97608f178d1ffeca59860195ab7da295f7c8e5f'
REPOSITORY = 'https://github.com/NousResearch/hermes-agent.git'
UV_SHA = 'a63d18a0aa38ee9f21a5406afbbaeb41303bcd954be9d6b7c1b95ac275e53958'
UV_URL = ('https://files.pythonhosted.org/packages/76/71/'
          'b47cec536d8ee7b09017c1d9db211dfc2e7ce5c87d0482918d8b3411ec48/'
          'uv-0.12.19-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl')
SOURCE_FILES = {'pyproject.toml', 'uv.lock', 'run_agent.py', 'model_tools.py',
                'agent/agent_init.py', 'hermes_cli/config_defaults.py',
                'hermes_cli/main.py', 'setup.py'}
MAX_SOURCE_FILE = 2 * 1024 * 1024
TOTAL_SECONDS = 900


class HermesError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise HermesError(reason)


def load_manifest(path=CONFIG):
    value = read_json(path, 16 * 1024)
    require(type(value) is dict and set(value) == {
        'schema_version', 'checked_at', 'repository', 'release', 'version', 'commit',
        'python', 'license', 'model', 'install_inference_calls', 'files', 'uv',
        'build_constraints'}, 'invalid_manifest')
    require(type(value['schema_version']) is int and value['schema_version'] == 1,
            'invalid_manifest')
    require(value['repository'] == REPOSITORY and value['commit'] == COMMIT
            and value['version'] == '0.21.5' and value['release'] == 'v2026.9.24'
            and value['python'] == '>=3.11,<3.14' and value['license'] == 'MIT'
            and value['model'] == 'qwen3-235b-a22b', 'unexpected_source')
    require(type(value['install_inference_calls']) is int
            and value['install_inference_calls'] == 0, 'inference_not_allowed')
    require(type(value['checked_at']) is str
            and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value['checked_at']), 'invalid_manifest')
    require(type(value['files']) is dict and set(value['files']) == SOURCE_FILES,
            'invalid_source_pins')
    for digest in value['files'].values():
        require(type(digest) is str and re.fullmatch(r'[0-9a-f]{64}', digest), 'invalid_source_pins')
    require(value['uv'] == {'version': '0.12.19', 'url': UV_URL, 'sha256': UV_SHA,
                            'size': 20478749}, 'unexpected_uv')
    require(value['build_constraints'] == ['setuptools==83.0.0', 'wheel==0.48.0',
                                            'packaging==26.0'], 'unexpected_build_constraints')
    return value


def no_links(path):
    for parent in (Path(path), *Path(path).parents):
        require(not parent.is_symlink(), 'symlink_path_rejected')


def runtime_path(value, root=ROOT):
    root = Path(root).absolute()
    path = Path(value)
    path = path if path.is_absolute() else root / path
    path = path.absolute()
    no_links(path)
    require(path.resolve().is_relative_to((root / 'runtime').resolve())
            and path != root / 'runtime', 'runtime_path_required')
    return path


def install_path(value, root=ROOT):
    path = runtime_path(value, root)
    require(path.parent == Path(root).absolute() / 'runtime'
            and re.fullmatch(r'hermes-harness(?:-[A-Za-z0-9_-]{1,48})?', path.name),
            'install_path_rejected')
    return path


def require_cloud():
    require(os.environ.get('GITHUB_ACTIONS') == 'true' or bool(os.environ.get('COLAB_RELEASE_TAG')),
            'cloud_execution_required')
    require(platform.system() == 'Linux' and platform.machine().lower() in {'x86_64', 'amd64'},
            'linux_x64_required')
    require((3, 11) <= sys.version_info[:2] < (3, 14), 'python_311_to_313_required')


def hash_file(path, limit=MAX_SOURCE_FILE):
    no_links(path)
    with Path(path).open('rb') as source:
        raw = source.read(limit + 1)
    require(len(raw) <= limit, 'source_file_limit')
    return hashlib.sha256(raw).hexdigest()


def verify_source(source, manifest):
    source = Path(source)
    no_links(source)
    for name, digest in manifest['files'].items():
        require(hash_file(source / name) == digest, 'source_digest_mismatch')


def install_environment(output, python, git):
    home, temp = output / 'home', output / 'tmp'
    for path in (home, temp, home / 'config', home / 'cache', home / 'data'):
        path.mkdir(parents=True, exist_ok=True)
    return {
        'PATH': os.pathsep.join(dict.fromkeys((str(Path(python).parent), str(Path(git).parent),
                                              '/usr/bin', '/bin'))),
        'HOME': str(home), 'TMPDIR': str(temp), 'XDG_CONFIG_HOME': str(home / 'config'),
        'XDG_CACHE_HOME': str(home / 'cache'), 'XDG_DATA_HOME': str(home / 'data'),
        'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONUTF8': '1', 'PYTHONNOUSERSITE': '1',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
        'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/false',
        'UV_PYTHON_DOWNLOADS': 'never', 'UV_NO_PROGRESS': '1',
        'UV_PROJECT_ENVIRONMENT': str(output / 'venv'), 'UV_CACHE_DIR': str(output / 'uv-cache'),
        'UV_DEFAULT_INDEX': 'https://pypi.org/simple', 'DO_NOT_TRACK': '1', 'CI': 'true',
    }


def download_uv(pin, destination, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(pin['url'], headers={'User-Agent': 'neuromorph-hermes-bootstrap/1'})
    deadline, size, digest = time.monotonic() + timeout, 0, hashlib.sha256()
    with opener.open(request, timeout=min(30, timeout)) as response, Path(destination).open('xb') as out:
        require(response.geturl() == pin['url'] and response.status == 200, 'uv_download_rejected')
        while True:
            require(time.monotonic() < deadline, 'uv_download_timeout')
            block = response.read(64 * 1024)
            if not block:
                break
            size += len(block)
            require(size <= pin['size'], 'uv_size_limit')
            digest.update(block)
            out.write(block)
    require(size == pin['size'] and digest.hexdigest() == pin['sha256'], 'uv_digest_mismatch')


def extract_uv(wheel, destination):
    expected = 'uv-0.12.19.data/scripts/uv'
    with zipfile.ZipFile(wheel) as archive:
        entries = archive.infolist()
        require(len(entries) <= 100, 'uv_archive_rejected')
        matches = [item for item in entries if item.filename == expected]
        require(len(matches) == 1, 'uv_entry_missing')
        item = matches[0]
        require(not item.is_dir() and not stat.S_ISLNK(item.external_attr >> 16)
                and 0 < item.file_size <= 96 * 1024 * 1024, 'uv_archive_rejected')
        # Extract only this fixed member. No archive-supplied path is used as a destination.
        with archive.open(item) as source, Path(destination).open('xb') as out:
            remaining = item.file_size
            while remaining:
                data = source.read(min(64 * 1024, remaining))
                require(bool(data), 'uv_archive_rejected')
                out.write(data)
                remaining -= len(data)
            require(not source.read(1), 'uv_archive_rejected')
    Path(destination).chmod(0o700)


def install_commands(uv, source, python, constraints, bootstrap_python):
    return [
        [str(uv), 'sync', '--frozen', '--no-dev', '--no-default-groups',
         '--no-install-project', '--python', str(bootstrap_python)],
        [str(uv), 'pip', 'install', '--python', str(python), '--no-deps',
         '--build-constraint', str(constraints), '-e', str(source)],
    ]

def bootstrap(output='runtime/hermes-harness', *, execute=False, root=ROOT,
              runner=run_bounded, downloader=download_uv):
    manifest = load_manifest()
    output = install_path(output, root)
    plan = {'schema_version': 1, 'status': 'planned', 'source_commit': COMMIT,
            'version': manifest['version'], 'source_root': str(output / 'source'),
            'python': str(output / 'venv' / 'bin' / 'python'), 'model_calls': 0,
            'install_mode': 'core_editable_frozen_no_extras',
            'uv_lock_sha256': manifest['files']['uv.lock']}
    if not execute:
        return plan
    require_cloud()
    require(not output.exists(), 'fresh_output_required')
    git = shutil.which('git')
    require(git is not None, 'git_missing')
    output.mkdir(parents=True)
    env = install_environment(output, sys.executable, git)
    receipt = dict(plan, status='installing', commands=[])
    deadline = time.monotonic() + TOTAL_SECONDS

    def command(argv, cwd, seconds):
        require(time.monotonic() < deadline, 'install_deadline')
        result = runner(argv, cwd, env, min(seconds, deadline - time.monotonic()))
        receipt['commands'].append(result)
        require(result['status'] == 'ok', 'install_command_failed')
        return result

    try:
        source = output / 'source'
        command([git, 'init', str(source)], output, 20)
        prefix = [git, '-c', 'core.hooksPath=/dev/null', '-c', 'credential.helper=', '-C', str(source)]
        command(prefix + ['fetch', '--depth=1', '--no-tags', REPOSITORY, COMMIT], output, 180)
        command(prefix + ['checkout', '--detach', 'FETCH_HEAD'], output, 90)
        identity = command(prefix + ['rev-parse', 'HEAD'], output, 10)
        require(identity['output'].strip() == COMMIT, 'source_commit_mismatch')
        verify_source(source, manifest)
        wheel = output / 'uv.whl'
        downloader(manifest['uv'], wheel, min(90, deadline - time.monotonic()))
        require(hash_file(wheel, 32 * 1024 * 1024) == UV_SHA, 'uv_digest_mismatch')
        uv = output / 'uv'
        extract_uv(wheel, uv)
        constraints = output / 'build-constraints.txt'
        constraints.write_text('\n'.join(manifest['build_constraints']) + '\n', encoding='utf-8')
        python = output / 'venv' / 'bin' / 'python'
        # uv sync has no --build-constraint flag. Install its locked runtime graph
        # first; then install only the editable project with pip build constraints.
        # No vendor pyproject/lock bytes need changing for the build-only policy.
        commands = install_commands(uv, source, python, constraints, sys.executable)
        command(commands[0], source, 600)
        require(python.is_file(), 'installed_python_missing')
        command(commands[1], source, 180)
        verify_source(source, manifest)
        command(prefix + ['diff', '--exit-code', 'HEAD', '--'], source, 20)
        receipt.update(status='installed', install_model_calls=0, sdk_fixture='not_run',
                       source_files=manifest['files'], build_constraints=manifest['build_constraints'],
                       uv_sha256=UV_SHA,
                       limitations=['editable source checkout; build code executes on disposable cloud',
                                    'uv.lock pins runtime graph; build constraints are separately pinned',
                                    'no model, help or SDK inference executed by installer'])
        return receipt
    except Exception as exc:
        receipt.update(status='failed', reason=str(exc) if isinstance(exc, HermesError) else 'installation_failed')
        raise
    finally:
        (output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output-dir', default='runtime/hermes-harness')
    args = parser.parse_args(argv)
    try:
        result = bootstrap(args.output_dir, execute=args.execute)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, HermesError) else 'installation_failed'
        print(json.dumps({'status': 'failed', 'reason': reason, 'model_calls': 0}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

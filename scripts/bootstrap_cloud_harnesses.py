"""Plan by default; install pinned CLI packages and smoke help/version in cloud.

No model prompt, authentication, daemon or generated code is executed. This is
not an OS sandbox: reviewed vendor CLI help code still executes on the disposable
runner. Root archives are pinned; transitive npm resolution is recorded, not
claimed to be fully reproducible before an independently reviewed lock exists.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import selectors
import shutil
import signal
import subprocess
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config' / 'cloud_harnesses.json'
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_UNPACKED = 512 * 1024 * 1024
MAX_OUTPUT = 64 * 1024
MAX_LOCK = 8 * 1024 * 1024
TOTAL_SECONDS = 900
ALLOWED = {
    'dsh': ('@deepseek-ai/dsh', 'lib/bin.js', 'node'),
    'qwen': ('@qwen-code/qwen-code', 'cli-entry.js', 'node'),
    'gemini': ('@google/gemini-cli', 'bundle/gemini.js', 'node'),
    'opencode': ('opencode-linux-x64', 'bin/opencode', 'native'),
}


class BootstrapError(ValueError):
    pass


def require(condition, reason='invalid_manifest'):
    if not condition:
        raise BootstrapError(reason)


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result)
        result[key] = value
    return result


def read_json(path, limit):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, 'json_size_limit')
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BootstrapError('invalid_json') from exc


def validate_config(config):
    require(type(config) is dict and set(config) == {'schema_version', 'checked_at', 'node_minimum',
                                                    'platform', 'model_calls_allowed', 'packages'})
    require(type(config['schema_version']) is int and config['schema_version'] == 1)
    require(config['node_minimum'] == '22.19.0' and config['platform'] == 'linux-x64'
            and config['model_calls_allowed'] is False)
    require(type(config['checked_at']) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}', config['checked_at']))
    require(type(config['packages']) is list and 1 <= len(config['packages']) <= 4)
    seen = set()
    for package in config['packages']:
        require(type(package) is dict and set(package) == {'id', 'name', 'version', 'tarball',
                                                          'integrity', 'entry', 'kind', 'free_inference'})
        identity = package['id']
        require(type(identity) is str and identity in ALLOWED and identity not in seen)
        seen.add(identity)
        require((package['name'], package['entry'], package['kind']) == ALLOWED[identity])
        version = package['version']
        require(type(version) is str and re.fullmatch(r'\d{1,3}\.\d{1,3}\.\d{1,3}(?:-rc\.\d{1,3})?', version))
        basename = package['name'].split('/')[-1]
        require(package['tarball'] == 'https://registry.npmjs.org/' + package['name'] + '/-/' + basename + '-' + version + '.tgz')
        integrity = package['integrity']
        require(type(integrity) is str and integrity.startswith('sha512-'))
        try:
            require(len(base64.b64decode(integrity[7:], validate=True)) == 64)
        except ValueError as exc:
            raise BootstrapError('invalid_manifest') from exc
        require(type(package['free_inference']) is str and 1 <= len(package['free_inference']) <= 180)
    return config


def _no_links(path):
    for part in (path,) + tuple(path.parents):
        require(not part.is_symlink(), 'symlink_path_rejected')


def output_path(value, repository_root=ROOT):
    repository_root = Path(repository_root).absolute()
    _no_links(repository_root)
    path = Path(value)
    path = path if path.is_absolute() else repository_root / path
    path = path.absolute()
    _no_links(path)
    require(path.parent == repository_root / 'runtime'
            and re.fullmatch(r'cloud-harnesses(?:-[a-zA-Z0-9_-]{1,48})?', path.name), 'output_path_rejected')
    require(path.resolve().parent == (repository_root / 'runtime').resolve(), 'output_path_rejected')
    return path


def isolated_environment(root, identity, node, npm):
    home, work = root / 'home' / identity, root / 'work' / identity
    for path in (home, work, root / 'tmp', root / 'npm-cache', home / 'config', home / 'data', home / 'cache'):
        path.mkdir(parents=True, exist_ok=True)
    userconfig, globalconfig = home / 'npmrc', home / 'global-npmrc'
    userconfig.write_text('', encoding='utf-8')
    globalconfig.write_text('', encoding='utf-8')
    # No inherited proxy, provider credentials, NODE_OPTIONS, npm config or user paths.
    env = {'PATH': os.pathsep.join(dict.fromkeys((str(Path(node).parent), str(Path(npm).parent), '/usr/bin', '/bin'))),
           'HOME': str(home), 'TMPDIR': str(root / 'tmp'), 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
           'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_DATA_HOME': str(home / 'data'),
           'XDG_CACHE_HOME': str(home / 'cache'), 'CI': 'true', 'DO_NOT_TRACK': '1',
           'NPM_CONFIG_USERCONFIG': str(userconfig), 'NPM_CONFIG_GLOBALCONFIG': str(globalconfig),
           'NPM_CONFIG_CACHE': str(root / 'npm-cache'), 'NPM_CONFIG_REGISTRY': 'https://registry.npmjs.org/',
           'NPM_CONFIG_IGNORE_SCRIPTS': 'true', 'NPM_CONFIG_AUDIT': 'false', 'NPM_CONFIG_FUND': 'false',
           'NPM_CONFIG_UPDATE_NOTIFIER': 'false', 'NPM_CONFIG_FETCH_RETRIES': '0',
           'NPM_CONFIG_FETCH_TIMEOUT': '30000'}
    return env, work


def run_bounded(argv, cwd, env, timeout):
    """Only caller-constructed npm/node/help commands; bounded merged stdout."""
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               shell=False, start_new_session=True)
    captured = bytearray()
    status = 'ok'
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = 'timeout'
                break
            for key, _ in selector.select(min(remaining, 0.25)):
                data = os.read(key.fileobj.fileno(), 8192)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if len(captured) + len(data) > MAX_OUTPUT:
                    captured.extend(data[:MAX_OUTPUT - len(captured)])
                    status = 'output_limit'
                    break
                captured.extend(data)
            if status != 'ok':
                break
        if status != 'ok':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            code = process.wait(timeout=max(0.1, min(5, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = process.wait(timeout=5)
            status = 'timeout'
        if status == 'ok' and code:
            status = 'command_failed'
        return {'status': status, 'returncode': code, 'output_bytes': len(captured),
                'output_sha256': hashlib.sha256(captured).hexdigest(),
                'output': captured.decode('utf-8', errors='replace')[:8192]}
    finally:
        selector.close()
        process.stdout.close()
        # Clean the process group even when the parent already exited: helpers may survive it.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=5)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BootstrapError('archive_redirect_rejected')


def fetch_archive(package, destination, timeout):
    """Fixed registry host, no proxy/auth, bounded archive download."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    digest, size = hashlib.sha512(), 0
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(package['tarball'], headers={'User-Agent': 'neuromorph-cli-bootstrap/1'})
    with opener.open(request, timeout=min(timeout, 30)) as response, destination.open('xb') as target:
        require(response.geturl() == package['tarball'], 'archive_redirect_rejected')
        while True:
            require(time.monotonic() < deadline, 'archive_timeout')
            block = response.read(64 * 1024)
            if not block:
                break
            size += len(block)
            require(size <= MAX_ARCHIVE, 'archive_size_limit')
            digest.update(block)
            target.write(block)
    expected = base64.b64decode(package['integrity'][7:], validate=True)
    require(digest.digest() == expected, 'archive_integrity_mismatch')
    return size


def check_archive(path, package):
    # Recheck supplied bytes even when a test or caller replaces the downloader.
    digest = hashlib.sha512()
    size = 0
    with path.open('rb') as stream:
        while block := stream.read(64 * 1024):
            size += len(block)
            require(size <= MAX_ARCHIVE, 'archive_size_limit')
            digest.update(block)
    require(digest.digest() == base64.b64decode(package['integrity'][7:]), 'archive_integrity_mismatch')
    count, unpacked, metadata = 0, 0, None
    try:
        with tarfile.open(path, 'r:gz') as archive:
            for member in archive:
                count += 1
                unpacked += member.size
                parts = PurePosixPath(member.name).parts
                require(count <= 5000 and unpacked <= MAX_UNPACKED, 'archive_expansion_limit')
                require(parts and parts[0] == 'package' and '..' not in parts and '\\' not in member.name
                        and not member.name.startswith('/') and (member.isfile() or member.isdir()), 'archive_path_rejected')
                if member.name == 'package/package.json':
                    require(metadata is None and member.size <= 256 * 1024, 'archive_package_metadata')
                    metadata = json.loads(archive.extractfile(member).read(), object_pairs_hook=pairs)
    except (tarfile.TarError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BootstrapError('invalid_archive') from exc
    require(type(metadata) is dict and metadata.get('name') == package['name']
            and metadata.get('version') == package['version'], 'archive_package_metadata')
    return {'download_bytes': size, 'unpacked_bytes': unpacked, 'files': count}


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(64 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _write_receipt(root, receipt):
    temporary = root / 'receipt.tmp'
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(root / 'receipt.json')


def _entry_path(root, package):
    entry = root / 'packages' / package['id'] / 'node_modules' / package['name'] / package['entry']
    _no_links(entry)
    require(entry.is_file() and entry.resolve().is_relative_to(root.resolve()), 'entry_path_rejected')
    return entry


def bootstrap(config, output_dir, execute=False, *, repository_root=ROOT, environment=None,
              runner=run_bounded, fetcher=fetch_archive):
    config = validate_config(config)
    root = output_path(output_dir, repository_root)
    receipt = {'schema_version': 1, 'status': 'plan_only', 'model_calls': 0, 'authentication_performed': False,
               'package_scripts_enabled': False, 'packages': [],
               'limitations': ['root archives pinned; transitive dependencies resolved by npm at execution time',
                  'lockfile hash records resolution; does not retroactively establish a reviewed lock',
                  'isolated home/environment is not an OS sandbox or proof against malicious vendor code',
                  'help/version success is not authenticated model inference or scientific validation']}
    if not execute:
        receipt['packages'] = [{'id': p['id'], 'name': p['name'], 'version': p['version'],
                                'tarball': p['tarball'], 'integrity': p['integrity'],
                                'smoke_arguments': ['--version', '--help']} for p in config['packages']]
        return receipt
    environment = os.environ if environment is None else environment
    require(environment.get('GITHUB_ACTIONS') == 'true' or bool(environment.get('COLAB_RELEASE_TAG')),
            'cloud_execution_required')
    require(platform.system() == 'Linux' and platform.machine() in ('x86_64', 'amd64'), 'linux_x64_required')
    # Avoid upward-discovered user credentials/config (e.g. Qwen auto-loads .env).
    for ancestor in root.parents:
        for relative in ('.env', '.qwen/.env', '.gemini/.env', '.qwen/settings.json', '.gemini/settings.json',
                         'opencode.json', 'opencode.jsonc', 'dsh.yaml', 'dsh.yml'):
            require(not (ancestor / relative).exists(), 'ancestor_config_rejected')
    require(not root.exists(), 'fresh_output_directory_required')
    node, npm = shutil.which('node'), shutil.which('npm')
    require(node is not None and npm is not None, 'node_npm_missing')
    root.parent.mkdir(parents=True, exist_ok=True)
    _no_links(root.parent)
    root.mkdir()  # Atomic claim: another invocation cannot reuse this installation directory.
    (root / 'archives').mkdir()
    receipt['status'] = 'running'
    _write_receipt(root, receipt)
    deadline = time.monotonic() + TOTAL_SECONDS
    try:
        env, cwd = isolated_environment(root, 'bootstrap', node, npm)
        version = runner([node, '--version'], cwd, env, 15)
        text = version.get('output', '').strip()
        match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', text)
        require(version['status'] == 'ok' and match is not None, 'node_version_unavailable')
        numeric = tuple(int(x) for x in match.groups())
        require((numeric[0] == 22 and numeric >= (22, 19, 0)) or numeric[0] >= 24, 'node_version_unsupported')
        receipt['node_version'] = text
        for package in config['packages']:
            item = {'id': package['id'], 'name': package['name'], 'version': package['version'],
                    'integrity': package['integrity'], 'status': 'pending', 'model_calls': 0}
            receipt['packages'].append(item)
            _write_receipt(root, receipt)
            try:
                require(time.monotonic() + 10 < deadline, 'total_timeout')
                env, cwd = isolated_environment(root, package['id'], node, npm)
                archive = root / 'archives' / (package['id'] + '.tgz')
                fetcher(package, archive, min(90, deadline - time.monotonic()))
                item['archive'] = check_archive(archive, package)
                prefix = root / 'packages' / package['id']
                prefix.mkdir(parents=True)
                (prefix / 'package.json').write_text('{"private":true,"name":"cloud-harness-smoke","version":"1.0.0"}\n', encoding='utf-8')
                install = runner([npm, 'install', '--prefix', str(prefix), '--ignore-scripts', '--no-audit',
                                  '--no-fund', '--package-lock=true', '--registry=https://registry.npmjs.org/',
                                  str(archive)], prefix, env, min(180, max(1, deadline - time.monotonic())))
                item['install'] = install
                require(install['status'] == 'ok', 'npm_install_failed')
                lock_path = prefix / 'package-lock.json'
                _no_links(lock_path)
                lock = read_json(lock_path, MAX_LOCK)
                require(type(lock) is dict and type(lock.get('packages')) is dict, 'invalid_lockfile')
                root_entry = lock['packages'].get('node_modules/' + package['name'], {})
                require(type(root_entry) is dict, 'invalid_lockfile')
                require(root_entry.get('version') == package['version']
                        and root_entry.get('integrity') == package['integrity'], 'installed_pin_mismatch')
                item['lock_sha256'] = hashlib.sha256(lock_path.read_bytes()).hexdigest()
                item['resolved_package_count'] = len(lock.get('packages', {}))
                entry = _entry_path(root, package)
                item['entry_path'] = str(entry)
                item['entry_sha256'] = file_sha256(entry)
                item['smoke'] = []
                for flag in ('--version', '--help'):
                    command = [node, str(entry), flag] if package['kind'] == 'node' else [str(entry), flag]
                    probe = runner(command, cwd, env, min(20, max(1, deadline - time.monotonic())))
                    item['smoke'].append({'flag': flag, **probe})
                    require(probe['status'] == 'ok' and bool(probe.get('output', '').strip()), 'smoke_failed')
                    if flag == '--version':
                        require(package['version'] in probe['output'], 'smoke_version_mismatch')
                item['status'] = 'installed_smoke_passed'
            except (BootstrapError, OSError, ValueError) as exc:
                item['status'] = 'failed'
                item['reason'] = str(exc) if isinstance(exc, BootstrapError) else type(exc).__name__
            _write_receipt(root, receipt)
        receipt['status'] = 'completed' if all(x['status'] == 'installed_smoke_passed' for x in receipt['packages']) else 'completed_with_failures'
    except (BootstrapError, OSError, ValueError) as exc:
        receipt['status'] = 'failed'
        receipt['reason'] = str(exc) if isinstance(exc, BootstrapError) else type(exc).__name__
    _write_receipt(root, receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output-dir', default='runtime/cloud-harnesses')
    args = parser.parse_args(argv)
    try:
        receipt = bootstrap(read_json(CONFIG, 32 * 1024), args.output_dir, args.execute)
    except (BootstrapError, OSError, ValueError) as exc:
        print(json.dumps({'status': 'rejected', 'reason': str(exc) if isinstance(exc, BootstrapError) else type(exc).__name__, 'model_calls': 0}))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt['status'] in ('plan_only', 'completed') else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Run one finite OpenClaw/Hermes review through the local, reserved model gateway.

Configuration is an operator-owned local file, never a model-generated profile.
The CLI does not accept executable task text or external provider endpoints.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_profiles(config_path):
    config_path = Path(config_path).resolve(strict=True)
    if config_path.stat().st_size > 16 * 1024:
        raise ValueError('Profile too large')
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    expected = {'schema_version', 'model_root', 'data_dir', 'node', 'openclaw_package',
                'hermes_python', 'hermes_source', 'git'}
    if set(config) != expected or config['schema_version'] != 1:
        raise ValueError('Invalid operator profile')
    for key in expected - {'schema_version'}:
        if not isinstance(config[key], str) or not Path(config[key]).is_absolute():
            raise ValueError('Operator paths must be absolute')
    return config


def make_runner(config):
    here = Path(__file__).resolve().parent
    openclaw = _module(here / 'openclaw_local_peer.py', 'neuromorph_openclaw_peer')
    claw_runner = openclaw.make_role_runner(Path(config['node']), Path(config['openclaw_package']))

    def run(stage, prompt, base_url, token, stage_dir):
        if stage != 'critique':
            return claw_runner(stage, prompt, base_url, token, stage_dir)
        folder = Path(stage_dir)
        prompt_file = folder / 'hermes-prompt.txt'
        with prompt_file.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(prompt)
        output = folder / 'hermes-result.json'
        home = folder / 'hermes-home'
        argv = [config['hermes_python'], '-I', '-B', str(here / 'hermes_local_peer.py'),
                '--source-root', config['hermes_source'], '--base-url', base_url,
                '--prompt-file', str(prompt_file), '--output-file', str(output),
                '--home-dir', str(home), '--stop-file', str(folder / 'STOP')]
        env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'LANG', 'LC_ALL')
               if key in os.environ}
        env.update({'NEUROMORPH_TANDEM_TOKEN': token, 'PYTHONUTF8': '1',
                    'PATH': str(Path(config['git']).parent),
                    'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
                    'HOME': str(home), 'USERPROFILE': str(home),
                    'TMP': str(folder), 'TEMP': str(folder)})
        # Output goes to bounded temporary files, never to a provider's account log.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(argv, cwd=folder, env=env, stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr, shell=False,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            deadline = time.monotonic() + 370
            try:
                while proc.poll() is None:
                    if time.monotonic() > deadline or max(os.fstat(stdout.fileno()).st_size,
                                                        os.fstat(stderr.fileno()).st_size) > 256 * 1024:
                        raise RuntimeError('Hermes child exceeded its bound')
                    time.sleep(0.1)
                if proc.returncode:
                    # Adapter emits a fixed diagnostic code, not credentials or arbitrary logs.
                    if not output.exists():
                        stdout.seek(0)
                        try:
                            diagnostic = json.loads(stdout.read(256 * 1024))
                            safe = {key: val for key, val in diagnostic.items()
                                    if key in {'reason', 'stage', 'failure_kind', 'missing_module'}
                                    and isinstance(val, str)
                                    and re.fullmatch(r'[A-Za-z0-9_.]{1,120}', val)}
                            with output.open('x', encoding='utf-8') as stream:
                                json.dump(dict(safe, status='failed'), stream)
                        except (ValueError, AttributeError):
                            pass
                    if output.is_file() and output.stat().st_size < 64 * 1024:
                        value = json.loads(output.read_text(encoding='utf-8'))
                        raise RuntimeError('Hermes failed: ' + str(value.get('reason', 'child_failed'))[:120])
                    raise RuntimeError('Hermes child failed')
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
        if not output.is_file() or output.stat().st_size > 64 * 1024:
            raise RuntimeError('Invalid Hermes result file')
        value = json.loads(output.read_text(encoding='utf-8'))
        if value.get('status') != 'response_received' or value.get('api_calls') != 1:
            raise RuntimeError('Hermes did not complete one model request')
        return value['text']
    return run


def profile_ids(config):
    path = Path(config['openclaw_package']) / 'neuromorph-installed-manifest.json'
    if path.stat().st_size > 16 * 1024:
        raise ValueError('Installation manifest too large')
    manifest = json.loads(path.read_text(encoding='utf-8-sig'))
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                                      allow_nan=False).encode()).hexdigest()
    return {'openclaw': 'openclaw@2026.9.6:sha256:' + digest,
            'hermes': 'hermes@54fb5a42e8ecf2bf0326f7bd829a292ee0965451'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--question-file', type=Path, required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--public-data-confirmed', action='store_true', required=True)
    args = parser.parse_args(argv)
    if args.question_file.stat().st_size > 12 * 1024:
        raise ValueError('Question file too large')
    config = load_profiles(args.profiles)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from workbench.tandem import TandemCoordinator
    coordinator = TandemCoordinator(config['model_root'], config['data_dir'],
        role_runner=make_runner(config), profile_ids=profile_ids(config))
    result = coordinator.run(args.task_id, args.question_file.read_text(encoding='utf-8-sig'),
                             public_data_confirmed=args.public_data_confirmed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('status') == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())

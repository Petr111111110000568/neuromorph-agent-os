"""One pinned Hermes SDK turn through a caller-owned loopback gateway, cloud only.

The gateway, not Hermes, owns the durable quota and permits at most one provider
call. This wrapper is a reviewed Python I/O guard, not an OS/native-code sandbox.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from urllib.parse import urlsplit

try:
    from scripts.bootstrap_hermes import (ROOT, COMMIT, HermesError, load_manifest, no_links,
                                         require, require_cloud, runtime_path, verify_source)
except ModuleNotFoundError:
    from bootstrap_hermes import (ROOT, COMMIT, HermesError, load_manifest, no_links,
                                 require, require_cloud, runtime_path, verify_source)

MODEL = 'qwen3-235b-a22b'
MAX_PROMPT = 4000
MAX_TEXT = 65536
WALL_SECONDS = 160
SDK_SECONDS = 130


def loopback_url(value):
    require(type(value) is str and re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}/v1', value),
            'loopback_url_required')
    parsed = urlsplit(value)
    require(parsed.port is not None and 1024 <= parsed.port <= 65535, 'loopback_port_rejected')
    return parsed.port


def read_prompt(path):
    no_links(path)
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_PROMPT * 4 + 1)
    require(len(raw) <= MAX_PROMPT * 4, 'prompt_limit')
    try:
        value = raw.decode('utf-8', errors='strict')
    except UnicodeError as exc:
        raise HermesError('invalid_prompt_encoding') from exc
    require(1 <= len(value) <= MAX_PROMPT and value.strip() and '\x00' not in value, 'prompt_limit')
    return value


def profile(base_url):
    loopback_url(base_url)
    # JSON is a YAML subset. Match runtime route exactly so Hermes accepts the
    # configured context metadata. The input is still bounded to 4000 chars.
    return {
        'model': {'default': MODEL, 'provider': 'custom', 'base_url': base_url,
                  'api_key': 'no-key-required', 'context_length': 65536},
        'providers': {}, 'fallback_providers': [], 'mcp_servers': {}, 'toolsets': [],
        'platform_toolsets': {'cli': []},
        'agent': {'max_turns': 1, 'api_max_retries': 1, 'auto_recovery_cycles': 0,
                  'environment_probe': False, 'bot_mode_protocol': False,
                  'parallel_tool_call_guidance': False, 'task_completion_guidance': False,
                  'stall_guards': False},
        'compression': {'enabled': False},
        'memory': {'memory_enabled': False, 'user_profile_enabled': False, 'provider': ''},
        'skills': {'auto_load': []},
        'auxiliary': {'free_only': True, 'transient_retries': 0,
                      'title_generation': {'enabled': False, 'model_upgrade_enabled': False},
                      'background_review': {'enabled': False}},
        'security': {'allow_lazy_installs': False}, 'updates': {'check': False},
        'checkpoints': {'enabled': False},
    }


def fresh_environment(home, python=sys.executable):
    home = Path(home)
    no_links(home)
    require(not home.exists(), 'fresh_home_required')
    home.mkdir(parents=True, mode=0o700)
    for name in ('tmp', 'config', 'cache', 'data', 'work'):
        (home / name).mkdir(mode=0o700)
    return {
        'PATH': os.pathsep.join((str(Path(python).parent), '/usr/bin', '/bin')),
        'HOME': str(home), 'HERMES_HOME': str(home), 'TMPDIR': str(home / 'tmp'),
        'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_CACHE_HOME': str(home / 'cache'),
        'XDG_DATA_HOME': str(home / 'data'), 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
        'PYTHONUTF8': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        'HERMES_DISABLE_LAZY_INSTALLS': '1', 'HERMES_SINGLE_QUERY_SESSION': '1',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
        'DO_NOT_TRACK': '1', 'CI': 'true',
    }


def audit_guard(port):
    """A testable hook installed only in the dedicated production child process."""
    def guard(event, args):
        if event in {'socket.connect', 'socket.connect_ex'}:
            address = args[1]
            if not (type(address) is tuple and len(address) == 2
                    and address[0] == '127.0.0.1' and address[1] == port):
                raise PermissionError('external_connection_blocked')
        elif event == 'socket.getaddrinfo':
            if args[0] != '127.0.0.1' or args[1] not in (port, str(port)):
                raise PermissionError('external_dns_blocked')
        elif event in {'socket.bind', 'socket.sendto', 'subprocess.Popen', 'os.system',
                       'os.exec', 'os.posix_spawn', 'pty.spawn'}:
            raise PermissionError('runtime_execution_blocked')
    return guard


class BoundedSink(io.TextIOBase):
    def __init__(self):
        self.count = 0

    def writable(self):
        return True

    def write(self, text):
        self.count += len(str(text).encode('utf-8', errors='replace'))
        require(self.count <= 65536, 'sdk_log_limit')
        return len(text)


def run_sdk(prompt, base_url, home, factory):
    """Source-pinned constructor; fake factory tests do not import Hermes."""
    loopback_url(base_url)
    require(type(prompt) is str and 1 <= len(prompt) <= MAX_PROMPT, 'prompt_limit')
    agent = None
    try:
        agent = factory(
            base_url=base_url, api_key='no-key-required', provider='custom',
            requested_provider='custom', api_mode='chat_completions', model=MODEL,
            enabled_toolsets=[], disabled_toolsets=[], max_iterations=1, max_tokens=2048,
            skip_context_files=True, load_soul_identity=False, skip_memory=True,
            skip_background_review=True, session_db=None, fallback_model=None,
            credential_pool=None, checkpoints_enabled=False, save_trajectories=False,
            verbose_logging=False, quiet_mode=True, stream_delta_callback=None,
            run_budget_seconds=SDK_SECONDS, cwd=str(Path(home) / 'work'),
            request_overrides={'stream': False},
        )
        require(getattr(agent, 'tools', None) == []
                and not getattr(agent, 'valid_tool_names', None), 'nonempty_tools_rejected')
        require(getattr(agent, 'compression_enabled', None) is False,
                'compression_not_disabled')
        # The pinned implementation exposes the OpenAI-compatible client and its
        # reconstruction kwargs. Disable its built-in retries as well as Hermes'
        # application retries. Gateway admission remains the hard one-call gate.
        client = getattr(agent, 'client', None)
        require(client is not None and hasattr(client, 'max_retries'), 'unexpected_sdk_client')
        client.max_retries = 0
        require(client.max_retries == 0, 'sdk_retries_not_disabled')
        kwargs = getattr(agent, '_client_kwargs', None)
        require(type(kwargs) is dict, 'unexpected_sdk_client')
        kwargs['max_retries'] = 0
        kwargs['timeout'] = SDK_SECONDS
        agent.suppress_status_output = True
        result = agent.run_conversation(prompt)
        require(type(result) is dict and result.get('completed') is True
                and not result.get('interrupted') and not result.get('partial'), 'sdk_incomplete')
        text = result.get('final_response')
        require(type(text) is str and text.strip()
                and len(text.encode('utf-8')) <= MAX_TEXT, 'sdk_response_rejected')
        return {'status': 'response_received', 'text': text, 'model': MODEL,
                'source_commit': COMMIT, 'tools': [], 'output_execution': False,
                'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest()}
    finally:
        if agent is not None:
            # With skip_memory=True no memory flush/learning is requested here.
            agent.close()


def source_identity(source, env):
    git = shutil.which('git', path=env['PATH'])
    require(git is not None, 'git_missing')
    for command, expected in ((['rev-parse', 'HEAD'], COMMIT),
                              (['status', '--porcelain', '--untracked-files=no'], '')):
        result = subprocess.run([git, '-c', 'core.hooksPath=/dev/null', '-C', str(source), *command],
                                env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding='utf-8', timeout=10, check=False)
        require(result.returncode == 0 and result.stdout.strip() == expected,
                'source_checkout_changed')


def safe_failure(exc):
    """Useful import diagnostics without exception messages, locals or source lines."""
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        name = Path(trace.tb_frame.f_code.co_filename).name
        frames.append({'file': re.sub(r'[^A-Za-z0-9_.-]', '_', name)[:80],
                       'line': trace.tb_lineno})
        trace = trace.tb_next
    return {'status': 'failed', 'reason': str(exc) if type(exc) is HermesError else 'sdk_failed',
            'failure_kind': re.sub(r'[^A-Za-z0-9_]', '_', type(exc).__name__)[:64],
            'frames': frames[-6:], 'model': MODEL, 'source_commit': COMMIT, 'tools': []}

def _deadline(signum, frame):
    raise HermesError('sdk_timeout')


def execute(source_root, base_url, prompt_file, output_file, home_dir):
    require_cloud()
    source, prompt_path, output, home = [runtime_path(value) for value in
                                       (source_root, prompt_file, output_file, home_dir)]
    require(source.name == 'source' and source.parent.parent == ROOT / 'runtime'
            and re.fullmatch(r'hermes-harness(?:-[A-Za-z0-9_-]{1,48})?', source.parent.name),
            'source_path_rejected')
    require(output.parent.exists() and not output.exists(), 'fresh_output_required')
    port = loopback_url(base_url)
    prompt = read_prompt(prompt_path)
    manifest = load_manifest()
    verify_source(source, manifest)
    env = fresh_environment(home)
    source_identity(source, env)
    (home / 'config.yaml').write_text(json.dumps(profile(base_url), ensure_ascii=False, indent=2) + '\n',
                                    encoding='utf-8')
    os.environ.clear()
    os.environ.update(env)
    os.chdir(home / 'work')
    # Do not search the task/caller directory for vendor imports. The other
    # existing interpreter paths are the pinned venv and stdlib.
    sys.path[:] = [str(source)] + [p for p in sys.path if p and Path(p).resolve() != ROOT / 'scripts']
    sys.dont_write_bytecode = True
    sys.addaudithook(audit_guard(port))
    signal.signal(signal.SIGALRM, _deadline)
    signal.alarm(WALL_SECONDS)
    sink = BoundedSink()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            from run_agent import AIAgent
            result = run_sdk(prompt, base_url, home, AIAgent)
    except Exception as exc:
        result = safe_failure(exc)

    finally:
        signal.alarm(0)
    result['sdk_log_bytes'] = sink.count
    # Exclusive write: parent owns this output path and rejects stale outputs.
    with output.open('x', encoding='utf-8') as target:
        target.write(json.dumps(result, ensure_ascii=False) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('source-root', 'base-url', 'prompt-file', 'output-file', 'home-dir'):
        parser.add_argument('--' + flag, required=True)
    args = parser.parse_args(argv)
    try:
        result = execute(args.source_root, args.base_url, args.prompt_file, args.output_file, args.home_dir)
    except Exception as exc:
        result = safe_failure(exc)
    print(json.dumps({key: value for key, value in result.items() if key != 'text'}, ensure_ascii=False))
    return 0 if result['status'] == 'response_received' else 1


if __name__ == '__main__':
    raise SystemExit(main())

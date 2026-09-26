"""Optional Hermes transport over the EXISTING durably reserved Qwen attempt.

Hermes provides the SDK turn, not another model provider or a new allowance.
The gateway sends only the exact canonical public prompt, not Hermes' system
instructions. No tools, model-proposed code execution, fallback or extra calls.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading

from .free_gateway import SECRET, server_for
from ..autonomy.qwen_space import MODEL, MAX_PROMPT_CHARS

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = 'Hermes protocol fixture. No remote model was called.'


def validate_payload(payload, prompt):
    if not isinstance(payload, dict) or payload.get('model') != MODEL:
        raise ValueError('model_not_allowed')
    if payload.get('tools') or payload.get('functions') or payload.get('tool_choice') not in (None, 'none'):
        raise ValueError('tools_not_allowed')
    messages = payload.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 8:
        raise ValueError('invalid_messages')
    users = []
    for message in messages:
        if (not isinstance(message, dict) or set(message) - {'role', 'content'}
                or message.get('role') not in {'system', 'user'} or not isinstance(message.get('content'), str)):
            raise ValueError('text_only')
        if SECRET.search(message['content']):
            raise ValueError('secret_rejected')
        if message['role'] == 'user':
            users.append(message['content'])
    if users != [prompt] or len(json.dumps(messages).encode()) > 64000:
        raise ValueError('canonical_prompt_required')


class ReservedQwenTurn:
    def __init__(self, prompt, provider=None):
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= MAX_PROMPT_CHARS or SECRET.search(prompt):
            raise ValueError('invalid_prompt')
        self.prompt, self.provider = prompt, provider
        self.lock = threading.Lock()
        self.attempted = False
        self.result = None
        self.receipt = {'harness': 'hermes-agent', 'model': MODEL, 'harness_requests': 0,
                        'provider_adapter_calls': 0, 'status': 'waiting_for_harness',
                        'live': provider is not None, 'tools': [], 'paid_fallback': False,
                        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}

    def complete(self, payload):
        with self.lock:
            self.receipt['harness_requests'] += 1
            if self.attempted:
                raise ValueError('already_attempted')
            self.attempted = True
            self.receipt['status'] = 'request_rejected'
            validate_payload(payload, self.prompt)
            if self.provider is None:
                self.result = {'status': 'protocol_fixture_received', 'text': FIXTURE}
            else:
                self.receipt['provider_adapter_calls'] = 1
                self.receipt['status'] = 'request_started'
                try:
                    self.result = self.provider(self.prompt)
                except Exception:
                    # Never expose third-party exception bodies in cloud logs.
                    self.result = {'status': 'request_failed'}
                if not isinstance(self.result, dict):
                    self.result = {'status': 'invalid_response'}
            self.receipt['status'] = self.result.get('status', 'failed')
            if self.receipt['status'] not in {'response_received', 'protocol_fixture_received'}:
                raise ValueError('provider_failed')
            text = self.result.get('text')
            if not isinstance(text, str) or not text.strip() or len(text.encode()) > 65536 or SECRET.search(text):
                self.receipt['status'] = 'invalid_response'
                self.result = {'status': 'invalid_response'}
                raise ValueError('invalid_response')
            self.receipt['output_sha256'] = hashlib.sha256(text.encode()).hexdigest()
            return text


def finish(guard, process, output):
    result = dict(guard.result or {'status': 'no_result'})
    # A failed provider response remains a failure regardless of harness text.
    if result.get('status') in {'response_received', 'protocol_fixture_received'}:
        if (process.get('status') != 'exited' or process.get('returncode') != 0
                or guard.receipt['harness_requests'] != 1
                or output.get('status') != 'response_received' or output.get('text') != result.get('text')):
            result = {'status': 'failed'}
    result['harness_receipt'] = {**guard.receipt, 'process': process,
        'accepted': result.get('status') in {'response_received', 'protocol_fixture_received'}}
    return result


def run(prompt, *, provider=None, output_dir=None):
    if sys.platform != 'linux' or not (os.environ.get('GITHUB_ACTIONS') == 'true' or os.environ.get('COLAB_RELEASE_TAG')):
        raise ValueError('authorized_linux_cloud_required')
    from scripts.run_dsh_free_review import bounded_process
    install = ROOT / 'runtime/hermes-harness'
    python = install / 'venv/bin/python'
    source = install / 'source'
    if not python.is_file() or not (install / 'receipt.json').is_file():
        return {'status': 'dependency_unavailable'}
    output_dir = Path(output_dir or ROOT / 'runtime/hermes-fixture')
    if output_dir.is_symlink() or output_dir.parent.is_symlink():
        raise ValueError('symlink_output_refused')
    output_dir.mkdir(parents=True, exist_ok=False)
    prompt_path, result_path = output_dir / 'prompt.txt', output_dir / 'sdk-result.json'
    prompt_path.write_text(prompt, encoding='utf-8')
    guard = ReservedQwenTurn(prompt, provider)
    server = server_for(guard, model=MODEL)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = {'PATH': os.environ.get('PATH', os.defpath), 'LANG': 'C.UTF-8',
                   'HOME': str(output_dir), 'GITHUB_ACTIONS': 'true', 'PYTHONDONTWRITEBYTECODE': '1'}
    process = {'status': 'not_started', 'returncode': None}
    output = {}
    try:
        process = bounded_process([str(python), str(ROOT / 'scripts/hermes_no_tools.py'),
            '--source-root', str(source), '--base-url', f'http://127.0.0.1:{server.server_port}/v1',
            '--prompt-file', str(prompt_path), '--output-file', str(result_path),
            '--home-dir', str(output_dir / 'home')], str(output_dir), environment, limit_seconds=160)
        (output_dir / 'harness.log').write_text(process.pop('log'), encoding='utf-8')
        if result_path.is_file() and result_path.stat().st_size <= 131072:
            value = json.loads(result_path.read_text(encoding='utf-8'))
            if isinstance(value, dict):
                output = value
    except (OSError, ValueError, TypeError):
        process = {'status': 'runner_failed', 'returncode': None}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    result = finish(guard, process, output)
    (output_dir / 'receipt.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result


def main():
    # The standalone entry point has NO live flag. Only continuous.perform can
    # supply the live provider after its existing durable Git reservation.
    result = run('HERMES-PROTOCOL-001. Return the public protocol fixture unchanged.')
    print('HERMES_RECEIPT=' + json.dumps(result, ensure_ascii=False))
    return 0 if result.get('status') == 'protocol_fixture_received' else 1


if __name__ == '__main__':
    raise SystemExit(main())

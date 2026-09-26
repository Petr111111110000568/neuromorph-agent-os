"""Cloud-only finite DSH run. Default fixture; live requires explicit --live.

The installed harness receives no repository write credential or account state.
All output remains a proposal. This script never executes model-generated code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.dsh_profile import command, prepare_profile
from workbench.harnesses.free_gateway import MODEL, OneShot, server_for

TASK_ID = 'DSH-FREE-REVIEW-001'
PROMPT = '''DSH-FREE-REVIEW-001. Public NeuroMorf / Meta-Harness task.
Base commit: 9b11d3a55d6072d9c516c0d485794074b91a9d36.
Parent: DEEPSEEK-011 / CLAUDE-MOD-004 / SOURCECRAFT-REVIEW-003 / PR11.
Public artifact: https://github.com/Petr111111110000568/neuromorph-agent-os/pull/11
All evidence is supplied below; no browsing, file access or test execution is available.
Measured synthetic M03 results: membership 5/10 correct answers and4/14 refusals;
scope-only7/10 and8/14; provenance9/10 and14/14,foreign0/5,expired0/2,revoked0/2.
A deliberately registered false claim passes integrity checks but is a wrong answer.
Counterexample from colleagues: identical answer text from a foreign project is
not correct evidence. Hashing cannot prove truth or registry authenticity.
SourceCraft Code Assistant also proposed a stale-response counterexample:
on HTTP429 a cached/template answer must not be labelled a fresh model result.
Controller correction: unique response IDs/timestamps or hashes alone do not
prove model weights or scientific truth. Failed transport must remain failed.
Next finite mission M02 is a backlog dispatcher for two synthetic projects:
task leases, version check before write, deduplication, bounded attempts, checkpoints.
Propose one concrete interleaving where an expired worker overwrites a newer
accepted result, then a minimal invariant and cloud regression to prevent it.
Give expected states before/after. Consider secrets, access, dependencies, untrusted
code and resources briefly. Do not request tools or code execution, change policy,
claim consensus, or claim that you ran tests. Answer in Russian under350words.
'''


def bounded_process(argv, cwd, environment, limit_seconds=100):
    process = subprocess.Popen(argv, cwd=cwd, env=environment, shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + limit_seconds
    status = 'exited'
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                status = 'timeout'
                break
            for key, _ in selector.select(0.2):
                chunk = os.read(key.fd, 16384)
                if not chunk:
                    selector.unregister(key.fileobj)
                    break
                output.extend(chunk)
                if len(output) > 512000:
                    status = 'output_limit'
                    break
            if status != 'exited':
                break
    finally:
        if status == 'exited':
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                status = 'timeout'
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        selector.close()
        process.stdout.close()
    return {'status': status, 'returncode': process.returncode,
            'log': output[:512000].decode('utf-8', errors='replace')}


def run(live=False):
    if sys.platform != 'linux' or not (os.environ.get('GITHUB_ACTIONS') == 'true' or os.environ.get('COLAB_RELEASE_TAG')):
        raise ValueError('authorized_linux_cloud_required')
    install = ROOT / 'runtime/cloud-harnesses'
    installed = json.loads((install / 'receipt.json').read_text(encoding='utf-8'))
    entry = install / 'packages/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js'
    if not entry.is_file():
        raise ValueError('pinned_dsh_install_required')
    package = json.loads((entry.parent.parent / 'package.json').read_text(encoding='utf-8'))
    if package.get('name') != '@deepseek-ai/dsh' or package.get('version') != '0.1.7-rc.2':
        raise ValueError('unexpected_dsh_package')
    output = ROOT / 'runtime' / ('dsh-live' if live else 'dsh-fixture')
    if output.is_symlink() or output.parent.is_symlink():
        raise ValueError('symlink_output_refused')
    output.mkdir(exist_ok=False)  # A second live run cannot silently reuse/reset a reservation.
    receipt_path = output / 'receipt.json'
    def reserve():
        with (output / 'attempt.json').open('x', encoding='utf-8') as file:
            json.dump({'task_id': TASK_ID, 'attempts': 1, 'max_attempts': 1}, file)
            file.flush()
            os.fsync(file.fileno())
    guard = OneShot(TASK_ID, live=live, reserve=reserve)
    server = server_for(guard)
    profile = prepare_profile(output / 'profile-home', port=server.server_port, prompt=PROMPT)
    guard.receipt.update({'base_commit': '9b11d3a55d6072d9c516c0d485794074b91a9d36',
        'profile_sha256': profile['config_sha256'], 'tools': profile['tools'],
        'installation_receipt_sha256': hashlib.sha256((install / 'receipt.json').read_bytes()).hexdigest(),
        'execution_scope': 'finite_cloud_process_not_continuous_native_chat_bridge'})
    receipt_path.write_text(json.dumps(guard.receipt, indent=2) + '\n', encoding='utf-8')
    environment = {'PATH': os.environ.get('PATH', os.defpath), 'HOME': profile['home'],
        'LANG': 'C.UTF-8', 'XDG_CONFIG_HOME': profile['home'],
        'XDG_DATA_HOME': profile['home'], 'XDG_CACHE_HOME': profile['home'],
        'NODE_OPTIONS': '--max-old-space-size=512', **profile['environment']}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        process = bounded_process(command(shutil.which('node'), entry), profile['cwd'], environment)
        (output / 'harness.log').write_text(process.pop('log'), encoding='utf-8')
        guard.receipt['harness_process'] = process
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        guard.receipt['elapsed_seconds'] = round(time.monotonic() - started, 3)
        guard.receipt['model_claims_unverified'] = True
        guard.receipt['harness_log_sha256'] = hashlib.sha256((output / 'harness.log').read_bytes()).hexdigest() if (output / 'harness.log').exists() else None
        receipt_path.write_text(json.dumps(guard.receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return guard.receipt


def accepted_result(result, live):
    expected = 'response_received' if live else 'protocol_fixture_received'
    process = result.get('harness_process', {})
    return result.get('status') == expected and process.get('status') == 'exited' and process.get('returncode') == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'status': 'plan_only', 'live': args.live, 'model': MODEL,
                          'max_upstream_requests': 1 if args.live else 0, 'task_id': TASK_ID}))
        return 0
    result = run(args.live)
    print('DSH_REVIEW_RECEIPT=' + json.dumps(result, ensure_ascii=False))
    return 0 if accepted_result(result, args.live) else 1


if __name__ == '__main__':
    raise SystemExit(main())

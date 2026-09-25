"""Fail-closed Actions artifact chain for one finite public cloud-review window.

Only select() receives a read-only GitHub token. Model/discovery subprocesses get
an explicit clean environment. Imported functions perform no IO until called.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .daemon import ROOT, _safe_path, _save_state
from .providers import NoRedirect, json_load
from ..resource_policy import load_policy

REPOSITORY = 'Petr111111110000568/neuromorph-agent-os'
WORKFLOW = '.github/workflows/cloud-review.yml'
CONFIG = 'config/cloud_review_schedule.json'
CONTROL = ROOT / 'runtime/cloud-actions-control'
INCOMING = CONTROL / 'incoming'
BUNDLE = CONTROL / 'bundle'
REVIEWS = ROOT / 'runtime/cloud-actions-reviews'
STATE = REVIEWS / 'state.json'
INTAKE = ROOT / 'runtime/continuous-discovery/cycle.json'
MAX_BUNDLE = 128 * 1024
ALLOWED_FILES = {'checkpoint.json', 'cloud-state.json', 'last-report.json'}


class SafetyError(ValueError):
    """Only fixed, non-sensitive messages may be exposed by the CLI."""


def _read(path, limit=192 * 1024):
    with _safe_path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise SafetyError('Local JSON exceeds its limit')
    return json_load(raw)


def _write(path, value):
    _safe_path(path)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value):
        raise SafetyError('Invalid fixed window timestamp')
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()


def validate_config(value):
    expected = {'schema_version', 'zero_budget_confirmed', 'budget_evidence', 'starts_at', 'expires_at',
                'max_workflow_runs', 'max_model_attempts', 'interval_seconds'}
    if type(value) is not dict or set(value) != expected or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise SafetyError('Invalid cloud Actions configuration')
    if type(value['zero_budget_confirmed']) is not bool:
        raise SafetyError('Budget confirmation must be explicit')
    evidence = value['budget_evidence']
    if (type(evidence) is not dict or set(evidence) != {'checked_at', 'product', 'budget_usd', 'stop_usage'}
            or evidence['product'] != 'Actions' or type(evidence['budget_usd']) is not int
            or evidence['budget_usd'] != 0 or evidence['stop_usage'] is not True
            or not isinstance(evidence['checked_at'], str)):
        raise SafetyError('An explicit zero Actions budget with Stop usage is required')
    datetime.strptime(evidence['checked_at'], '%Y-%m-%d')
    for name, required in (('max_workflow_runs', 28), ('max_model_attempts', 28), ('interval_seconds', 21600)):
        if type(value[name]) is not int or value[name] != required:
            raise SafetyError('Cloud Actions limits are fixed at 28 attempts and six hours')
    if not 0 < _utc(value['expires_at']) - _utc(value['starts_at']) <= 7 * 86400:
        raise SafetyError('Cloud Actions window must be at most seven days')
    return value


def _settings():
    value = validate_config(_read(ROOT / CONFIG))
    return value, hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _outputs(**items):
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
        for key, value in items.items():
            text = str(value).lower() if type(value) is bool else str(value)
            if not re.fullmatch(r'[a-z_]+', key) or not re.fullmatch(r'[a-zA-Z0-9_.-]*', text):
                raise SafetyError('Unsafe Actions output')
            stream.write(key + '=' + text + '\n')


def _api(path):
    if not path.startswith('/repos/' + REPOSITORY + '/'):
        raise SafetyError('Unexpected GitHub API path')
    token = os.environ.get('GH_TOKEN', '')
    if not token or any(char.isspace() for char in token):
        raise SafetyError('Read-only GitHub token is unavailable')
    url = 'https://api.github.com' + path
    request = Request(url, headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2026-03-10',
        'User-Agent': 'Meta-Harness-Finite-Cloud-Actions'})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
            if response.status != 200 or response.geturl() != url:
                raise SafetyError('Unexpected GitHub API response')
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise SafetyError('GitHub response exceeds bound')
            return json.loads(raw.decode('utf-8'))
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise SafetyError('GitHub API failed with HTTP ' + str(code)) from None


def _id(value):
    if type(value) is not int or value <= 0:
        raise SafetyError('Invalid GitHub identifier')
    return value


def select():
    settings, settings_hash = _settings()
    now = time.time()
    _outputs(active=False, restore=False)
    if not settings['zero_budget_confirmed'] or not _utc(settings['starts_at']) <= now < _utc(settings['expires_at']):
        print('No work: zero budget is unconfirmed or the fixed window is closed.')
        return
    if os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or os.environ.get('GITHUB_RUN_ATTEMPT') != '1':
        raise SafetyError('Wrong repository or forbidden workflow rerun')
    run_id, run_number = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_NUMBER'])
    if not 1 <= run_number <= 28:
        raise SafetyError('Finite workflow run limit reached')
    branch = os.environ['CLOUD_DEFAULT_BRANCH']
    current = _api(f'/repos/{REPOSITORY}/actions/runs/{run_id}')
    workflow_id = _id(current.get('workflow_id'))
    if (current.get('id') != run_id or current.get('run_number') != run_number or current.get('run_attempt') != 1
            or current.get('head_branch') != branch or current.get('head_sha') != os.environ['GITHUB_SHA']
            or current.get('path', '').split('@')[0] != WORKFLOW
            or current.get('event') not in {'schedule', 'workflow_dispatch'}
            or current.get('repository', {}).get('private') is not False):
        raise SafetyError('Workflow provenance does not match the public default branch')
    listing = _api(f'/repos/{REPOSITORY}/actions/workflows/{workflow_id}/runs?per_page=100')
    runs = listing.get('workflow_runs', [])
    if not isinstance(runs, list) or listing.get('total_count') != len(runs):
        raise SafetyError('Incomplete workflow history; cannot prove the attempt ledger')
    prior = {item.get('run_number'): item for item in runs if isinstance(item, dict)
             and type(item.get('run_number')) is int and item['run_number'] < run_number}
    if set(prior) != set(range(1, run_number)):
        raise SafetyError('Missing workflow history; no automatic counter reset')
    previous = None
    for number in range(run_number - 1, 0, -1):
        item = prior[number]
        _id(item.get('id'))
        if item.get('workflow_id') != workflow_id or item.get('head_branch') != branch:
            raise SafetyError('Prior run is not from the same workflow and branch')
        sha = item.get('head_sha', '')
        if not re.fullmatch(r'[a-f0-9]{40}', sha):
            raise SafetyError('Invalid prior commit')
        if sha != current['head_sha']:
            # A disabled draft may precede activation. Prove its exact config from
            # the immutable commit; an active changed commit cannot reset state.
            entry = _api(f'/repos/{REPOSITORY}/contents/{CONFIG}?' + urlencode({'ref': sha}))
            if entry.get('encoding') != 'base64' or type(entry.get('size')) is not int or entry['size'] > 4096:
                raise SafetyError('Cannot verify the previous budget gate')
            old = validate_config(json_load(base64.b64decode(entry['content'], validate=False)))
            if old['zero_budget_confirmed'] is False:
                continue
            raise SafetyError('Active workflow commit changed; manual review is required')
        if item.get('status') != 'completed' or item.get('run_attempt') != 1:
            raise SafetyError('Previous run is incomplete or rerun; stop conservatively')
        previous = item
        break
    selected = {'schema_version': 1, 'settings_sha256': settings_hash, 'repository': REPOSITORY,
        'workflow_id': workflow_id, 'run_id': run_id, 'run_number': run_number, 'head_sha': current['head_sha'],
        'previous_run_id': previous['id'] if previous else None, 'previous_run_number': previous['run_number'] if previous else None}
    CONTROL.mkdir(parents=True, exist_ok=True)
    _write(CONTROL / 'selection.json', selected)
    if previous:
        name = f"cloud-review-checkpoint-{previous['id']}-1"
        data = _api(f"/repos/{REPOSITORY}/actions/runs/{previous['id']}/artifacts?per_page=100")
        matches = [a for a in data.get('artifacts', []) if isinstance(a, dict) and a.get('name') == name]
        if len(matches) != 1 or matches[0].get('expired') is not False or not 0 < matches[0].get('size_in_bytes', 0) <= MAX_BUNDLE:
            raise SafetyError('Previous checkpoint is absent, expired, duplicated or too large')
        artifact_id = _id(matches[0]['id'])
        _outputs(restore=True, artifact_id=artifact_id, previous_run_id=previous['id'])
    _outputs(active=True)


def prepare():
    selected = _read(CONTROL / 'selection.json')
    settings, settings_hash = _settings()
    if not settings['zero_budget_confirmed'] or selected['settings_sha256'] != settings_hash:
        raise SafetyError('Selected schedule configuration changed')
    REVIEWS.mkdir(parents=True, exist_ok=True)
    INTAKE.parent.mkdir(parents=True, exist_ok=True)
    prior = None
    if selected['previous_run_id'] is not None:
        files = list(INCOMING.iterdir())
        if any(not path.is_file() or path.is_symlink() or path.name not in ALLOWED_FILES for path in files):
            raise SafetyError('Checkpoint contains unexpected files')
        if sum(path.stat().st_size for path in files) > MAX_BUNDLE:
            raise SafetyError('Checkpoint extraction exceeds bound')
        prior = _read(INCOMING / 'checkpoint.json', 4096)
        if type(prior) is not dict or set(prior) != set(selected) | {'halted', 'state_sha256', 'report_sha256'}:
            raise SafetyError('Unknown checkpoint fields')
        for name in ('repository', 'workflow_id', 'head_sha', 'settings_sha256'):
            if prior.get(name) != selected[name]:
                raise SafetyError('Restored checkpoint provenance mismatch')
        if (type(prior.get('schema_version')) is not int or prior.get('schema_version') != 1 or prior.get('run_id') != selected['previous_run_id']
                or prior.get('run_number') != selected['previous_run_number'] or type(prior.get('halted')) is not bool):
            raise SafetyError('Restored checkpoint identity mismatch')
        for filename, key in (('cloud-state.json', 'state_sha256'), ('last-report.json', 'report_sha256')):
            path = INCOMING / filename
            digest = prior.get(key)
            if digest is None:
                if path.exists():
                    raise SafetyError('Unreferenced checkpoint file')
            elif not path.is_file() or _hash(path) != digest:
                raise SafetyError('Restored checkpoint file hash mismatch')
        if prior.get('state_sha256') is not None:
            state = _read(INCOMING / 'cloud-state.json', 4096)
            if type(state.get('attempts')) is not int or not 0 <= state['attempts'] <= 28:
                raise SafetyError('Restored model attempt count is invalid')
            STATE.write_bytes((INCOMING / 'cloud-state.json').read_bytes())
        elif prior['halted'] is not True:
            raise SafetyError('Active previous run has no durable model state')
    _write(CONTROL / 'prepared.json', {'halted': prior['halted'] if prior else False})
    _outputs(ready=True)


def _command(arguments, seconds, log):
    environment = {'PATH': os.environ.get('PATH', os.defpath), 'LANG': 'C.UTF-8',
                   'LC_ALL': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    with log.open('wb') as stream:
        process = subprocess.Popen(arguments, cwd=ROOT, env=environment, shell=False,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            return 124


def execute():
    settings, _ = _settings()
    if not settings['zero_budget_confirmed']:
        raise SafetyError('Zero budget confirmation was withdrawn')
    prepared = _read(CONTROL / 'prepared.json', 4096)
    if prepared['halted']:
        print('Previous error halted this window; no automatic retry.')
        return
    load_policy(ROOT)
    if not _utc(settings['starts_at']) <= time.time() < _utc(settings['expires_at']) - 180:
        _write(CONTROL / 'execution.json', {'halted': True, 'status': 'window_closed'})
        return
    if STATE.exists() and _read(STATE, 4096).get('next_due', 0) > time.time():
        print('Checkpoint is not due; no discovery or model request.')
        return
    # Mark uncertain completion before either bounded subprocess. Finalization
    # will halt if this process is killed before it records a trusted outcome.
    _write(CONTROL / 'execution.json', {'halted': True, 'status': 'interrupted'})
    code = _command([sys.executable, '-m', 'workbench.autonomy', '--config', 'config/autonomy.json',
        '--output-dir', str(INTAKE.parent), '--online', '--provider', 'none'], 120, CONTROL / 'discovery.log')
    if code or time.time() >= _utc(settings['expires_at']) - 130:
        _write(CONTROL / 'execution.json', {'halted': True, 'status': 'discovery_failed_or_window_closed'})
        return
    previous_attempts = _read(STATE, 4096).get('attempts', 0) if STATE.exists() else 0
    code = _command([sys.executable, '-m', 'workbench.autonomy.cloud_review', '--intake', str(INTAKE),
        '--output-dir', str(REVIEWS), '--state-file', str(STATE), '--stop-file', str(REVIEWS / 'STOP'),
        '--interval', '21600', '--max-runs', '28', '--once'], 125, CONTROL / 'review.log')
    state = _read(STATE, 4096) if STATE.exists() else {}
    status = state.get('last_status', 'failed')
    delayed_retry = (code == 2 and status in {'provider_unavailable', 'transport_unavailable'}
                     and state.get('attempts') == previous_attempts + 1)
    if delayed_retry:
        # A transient outage consumes its attempt. Preserve a full day cooldown
        # atomically before upload; never rotate sessions or retry in this run.
        state['next_due'] = max(state['next_due'], time.time() + 86400)
        _save_state(STATE, state)
    continuing = delayed_retry or (code == 0 and status in {'response_received', 'no_public_metadata', 'rate_limited'})
    _write(CONTROL / 'execution.json', {'halted': not continuing, 'status': status})


def pack():
    selected = _read(CONTROL / 'selection.json')
    prepared = _read(CONTROL / 'prepared.json', 4096)
    execution = _read(CONTROL / 'execution.json', 4096) if (CONTROL / 'execution.json').exists() else prepared
    checkpoint = dict(selected, halted=bool(execution.get('halted', True)), state_sha256=None, report_sha256=None)
    BUNDLE.mkdir()
    if STATE.exists():
        state = _read(STATE, 4096)
        prior_count = _read(INCOMING / 'cloud-state.json', 4096)['attempts'] if (INCOMING / 'cloud-state.json').exists() else 0
        if type(state.get('attempts')) is not int or not prior_count <= state['attempts'] <= min(28, prior_count + 1):
            raise SafetyError('Model counter moved outside one reserved attempt')
        (BUNDLE / 'cloud-state.json').write_bytes(STATE.read_bytes())
        checkpoint['state_sha256'] = _hash(BUNDLE / 'cloud-state.json')
        report = REVIEWS / f"cloud-{state['attempts']:03d}.json"
        if not report.is_file():
            report = INCOMING / 'last-report.json'
        if report.is_file():
            value = _read(report, 64 * 1024)
            if value.get('validated') is not False or value.get('execution_allowed') is not False:
                raise SafetyError('Only explicitly unverified data may be archived')
            (BUNDLE / 'last-report.json').write_bytes(report.read_bytes())
            checkpoint['report_sha256'] = _hash(BUNDLE / 'last-report.json')
    elif checkpoint['halted'] is not True:
        raise SafetyError('No active checkpoint may reset a missing model state')
    _write(BUNDLE / 'checkpoint.json', checkpoint)
    if sum(path.stat().st_size for path in BUNDLE.iterdir()) > MAX_BUNDLE:
        raise SafetyError('Upload bundle exceeds 128 KiB')
    _outputs(ready=True)
    print(json.dumps({'status': 'checkpoint_ready', 'halted': checkpoint['halted'], 'run_number': selected['run_number']}))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Finite cloud Actions orchestration only')
    parser.add_argument('operation', choices=('select', 'prepare', 'execute', 'pack'))
    args = parser.parse_args(argv)
    try:
        globals()[args.operation]()
    except SafetyError as exc:
        print('Cloud Actions stopped: ' + str(exc), file=sys.stderr)
        return 2
    except Exception:
        print('Cloud Actions stopped: unavailable API, local state or storage; no reset.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


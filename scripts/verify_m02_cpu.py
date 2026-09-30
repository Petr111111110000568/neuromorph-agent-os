"""Finite pinned CPU experiment, with recovery on a second cloud worker.

Only the existing coupled_dynamics builtin executes. The seed deliberately
exits after Queue.finish; resume verifies an explicitly identified checkpoint.
No model, provider credentials or arbitrary handler is involved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.m02_checkpoint import create_checkpoint, restore_checkpoint
from workbench.m02_cpu import run_cpu_once
from workbench.network.queue import Queue
from workbench.project_dispatcher import POLICY, ProjectDispatcher

BASE_COMMIT = '898162ee6208be03c5d5cf294c3d264f10f7de1b'
REPOSITORY = 'Petr111111110000568/neuromorph-agent-os'
WORKFLOW = '.github/workflows/m02-cpu.yml'
BRANCH = 'autonomy/m02-cpu-coupled'
PARAMETERS = dict(steps=80, replicates=3, recovery=0.7, coupling=0,
                  load=0.4, perturbation=0.8, uncertainty=0.1, seed=17)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def identity():
    result = dict(repository=os.environ.get('GITHUB_REPOSITORY', ''),
                  workflow_id=WORKFLOW, branch=os.environ.get('GITHUB_REF_NAME', ''),
                  commit=os.environ.get('GITHUB_SHA', ''), run_id=os.environ.get('GITHUB_RUN_ID', ''))
    require(result['repository'] == REPOSITORY, 'wrong_repository')
    require(result['branch'] in (BRANCH, 'main'), 'wrong_branch')
    require(os.environ.get('GITHUB_EVENT_NAME') in ('push', 'workflow_dispatch'), 'wrong_event')
    require(os.environ.get('GITHUB_RUN_ATTEMPT') == '1', 'reruns_not_supported')
    require(os.environ.get('GITHUB_WORKFLOW_REF') ==
            f"{REPOSITORY}/{WORKFLOW}@refs/heads/{result['branch']}", 'wrong_workflow')
    checked = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(checked == result['commit'], 'checkout_mismatch')
    return result


def business(dispatcher, queue):
    """Predeclared B: identity, budgets, effects, reviews; exclude maintenance."""
    state = dispatcher.snapshot()
    return dict(
        budget=state['budget'],
        tasks=sorted((t['task_id'], t['project_id'], t['revision'], t['handoff_id'],
                      t['input_sha256'], t['current_result_sha256']) for t in state['tasks']),
        intents=sorted((i['project_id'], i['task_id'], i['handoff_id'], i['revision'],
                        i['input_sha256'], i['queue_key'], i['reserved_attempts']) for i in state['intents']),
        jobs=sorted((j['id'], j['kind'], canonical(j['payload']), j['max_attempts'],
                     j['attempts'], canonical(j['result'])) for j in queue.jobs()),
        artifacts=sorted((a['sha256'], canonical(a['content'])) for a in state['artifacts']),
        cpu_results=sorted((r['queue_job_id'], r['sha256'], canonical(r['content']))
                           for r in state.get('cpu_results', [])),
        publications=sorted((e['event_id'], e['task_id'], e['handoff_id'], e['revision'],
                             e['result_sha256']) for e in state['outbox']),
        reviews=sorted((r['task_id'], r['handoff_id'], r['revision'], r['result_sha256'],
                        r['decision'], r['note']) for r in state['reviews']))


def open_state(path):
    queue = Queue(path / 'queue.sqlite')
    return ProjectDispatcher(path / 'control.sqlite', queue, cpu_root=ROOT), queue


def close_state(dispatcher, queue):
    dispatcher.close()
    queue.close()


def task(dispatcher, task_id):
    return next(t for t in dispatcher.snapshot()['tasks'] if t['task_id'] == task_id)


def approve(dispatcher, task_id):
    target = task(dispatcher, task_id)
    require(target['current_result_sha256'], 'first_result_missing')
    dispatcher.review(task_id, expected_revision=target['revision'],
                      result_sha256=target['current_result_sha256'], decision='accepted',
                      note='Finite engineering control only; no scientific validation.')
    dispatcher.deliver_outbox()


def spec(task_id, project_id, **overrides):
    return dict(task_id=task_id, project_id=project_id, goal='Check the fixed coupled CPU invariant.',
                dependencies=[], revision=1, handoff_id=task_id + '-R1', base_commit=BASE_COMMIT,
                handler='m02.cpu.coupled.v1', parameters={**PARAMETERS, **overrides},
                limitations=['Dimensionless engineering experiment; not biological validation.'])


def crash_worker(path):
    dispatcher, queue = open_state(path)
    def crash(stage):
        if stage == 'after_queue_finish':
            os._exit(71)
    try:
        run_cpu_once(dispatcher, root=ROOT, worker_id='cpu-crash', failpoint=crash)
    finally:
        close_state(dispatcher, queue)
    raise ValueError('crash_boundary_not_reached')


def seed(path, output):
    current = identity()
    require(not path.exists() and not output.exists(), 'seed_must_use_fresh_paths')
    path.mkdir(parents=True)
    dispatcher, queue = open_state(path)
    try:
        for project_id in ('negative-control', 'coupling-control'):
            dispatcher.create_project(dict(project_id=project_id,
                mission='Finite CPU engineering controls', allowed_artifacts=['synthetic-evidence'],
                closure_criteria=['First effect, numerical control and durable recovery verified'],
                resource_policy=dict(POLICY)))
        for contract in (spec('negative', 'negative-control', perturbation=0),
                         spec('coupling-zero', 'coupling-control', coupling=0)):
            dispatcher.create_task(contract)
            dispatcher.tick()
            run_cpu_once(dispatcher, root=ROOT, worker_id='cpu-control')
            dispatcher.tick()
            approve(dispatcher, contract['task_id'])
        dispatcher.create_task(spec('coupling-one', 'coupling-control', coupling=1))
        dispatcher.tick()
    finally:
        close_state(dispatcher, queue)
    child = subprocess.run([sys.executable, '-I', '-B', __file__, 'crash', '--state', str(path)],
                           cwd=ROOT, timeout=30, check=False)
    require(child.returncode == 71, 'expected_process_crash_missing')
    dispatcher, queue = open_state(path)
    try:
        projection = business(dispatcher, queue)
        require(projection['budget']['reserved_attempts'] == 6, 'seed_reserve_mismatch')
        require(projection['budget']['actual_attempts'] == 3, 'seed_attempts_mismatch')
        require(len(projection['artifacts']) == 2, 'unexpected_seed_artifacts')
        require(all(j['status'] == 'completed' for j in queue.jobs()), 'queue_finish_not_durable')
        require(task(dispatcher, 'coupling-one')['current_result_sha256'] is None, 'early_promotion')
        receipt = dict(schema_version=1, phase='seed', identity=current,
                       business_sha256=digest(projection), budget=projection['budget'],
                       crash_exit_code=71, crash_boundary='after_queue_finish',
                       pending_task='coupling-one', model_calls=0, additional_spend_usd=0)
    finally:
        close_state(dispatcher, queue)
    (path / 'seed-receipt.json').write_text(canonical(receipt) + '\n', encoding='utf-8')
    manifest_sha = create_checkpoint(path, output, current)
    summary = dict(manifest_sha256=manifest_sha, **receipt)
    print('M02_CPU_SEED=' + canonical(summary))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('manifest_sha256=' + manifest_sha + '\n')


def controls(results):
    def series(task_id, name):
        return next(s['points'] for s in results[task_id]['scientific_payload']['series'] if s['name'] == name)
    def distance(a, b):
        require(len(a) == len(b) == PARAMETERS['steps'] + 1, 'trajectory_size')
        require(all(x['x'] == y['x'] == i * .05 for i, (x, y) in enumerate(zip(a, b))), 'time_grid')
        return max(abs(x['y'] - y['y']) for x, y in zip(a, b))
    negative = distance(series('negative', 'Базовая модель'), series('negative', 'Модель с возмущением'))
    mean_error = max(distance(series('coupling-zero', name), series('coupling-one', name))
                     for name in ('Базовая модель', 'Модель с возмущением'))
    local_change = distance(series('coupling-zero', 'Компонент 1 · возмущённый'),
                            series('coupling-one', 'Компонент 1 · возмущённый'))
    negative_metrics = results['negative']['scientific_payload']['metrics']
    require(negative == 0 and all(m['value'] == 0 for m in negative_metrics[:2]), 'negative_control_failed')
    require(mean_error <= 1e-12, 'mean_invariance_failed')
    require(local_change > 1e-6, 'first_nonzero_local_effect_missing')
    return dict(negative_max_abs=negative, negative_integral=negative_metrics[0]['value'],
                negative_residual=negative_metrics[1]['value'], mean_max_abs=mean_error,
                mean_tolerance=1e-12, local_max_change=local_change, minimum_local_change=1e-6)


def resume(path, incoming, output, expected_sha):
    current = identity()
    require(not path.exists() and not output.exists(), 'resume_must_not_overwrite_state')
    # expected_sha comes from this workflow's authenticated seed job output.
    # Download step is scoped to this exact run and fixed artifact name.
    restored = restore_checkpoint(incoming, path, expected_sha, current)
    seed_receipt = json.loads((path / 'seed-receipt.json').read_text(encoding='utf-8'))
    require(seed_receipt['phase'] == 'seed' and seed_receipt['identity'] == current, 'seed_identity')
    dispatcher, queue = open_state(path)
    try:
        initial = business(dispatcher, queue)
        require(digest(initial) == seed_receipt['business_sha256'], 'restored_business_mismatch')
        # No worker is run during recovery: the completed Queue result is reused.
        dispatcher.tick()
        approve(dispatcher, 'coupling-one')
        first = business(dispatcher, queue)
        require(len(first['artifacts']) == len(first['publications']) == 3, 'promotion_first_effect')
        require(first['budget']['reserved_attempts'] == 6 and first['budget']['actual_attempts'] == 3,
                'recovery_consumed_attempt_or_reserve')
        dispatcher.tick()
        approve(dispatcher, 'coupling-one')
        dispatcher.tick()
        repeated = business(dispatcher, queue)
        require(first == repeated, 'repeat_changed_business_state')
        results = {a['content']['input']['task_id']: a['content'] for a in dispatcher.snapshot()['artifacts']}
        require(set(results) == {'negative', 'coupling-zero', 'coupling-one'}, 'result_identity')
        numerical = controls(results)
        receipt = dict(schema_version=1, phase='resume', identity=current,
                       manifest_sha256=expected_sha, restored_file_hashes=restored['files'],
                       restored_business_sha256=digest(initial), first_business_sha256=digest(first),
                       repeated_business_sha256=digest(repeated), business_repeat_equal=True,
                       numerical_controls=numerical, budget=first['budget'], results=results,
                       actual_handler='m02.cpu.coupled.v1', model_calls=0, network_calls=0,
                       additional_spend_usd=0, scientific_validation=False,
                       limitations=['Dimensionless synthetic engineering controls, no biological validation.',
                                    'Three CPU results, one deliberate process crash; not exactly-once computation.',
                                    'Same authenticated workflow run, two isolated jobs; checkpoint hash is not scientific proof.',
                                    'Trusted pinned subprocess is not an OS sandbox.'])
    finally:
        close_state(dispatcher, queue)
    output.mkdir(parents=True)
    (output / 'receipt.json').write_text(canonical(receipt) + '\n', encoding='utf-8')
    print('M02_CPU_RESUME=' + canonical({k: v for k, v in receipt.items() if k != 'results'}))
    print('M02_CPU_RECEIPT_SHA256=' + digest(receipt))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('seed', 'resume', 'crash'))
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--incoming', type=Path)
    args = parser.parse_args()
    if args.phase == 'seed':
        seed(args.state.resolve(), args.output.resolve())
    elif args.phase == 'resume':
        resume(args.state.resolve(), args.incoming.resolve(), args.output.resolve(),
               os.environ.get('EXPECTED_MANIFEST_SHA', ''))
    else:
        crash_worker(args.state.resolve())


if __name__ == '__main__':
    main()

"""Real localhost launcher, trusted worker processes and specialist workflow.

Uses synthetic models and local metadata only. No external agent or LLM is
contacted. The second session checks persisted episodic retrieval and cancel.
"""
import argparse
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_society import worker_processes, assert_no_credentials
from workbench.service import Service


def verify():
    with tempfile.TemporaryDirectory(prefix='meta-brain-proof-') as folder:
        folder = Path(folder)
        state = folder / 'state'
        log = folder / 'launcher.log'
        with log.open('w+') as stream:
            command = [sys.executable, '-m', 'workbench', '--data-dir', str(state),
                       'local-network', '--port', '0', '--hub-port', '0', '--federation-port', '0', '--workers', '2']
            process = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=stream,
                                       stdin=subprocess.DEVNULL, start_new_session=os.name == 'posix')
            observed = None
            try:
                deadline = time.monotonic() + 20
                port = None
                while time.monotonic() < deadline:
                    match = re.search(r'Meta-Harness: http://127\.0\.0\.1:(\d+)/', log.read_text())
                    if match:
                        port = int(match.group(1)); break
                    if process.poll() is not None:
                        raise AssertionError('Launcher exited before readiness')
                    time.sleep(.05)
                if port is None:
                    raise AssertionError('Launcher readiness deadline exceeded')
                observed = worker_processes(process.pid)
                if observed is not None:
                    assert len(observed) == 2

                def request(path, body=None):
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
                    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
                    connection.request('GET' if body is None else 'POST', path, payload,
                                       {} if body is None else {'Content-Type':'application/json'})
                    response = connection.getresponse()
                    raw, code, content_type = response.read(), response.status, response.getheader('Content-Type','')
                    connection.close()
                    assert code == 200, (path, code, raw[:200])
                    return json.loads(raw) if 'application/json' in content_type else raw

                for path in ('/brain.html', '/brain.js', '/brain.css'):
                    assert len(request(path)) > 300
                status = request('/api/brain')
                assert status['counts']['llm_agents_configured'] == 0
                session = request('/api/brain/start', {'question':'Сравнить KAN, кортикальную контекстную память и структурную пластичность на синтетических контрольных задачах.', 'seed':42})
                deadline = time.monotonic() + 40
                while session['status'] not in {'completed','partial','failed','cancelled'} and time.monotonic() < deadline:
                    time.sleep(.1)
                    # Coordinator advances automatically; the browser only reads.
                    session = request('/api/brain/session?id=' + session['id'])
                assert session['status'] == 'completed', (session['status'], session.get('issues'))
                assert len(session['jobs']) == 3 and len(set(session['jobs'].values())) == 3
                assert len(session['branches']) == 3
                assert all(b['status'] == 'accepted' and b['verification']['worker_attestation'] is False for b in session['branches'])
                assert {b['plugin_id'] for b in session['branches']} == {'kan_benchmark','cortical_sequence','structural_plasticity'}
                assert session['workspace']['aggregation'] == 'separate_experiments_no_cross_task_average'
                assert len({event['region'] for event in session['events']}) == 11
                event_count = len(session['events'])
                session_again = request('/api/brain/tick', {'id':session['id']})
                assert len(session_again['events']) == event_count
                jobs = {job['id']:job for job in session['job_details']}
                for job in jobs.values():
                    assert job['status'] == 'completed'
                    assert set(job['payload']) == {'plugin_id','parameters'}
                exported = request('/api/brain/export')
                assert_no_credentials(exported)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT) if os.name == 'posix' else process.terminate()
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=3)
                if observed and os.name == 'posix':
                    deadline = time.monotonic() + 3
                    while any(Path(f'/proc/{pid}').exists() for pid in observed) and time.monotonic() < deadline:
                        time.sleep(.05)
                    assert not any(Path(f'/proc/{pid}').exists() for pid in observed), 'Worker process did not stop'

        # A fresh coordinator process has no running workers. Read real durable
        # state and check retrieval/cancel without racing an actual computation.
        service = Service(ROOT, state / 'workbench.sqlite3')
        try:
            restored = service.brain.get(session['id'])
            assert restored['status'] == 'completed'
            followup = service.brain.start({'question':'Повторная проверка KAN с учётом прошлого вычислительного эпизода.',
                                           'plugins':['kan_benchmark'], 'seed':43, 'data_class':'internal'})
            assert any(m['kind']=='brain_episode' and m['id']==session['id'] for m in followup['memory'])
            cancelled = service.brain.cancel({'id':followup['id']})
            assert cancelled['status'] == 'cancelled'
            assert all(job['status']=='cancelled' for job in cancelled['job_details'])
            assert_no_credentials(service.brain.export())
        finally:
            service.close()
        return {'passed':True, 'checked_at':datetime.now(timezone.utc).isoformat(),
                'proof_type':'actual_http_and_two_local_worker_processes',
                'worker_processes_observed':len(observed) if observed is not None else None,
                'external_requests':0, 'external_agents_enrolled':0, 'llm_agents_configured':0,
                'session':session, 'restored_session_id':restored['id'],
                'followup_memory':followup['memory'], 'cancelled_followup':cancelled,
                'process_cleanup':'launcher_and_observed_workers_stopped',
                'limitations':['Single physical host','Programmed roles, not a human brain','No biological validation','Hashes assume trusted workers']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'examples/brain_workflow_run.json')
    args = parser.parse_args()
    proof = verify()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(proof,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'passed':proof['passed'],'worker_processes_observed':proof['worker_processes_observed'],
                      'branches':len(proof['session']['branches']),'regions_executed':len({e['region'] for e in proof['session']['events']}),
                      'memory_retrieved':len(proof['followup_memory']),'cancelled_followup':proof['cancelled_followup']['status']},ensure_ascii=False))

"""Read-only view of one public cloud ledger. Never dispatches a model/workflow."""
import copy
import json
import re
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

from .autonomy.providers import NoRedirect, json_load
from .autonomy.continuous import validate_state

REPOSITORY = 'Petr111111110000568/neuromorph-agent-os'
REF_URL = f'https://api.github.com/repos/{REPOSITORY}/git/ref/heads/autonomy%2Fcontinuous'
RAW_PREFIX = f'https://raw.githubusercontent.com/{REPOSITORY}/'
STATE_PATH = '/docs/contributions/continuous-state.json'
TTL = 300


def read_json(url, opener):
    request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'NeuroMorf-readonly-observer/1'})
    with opener(request, timeout=4) as response:
        if response.geturl() != url or response.status != 200:
            raise ValueError('public_ledger_unavailable')
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('public_ledger_too_large')
    return json_load(raw)


class CloudObserver:
    def __init__(self, opener=None, clock=time.time):
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect()).open
        self.clock, self.lock = clock, threading.Lock()
        self.snapshot, self.last_checked, self.last_error = None, None, None

    def get(self):
        with self.lock:
            now = int(self.clock())
            if self.last_checked is None or now - self.last_checked >= TTL:
                self.last_checked = now
                try:
                    ref = read_json(REF_URL, self.opener)
                    if (type(ref) is not dict or ref.get('ref') != 'refs/heads/autonomy/continuous'
                            or type(ref.get('object')) is not dict):
                        raise ValueError('invalid_ref')
                    sha = ref['object'].get('sha')
                    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
                        raise ValueError('invalid_ref')
                    state = validate_state(read_json(RAW_PREFIX + sha + STATE_PATH, self.opener))
                    journal = state['journal']
                    successes = [entry for entry in journal if entry['status'] == 'response_received']
                    message = state.get('last_message')
                    self.snapshot = {'fetched_at': now, 'ledger_commit': sha,
                        'attempts': state['attempts'], 'successes': state['successes'],
                        'next_due': state['next_due'], 'provider_blocked': state['provider_blocked'],
                        'pending': state['pending'], 'last_attempt': journal[-1] if journal else None,
                        'last_success_in_retained_journal': successes[-1] if successes else None,
                        'retained_success_text': {k: message[k] for k in ('summary', 'research')} if message else None}
                    self.last_error = None
                except Exception:
                    # A stale successful answer is never relabelled fresh after an error.
                    self.last_error = 'public_ledger_unavailable'
            return {'status': 'unavailable' if self.last_error else 'observed',
                'snapshot': copy.deepcopy(self.snapshot), 'stale': self.last_error is not None,
                'last_checked_at': self.last_checked, 'refresh_after': self.last_checked + TTL,
                'read_only': True, 'model_calls': 0,
                'source': f'https://github.com/{REPOSITORY}/tree/autonomy/continuous',
                'limitations': 'Snapshot of one public ledger; no native chat or model availability guarantee.'}

"""Public observer integration: immutable reads, bounded cache, honest failures."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workbench import cloud_observer as observer
from workbench.autonomy import continuous, qwen_space
from workbench.service import Service


NOW = 1_700_000_000
HEAD = 'a' * 40
SECOND_HEAD = 'b' * 40
MESSAGE = {'summary': 'Retained successful research', 'research': 'An unverified observation.',
           'python': '', 'next_question': 'What independent evidence is available?'}


def ref(sha=HEAD):
    return {'ref': 'refs/heads/autonomy/continuous', 'object': {'sha': sha, 'type': 'commit'}}


def successful_state():
    pending = continuous.reserve_state(continuous.initial_state(), NOW, '100', HEAD)
    return continuous.finish_state(pending, {'status': 'response_received', 'message': MESSAGE}, NOW)


def failure_after_success(status='request_failed'):
    success = successful_state()
    later = success['next_due']
    pending = continuous.reserve_state(success, later, '101', HEAD)
    return continuous.finish_state(pending, {'status': status}, later)


class Response:
    def __init__(self, url, data, *, status=200, effective_url=None):
        self.url, self.status = effective_url or url, status
        self.raw = data if type(data) is bytes else json.dumps(data).encode('utf-8')
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        self.read_sizes.append(limit)
        return self.raw[:limit]


class ScriptedOpener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.responses = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if not self.replies:
            raise AssertionError('Unexpected extra network read')
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        response = value if isinstance(value, Response) else Response(request.full_url, value)
        self.responses.append(response)
        return response


class CloudObserverTests(unittest.TestCase):
    def make_observer(self, replies):
        self.clock = [NOW]
        self.opener = ScriptedOpener(replies)
        return observer.CloudObserver(opener=self.opener, clock=lambda: self.clock[0])

    def test_immutable_sha_snapshot_uses_two_gets_without_credentials_or_model_calls(self):
        view = self.make_observer([ref(), successful_state()])
        with patch.object(qwen_space, 'call_qwen_space') as inference:
            result = view.get()
        inference.assert_not_called()
        self.assertEqual([request.full_url for request, _ in self.opener.requests],
            [observer.REF_URL, observer.RAW_PREFIX + HEAD + observer.STATE_PATH])
        for request, timeout in self.opener.requests:
            self.assertEqual(request.get_method(), 'GET')
            self.assertIsNone(request.data)
            self.assertNotIn('authorization', {key.lower() for key, _ in request.header_items()})
            self.assertEqual(timeout, 4)
        self.assertEqual(result['status'], 'observed')
        self.assertFalse(result['stale'])
        self.assertEqual(result['model_calls'], 0)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['snapshot']['ledger_commit'], HEAD)
        self.assertEqual(result['snapshot']['retained_success_text'],
                         {key: MESSAGE[key] for key in ('summary', 'research')})
        self.assertEqual([response.read_sizes for response in self.opener.responses], [[65537], [65537]])

    def test_cache_refreshes_at_exact_ttl_boundary_and_uses_new_pinned_sha(self):
        view = self.make_observer([ref(), successful_state(), ref(SECOND_HEAD), failure_after_success()])
        first = view.get()
        self.clock[0] = NOW + observer.TTL - 1
        self.assertEqual(view.get(), first)
        self.assertEqual(len(self.opener.requests), 2)
        self.clock[0] += 1
        fresh = view.get()
        self.assertEqual(len(self.opener.requests), 4)
        self.assertEqual(self.opener.requests[-1][0].full_url,
                         observer.RAW_PREFIX + SECOND_HEAD + observer.STATE_PATH)
        self.assertEqual(fresh['snapshot']['ledger_commit'], SECOND_HEAD)
        self.assertEqual(fresh['last_checked_at'], NOW + observer.TTL)
        self.assertEqual(fresh['refresh_after'], NOW + 2 * observer.TTL)

    def test_concurrent_refreshes_share_one_bounded_pair_of_requests(self):
        view = self.make_observer([ref(), successful_state()])
        start = threading.Barrier(8)

        def read(_):
            start.wait(timeout=5)
            return view.get()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(read, range(8)))
        self.assertEqual(len(self.opener.requests), 2)
        self.assertTrue(all(value == results[0] for value in results))

    def test_latest_provider_failure_stays_distinct_from_retained_success(self):
        state = failure_after_success('rate_limited')
        result = self.make_observer([ref(), state]).get()
        snapshot = result['snapshot']
        self.assertEqual(result['status'], 'observed')  # Read succeeded; no provider health claim.
        self.assertEqual(snapshot['last_attempt']['status'], 'rate_limited')
        self.assertEqual(snapshot['last_attempt']['run_id'], '101')
        self.assertEqual(snapshot['last_success_in_retained_journal']['run_id'], '100')
        self.assertEqual(snapshot['retained_success_text']['summary'], MESSAGE['summary'])
        self.assertEqual(snapshot['attempts'], 2)
        self.assertEqual(snapshot['successes'], 1)
        self.assertEqual(snapshot['next_due'], state['next_due'])

    def test_old_success_text_does_not_invent_a_success_in_truncated_journal(self):
        state = failure_after_success()
        state['journal'] = state['journal'][-1:]
        result = self.make_observer([ref(), state]).get()['snapshot']
        self.assertIsNone(result['last_success_in_retained_journal'])
        self.assertIsNotNone(result['retained_success_text'])
        self.assertEqual(result['last_attempt']['status'], 'request_failed')

    def test_failed_refresh_preserves_old_snapshot_as_stale_and_throttles_retries(self):
        view = self.make_observer([ref(), successful_state(), OSError('PRIVATE_ERROR_MARKER'),
                                   ref(SECOND_HEAD), failure_after_success()])
        original = view.get()['snapshot']
        self.clock[0] += observer.TTL
        failed = view.get()
        self.assertEqual(failed['status'], 'unavailable')
        self.assertTrue(failed['stale'])
        self.assertEqual(failed['snapshot'], original)
        self.assertEqual(failed['snapshot']['fetched_at'], NOW)
        self.assertNotIn('PRIVATE_ERROR_MARKER', json.dumps(failed))
        self.clock[0] += observer.TTL - 1
        self.assertEqual(view.get(), failed)
        self.assertEqual(len(self.opener.requests), 3)
        self.clock[0] += 1
        recovered = view.get()
        self.assertEqual(recovered['status'], 'observed')
        self.assertFalse(recovered['stale'])
        self.assertEqual(recovered['snapshot']['ledger_commit'], SECOND_HEAD)

    def test_first_failure_has_no_synthetic_success_or_empty_fresh_snapshot(self):
        result = self.make_observer([OSError('not reachable')]).get()
        self.assertEqual(result['status'], 'unavailable')
        self.assertTrue(result['stale'])
        self.assertIsNone(result['snapshot'])
        self.assertEqual(result['model_calls'], 0)

    def test_returned_nested_data_cannot_mutate_shared_cache(self):
        view = self.make_observer([ref(), successful_state()])
        first = view.get()
        original = copy.deepcopy(first)
        first['snapshot']['last_attempt']['status'] = 'request_failed'
        first['snapshot']['retained_success_text']['summary'] = 'Client mutation'
        self.assertEqual(view.get(), original)
        self.assertEqual(len(self.opener.requests), 2)

    def test_malformed_refs_fail_closed_before_any_raw_or_attacker_url_fetch(self):
        bad_refs = [None, [], {}, {'ref': 'refs/heads/main', 'object': {'sha': HEAD}},
                    {'ref': 'refs/heads/autonomy/continuous', 'object': []}, ref('main'),
                    ref('A' * 40), ref('../private'), ref('https://example.org/x'), ref(0)]
        for bad in bad_refs:
            with self.subTest(ref=bad):
                view = self.make_observer([bad])
                result = view.get()
                self.assertEqual(result['status'], 'unavailable')
                self.assertIsNone(result['snapshot'])
                self.assertEqual(len(self.opener.requests), 1)

    def test_malformed_state_does_not_replace_previous_valid_snapshot(self):
        for field, invalid in (('attempts', True), ('successes', 100), ('contract', 'changed'),
                               ('provider_blocked', 'false'), ('journal', [{'status': 'response_received'}])):
            with self.subTest(field=field):
                broken = successful_state()
                broken[field] = invalid
                view = self.make_observer([ref(), successful_state(), ref(SECOND_HEAD), broken])
                original = view.get()['snapshot']
                self.clock[0] += observer.TTL
                result = view.get()
                self.assertEqual(result['status'], 'unavailable')
                self.assertTrue(result['stale'])
                self.assertEqual(result['snapshot'], original)

    def test_transport_rejects_redirect_http_error_oversize_and_non_strict_json(self):
        cases = [Response(observer.REF_URL, {}, effective_url='https://example.org/redirect'),
                 Response(observer.REF_URL, {}, status=403), b'x' * 65537,
                 b'{"ref":0,"ref":1}', b'{"value":NaN}', b'"\xff"']
        for reply in cases:
            with self.subTest(reply_type=type(reply).__name__):
                view = self.make_observer([reply])
                result = view.get()
                self.assertEqual(result['status'], 'unavailable')
                self.assertIsNone(result['snapshot'])
                self.assertEqual(len(self.opener.requests), 1)

    def test_pending_and_provider_quarantine_are_observed_without_new_action(self):
        pending = continuous.reserve_state(continuous.initial_state(), NOW, '100', HEAD)
        with patch.object(qwen_space, 'call_qwen_space') as inference:
            result = self.make_observer([ref(), pending]).get()['snapshot']
            blocked = failure_after_success('access_denied')
            quarantined = self.make_observer([ref(), blocked]).get()['snapshot']
        inference.assert_not_called()
        self.assertEqual(result['pending']['run_id'], '100')
        self.assertIsNone(result['last_attempt'])
        self.assertTrue(quarantined['provider_blocked'])
        self.assertEqual(quarantined['last_attempt']['status'], 'access_denied')

    def test_service_environment_keeps_historical_build_separate_from_current_platform(self):
        service = Service.__new__(Service)
        historical = {'checked_at': '2026-09-24', 'platform': 'Linux',
                      'python': '3.12.14', 'packages': {'torch': False}}
        live = {'platform': 'Windows', 'python': '3.13.0', 'builtin_integrity': True}
        historical_before, live_before = copy.deepcopy(historical), copy.deepcopy(live)
        with patch.object(service, 'read_data', return_value=historical) as read, \
                patch.object(service, 'status', return_value={'environment': live}) as status:
            result = service.environment()
        read.assert_called_once_with('environment_status.json', {'status': 'not_configured'})
        status.assert_called_once_with()
        self.assertEqual(result['snapshot_kind'], 'historical_build_report')
        self.assertEqual(result['historical_snapshot'], historical_before)
        self.assertEqual(result['live_runtime'], live_before)
        self.assertNotIn('checked_at', result['live_runtime'])
        self.assertNotIn('packages', result['live_runtime'])
        self.assertNotIn('platform', result)
        self.assertEqual(historical, historical_before)
        self.assertEqual(live, live_before)

    def test_service_concurrent_cloud_calls_share_one_observer_and_network_cache(self):
        service = Service.__new__(Service)
        service.store = SimpleNamespace(lock=threading.RLock())
        shared = self.make_observer([ref(), successful_state()])
        start = threading.Barrier(8)

        def read(_):
            start.wait(timeout=5)
            return service.cloud_status()

        with patch.object(observer, 'CloudObserver', return_value=shared) as factory, \
                patch.object(qwen_space, 'call_qwen_space') as inference:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(read, range(8)))
        factory.assert_called_once_with()
        inference.assert_not_called()
        self.assertIs(service._cloud_observer, shared)
        self.assertEqual(len(self.opener.requests), 2)
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(results[0]['snapshot']['ledger_commit'], HEAD)


if __name__ == '__main__':
    unittest.main()

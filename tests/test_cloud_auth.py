"""Offline auth regressions; every OAuth response is a local fixture."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from workbench.cloud_auth import (
    AuthError, CloudAuth, Config, FLOW_COOKIE, FLOW_TTL, MAX_PENDING,
    MAX_RESPONSE_BYTES, MAX_SESSIONS, PROFILE_URL, SESSION_COOKIE,
    SESSION_TTL, TOKEN_URL, _NoRedirect, request_json,
)


class CloudAuthTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.config = Config('https://research.example.org', 'test-client',
                             'fixture-secret', '123456')
        self.calls = []
        self.token = {'access_token': 'fixture-access-token', 'token_type': 'bearer'}
        self.profile = {'id': '123456', 'client_id': 'test-client'}
        self.auth = CloudAuth(self.config, self.transport, lambda: self.now)

    def transport(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.token if url == TOKEN_URL else self.profile

    def flow(self):
        result = self.auth.begin()
        query = parse_qs(urlsplit(result['authorize_url']).query)
        return query, result['set_cookie'].split(';', 1)[0]

    def login(self):
        query, cookie = self.flow()
        result = self.auth.callback({'code': 'fixture-code', 'state': query['state'][0]}, cookie)
        return result, result['set_cookie'].split(';', 1)[0]

    def assert_error(self, code, function, *args):
        with self.assertRaises(AuthError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_env_requires_explicit_owner_and_never_enrolls_first_visitor(self):
        self.assert_error('auth_unconfigured', Config.from_env, {})
        env = dict(NEUROMORPH_PUBLIC_ORIGIN=self.config.public_origin,
                   NEUROMORPH_YANDEX_CLIENT_ID=self.config.client_id,
                   NEUROMORPH_YANDEX_CLIENT_SECRET='fixture-secret')
        self.assert_error('auth_unconfigured', Config.from_env, env)
        env['NEUROMORPH_YANDEX_OWNER_ID'] = '123456'
        self.assertEqual(Config.from_env(env), self.config)
        self.assertNotIn('fixture-secret', repr(self.config))
        self.assertNotIn('123456', repr(self.config))

    def test_reject_ambiguous_or_insecure_origins(self):
        for origin in ('http://research.example.org', 'https://research.example.org/',
                       'https://user@research.example.org', 'https://research.example.org?x=1',
                       'https://research.example.org#x', 'https://bad..example.org',
                       'https://-bad.example.org', 'https://research.example.org:0',
                       'https://research.example.org:65536', 'https://research.example.org\r\nx:x'):
            with self.subTest(origin=origin):
                self.assert_error('auth_config_invalid', Config, origin, 'client', 'secret', '123')

    def test_pkce_binding_and_only_fixed_provider_endpoints(self):
        query, cookie = self.flow()
        self.assertEqual(query['scope'], ['login:info'])
        self.assertEqual(query['redirect_uri'], [self.config.public_origin + '/auth/yandex/callback'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        result = self.auth.callback({'code': 'fixture-code', 'state': query['state'][0]}, cookie)
        self.assertEqual([(x[0], x[1]) for x in self.calls], [('POST', TOKEN_URL), ('GET', PROFILE_URL)])
        body = parse_qs(self.calls[0][3].decode('ascii'))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'][0].encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        self.assertEqual(query['code_challenge'], [challenge])
        self.assertEqual(self.calls[1][2]['Authorization'], 'OAuth fixture-access-token')
        self.assertNotIn('fixture-access-token', json.dumps(result))
        for attribute in ('Secure', 'HttpOnly', 'SameSite=Lax', 'Path=/'):
            self.assertIn(attribute, result['set_cookie'])
        self.assertNotIn('Domain=', result['set_cookie'])
        self.assertIn('Max-Age=0', result['clear_oauth_cookie'])

    def test_cookie_bound_state_rejects_cross_browser_callback_without_consuming(self):
        query, cookie = self.flow()
        other_query, other_cookie = self.flow()
        payload = {'code': 'code', 'state': query['state'][0]}
        self.assert_error('invalid_state', self.auth.callback, payload, other_cookie)
        self.assertEqual(self.calls, [])
        self.auth.callback(payload, cookie)
        self.assert_error('invalid_state', self.auth.callback, payload, cookie)

    def test_expired_flow_never_contacts_provider(self):
        query, cookie = self.flow()
        self.now += FLOW_TTL
        self.assert_error('invalid_state', self.auth.callback,
                          {'code': 'code', 'state': query['state'][0]}, cookie)
        self.assertEqual(self.calls, [])

    def test_duplicate_flow_cookie_rejected(self):
        query, cookie = self.flow()
        self.assert_error('invalid_state', self.auth.callback,
                          {'code': 'code', 'state': query['state'][0]}, cookie + '; ' + cookie)
        self.assertEqual(self.calls, [])

    def test_denied_login_consumes_flow_without_provider_call(self):
        query, cookie = self.flow()
        payload = {'error': 'access_denied', 'state': query['state'][0]}
        self.assert_error('login_denied', self.auth.callback, payload, cookie)
        self.assert_error('invalid_state', self.auth.callback, payload, cookie)
        self.assertEqual(self.calls, [])

    def test_upstream_failure_sanitized_and_not_replayed(self):
        query, cookie = self.flow()
        payload = {'code': 'code', 'state': query['state'][0]}
        def failed(*args):
            raise RuntimeError('secret-upstream-response')
        self.auth._transport = failed
        error = self.assert_error('oauth_unavailable', self.auth.callback, payload, cookie)
        self.assertNotIn('secret-upstream-response', str(error))
        self.assert_error('invalid_state', self.auth.callback, payload, cookie)

    def test_owner_and_oauth_client_must_both_match(self):
        for profile, error in (({'id': '999', 'client_id': 'test-client'}, 'owner_required'),
                               ({'id': '123456', 'client_id': 'foreign-client'}, 'oauth_unavailable'),
                               ({'id': 123456, 'client_id': 'test-client'}, 'oauth_unavailable'),
                               ({'id': '123456'}, 'oauth_unavailable')):
            with self.subTest(profile=profile):
                self.profile = profile
                self.assert_error(error, self.login)
                self.assertEqual(self.auth._sessions, {})

    def test_malformed_token_does_not_reach_profile(self):
        for token in ({'access_token': 'x\r\ny', 'token_type': 'bearer'},
                      {'access_token': 'x', 'token_type': None},
                      {'access_token': 'x', 'token_type': 'basic'},
                      {'access_token': 'x', 'token_type': 'bearer', 'error': 'bad'}, []):
            with self.subTest(token=token):
                self.token = token
                self.calls.clear()
                self.assert_error('oauth_unavailable', self.login)
                self.assertEqual(len(self.calls), 1)

    def test_callback_rejects_ambiguous_and_unbounded_input(self):
        query, cookie = self.flow()
        state = query['state'][0]
        for payload in ({'code': 'x', 'error': 'x', 'state': state}, {'state': state},
                        {'code': '', 'state': state}, {'code': 'x' * 4097, 'state': state},
                        {'code': 'x', 'state': state, 'redirect_uri': 'https://evil.example'},
                        {'code': ['x', 'y'], 'state': state}, {'code': 'x', 'state': '\n'}):
            with self.subTest(payload=payload):
                self.assert_error('invalid_request', self.auth.callback, payload, cookie)
        self.assertEqual(self.calls, [])

    def test_replay_race_allows_exactly_one_exchange(self):
        query, cookie = self.flow()
        payload = {'code': 'code', 'state': query['state'][0]}
        def submit(_):
            try:
                self.auth.callback(payload, cookie)
                return 'accepted'
            except AuthError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(submit, range(8)))
        self.assertEqual(outcomes.count('accepted'), 1)
        self.assertEqual(outcomes.count('invalid_state'), 7)
        self.assertEqual(len(self.calls), 2)

    def test_session_expiry_restart_and_return_value_isolation(self):
        result, cookie = self.login()
        view = self.auth.session(cookie)
        view['subject'] = '999'
        self.assertEqual(self.auth.session(cookie)['subject'], '123456')
        restarted = CloudAuth(self.config, self.transport, lambda: self.now)
        self.assert_error('unauthorized', restarted.session, cookie)
        self.now += SESSION_TTL
        self.assert_error('unauthorized', self.auth.session, cookie)

    def test_session_cookie_ambiguity_and_control_characters_rejected(self):
        result, cookie = self.login()
        for invalid in ('', cookie + '; ' + cookie, cookie + '\r\nHeader:x',
                        SESSION_COOKIE + '=wrong', 'x' * 8193):
            with self.subTest(cookie=invalid):
                self.assert_error('unauthorized', self.auth.session, invalid)

    def test_writes_require_exact_origin_and_session_specific_csrf(self):
        result, cookie = self.login()
        other, other_cookie = self.login()
        for origin, csrf in (('', result['csrf_token']), ('https://evil.example', result['csrf_token']),
                             (self.config.public_origin + '/', result['csrf_token']),
                             (self.config.public_origin, ''),
                             (self.config.public_origin, other['csrf_token'])):
            self.assert_error('csrf_rejected', self.auth.authorize_write, cookie, origin, csrf)
        self.assertEqual(self.auth.authorize_write(cookie, self.config.public_origin,
                                                 result['csrf_token'])['subject'], '123456')

    def test_logout_requires_csrf_and_revokes_only_current_session(self):
        result, cookie = self.login()
        other, other_cookie = self.login()
        self.assert_error('csrf_rejected', self.auth.logout, cookie, self.config.public_origin, '')
        self.assertEqual(self.auth.session(cookie)['subject'], '123456')
        logged_out = self.auth.logout(cookie, self.config.public_origin, result['csrf_token'])
        self.assertIn('Max-Age=0', logged_out['set_cookie'])
        self.assert_error('unauthorized', self.auth.session, cookie)
        self.assertEqual(self.auth.session(other_cookie)['subject'], '123456')

    def test_pending_capacity_and_expiry_recovery(self):
        for _ in range(MAX_PENDING):
            self.auth.begin()
        self.assert_error('auth_capacity', self.auth.begin)
        self.now += FLOW_TTL
        self.auth.begin()
        self.assertEqual(len(self.auth._pending), 1)

    def test_session_capacity_does_not_evict_existing_owner_session(self):
        first, first_cookie = self.login()
        for _ in range(MAX_SESSIONS - 1):
            self.login()
        self.assert_error('auth_capacity', self.login)
        self.assertEqual(self.auth.session(first_cookie)['subject'], '123456')

    def test_invalid_clock_fails_closed(self):
        for invalid in (float('nan'), float('inf'), -1, True, '1000'):
            self.auth._clock = lambda: invalid
            self.assert_error('clock_unavailable', self.auth.begin)


class OAuthTransportTests(unittest.TestCase):
    class Response:
        def __init__(self, raw=b'{}', status=200, url=TOKEN_URL, content_type='application/json'):
            self.raw, self.status, self.url = raw, status, url
            self.headers = {'Content-Type': content_type}
            self.read_limit = None
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def geturl(self):
            return self.url
        def read(self, limit):
            self.read_limit = limit
            return self.raw[:limit]

    def test_transport_fixed_urls_and_no_network_on_bad_target(self):
        with patch('workbench.cloud_auth.build_opener') as factory:
            for method, url, body in (('POST', 'https://evil.example', b'x'),
                                      ('GET', TOKEN_URL, None), ('POST', TOKEN_URL, b'x' * 16385)):
                with self.assertRaises(AuthError):
                    request_json(method, url, {}, body)
            factory.assert_not_called()

    def test_transport_disables_proxy_and_redirect_and_bounds_read(self):
        response = self.Response(b'{"ok":true}')
        with patch('workbench.cloud_auth.build_opener') as factory:
            factory.return_value.open.return_value = response
            self.assertEqual(request_json('POST', TOKEN_URL, {}, b'x'), {'ok': True})
            handlers = factory.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], _NoRedirect)
            self.assertEqual(factory.return_value.open.call_args.kwargs['timeout'], 10)
            self.assertEqual(response.read_limit, MAX_RESPONSE_BYTES + 1)
            with self.assertRaises(AuthError):
                handlers[1].redirect_request(None, None, 302, '', {}, 'https://evil.example')

    def test_transport_rejects_redirect_html_oversize_duplicate_and_nonfinite_json(self):
        responses = (self.Response(status=302), self.Response(url='https://evil.example'),
                     self.Response(content_type='text/html'), self.Response(b'x' * (MAX_RESPONSE_BYTES + 1)),
                     self.Response(b'{"a":1,"a":2}'), self.Response(b'{"a":NaN}'),
                     self.Response(b'[]'), self.Response(b'\xff'))
        for response in responses:
            with self.subTest(response=response.raw[:50]):
                with patch('workbench.cloud_auth.build_opener') as factory:
                    factory.return_value.open.return_value = response
                    with self.assertRaises(AuthError) as caught:
                        request_json('POST', TOKEN_URL, {}, b'x')
                    self.assertEqual(str(caught.exception), 'oauth_unavailable')


if __name__ == '__main__':
    unittest.main()

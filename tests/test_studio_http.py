"""Cloud boundary and local workspace behavior through real HTTP sockets."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from workbench.server import make_server
from workbench.service import Service
from workbench.cloud_auth import AuthError


class FixtureAuth:
    config = SimpleNamespace(public_origin='https://research.example')
    def session(self, cookie):
        if cookie != 'fixture=owner':
            raise AuthError('unauthorized')
        return {'subject':'owner','csrf_token':'csrf-fixture','expires_at':1234}
    def authorize_write(self, cookie, origin, token):
        session = self.session(cookie)
        if origin != self.config.public_origin or token != session['csrf_token']:
            raise AuthError('csrf_rejected')
        return session


class StudioHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(db_path=Path(self.tmp.name)/'db.sqlite')
        self.server = make_server(self.service, port=0, cloud_auth=FixtureAuth())
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.close()
        self.tmp.cleanup()

    def request(self, path='/api/studio', data=None, cookie='', csrf='', host='research.example'):
        connection = http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=3)
        headers = {'Host':host, 'Cookie':cookie}
        body = None
        if data is not None:
            headers.update({'Content-Type':'application/json','Origin':'https://research.example','X-CSRF-Token':csrf})
            body = json.dumps(data)
        connection.request('POST' if body else 'GET',path,body,headers)
        response = connection.getresponse()
        content = response.read()
        result = response.status, content, dict(response.getheaders())
        connection.close()
        return result

    def test_private_routes_require_session_including_exports(self):
        for path in ['/api/studio','/api/studio/export','/api/sources','/api/export','/api/session','/api/science/catalog']:
            self.assertEqual(self.request(path)[0],401,path)

    def test_write_requires_cookie_origin_and_csrf(self):
        body = {'kind':'project','title':'Cloud research','idempotency_key':'http-key'}
        self.assertEqual(self.request('/api/studio/save',body,cookie='fixture=owner')[0],403)
        self.assertEqual(self.service.studio.snapshot('owner')['projects'],[])
        saved = self.request('/api/studio/save',body,cookie='fixture=owner',csrf='csrf-fixture')
        self.assertEqual(saved[0],200)
        item = json.loads(saved[1])
        self.assertEqual(self.service.studio.snapshot('owner')['projects'][0]['id'],item['id'])
        self.assertEqual(self.service.studio.snapshot('local')['projects'],[])

    def test_host_header_is_not_proxy_trusted(self):
        self.assertEqual(self.request(cookie='fixture=owner',host='attacker.example')[0],403)

    def test_cloud_shell_has_no_private_state(self):
        status,body,headers = self.request('/studio.html')
        self.assertEqual(status,200)
        self.assertNotIn(b'csrf-fixture',body)
        self.assertEqual(headers.get('Cache-Control'),'no-store')

    def test_callback_duplicate_blank_parameters_rejected(self):
        self.assertEqual(self.request('/auth/yandex/callback?code=x&code=&state=y')[0],400)

    def test_parameter_flood_rejected(self):
        self.assertEqual(self.request('/api/studio?'+'&'.join('k'+str(i)+'=x' for i in range(40)))[0],400)

    def test_science_protocol_requires_session_and_csrf_before_validation(self):
        self.assertEqual(self.request('/api/science/protocol',{'execute':True})[0],401)
        self.assertEqual(self.request('/api/science/protocol',{'execute':True},cookie='fixture=owner')[0],403)
        self.assertEqual(self.request('/api/science/protocol',{'execute':True},cookie='fixture=owner',csrf='csrf-fixture')[0],400)
        self.assertEqual(self.service.studio.snapshot('owner')['tasks'],[])


if __name__ == '__main__':
    unittest.main()

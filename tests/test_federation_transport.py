import http.client
import json
import tempfile
import threading
import unittest
import shutil
import socket
import ssl
import subprocess
from pathlib import Path
from workbench.federation.exchange import Exchange
from workbench.federation.transport import make_gateway
from workbench.service import Service, ServiceError
from workbench.server import make_server

ROOT=Path(__file__).resolve().parents[1]

class FederationTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=Service(ROOT,Path(self.tmp.name)/'workbench.sqlite3')
        self.exchange=self.service.federation.exchange
        self.server=make_gateway(self.exchange,port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        self.service.close();self.tmp.cleanup()
    def request(self,path,method='GET',body=None,headers=None):
        c=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        h=dict(headers or {})
        if body is not None:h['Content-Type']='application/json';body=json.dumps(body)
        c.request(method,path,body,h);r=c.getresponse();data=json.loads(r.read());c.close();return r.status,data
    def test_public_profile_and_no_remote_admin(self):
        status,data=self.request('/.well-known/meta-harness.json')
        self.assertEqual(status,200);self.assertFalse(data['a2a_compliant'])
        self.assertEqual(self.request('/v1/offers')[0],200)
        self.assertEqual(self.request('/v1/review','POST',{})[0],404)
        self.assertEqual(self.request('/api/federation/reviews')[0],404)
        self.assertEqual(self.request('/v1/member')[0],403)
    def test_host_origin_body_and_member_boundaries(self):
        self.assertEqual(self.request('/v1/offers',headers={'Host':'attacker.test'})[0],403)
        self.assertEqual(self.request('/v1/offers',headers={'Origin':'https://attacker.test'})[0],403)
        self.assertEqual(self.request('/v1/join','POST',[])[0],400)
        self.assertEqual(self.request('/v1/claim','POST',{},headers={'Authorization':'Bearer wrong'})[0],403)
        self.assertEqual(self.request('/v1/join','POST',{'pad':'x'*140000})[0],413)
    def test_remote_requires_tls_and_origin(self):
        with self.assertRaises(ValueError):make_gateway(self.exchange,host='0.0.0.0',port=0)
        with self.assertRaises(ValueError):make_gateway(self.exchange,host='0.0.0.0',port=0,public_base_url='http://example.org')
    @unittest.skipUnless(shutil.which('openssl'),'openssl unavailable for ephemeral TLS fixture')
    def test_silent_tls_peer_does_not_block_other_connections(self):
        cert=Path(self.tmp.name)/'cert.pem';key=Path(self.tmp.name)/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=10)
        server=make_gateway(self.exchange,port=0,certfile=str(cert),keyfile=str(key))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        silent=socket.create_connection(server.server_address,timeout=2)
        try:
            ctx=ssl.create_default_context(cafile=str(cert))
            c=http.client.HTTPSConnection(*server.server_address,timeout=2,context=ctx)
            c.request('GET','/v1/offers');r=c.getresponse();self.assertEqual(r.status,200);r.read();c.close()
        finally:
            silent.close();server.shutdown();server.server_close();thread.join(timeout=2)
    def test_local_control_offline_discovery_proposal_and_no_join(self):
        result=self.service.federation_call('discover',{'query':'research','providers':['agentverse'],'limit':2,'online':False,'data_class':'public'})
        self.assertEqual(result['requests'],0)
        self.assertTrue(result['items'])
        offer=self.exchange.create_offer({'title':'Research evidence','description':'Review reproducibility of public single-cell data analysis.',
          'task_type':'literature_review','requirements':['Primary sources'], 'reward_credits':2,'max_assignments':1,'data_class':'public'})
        proposal=self.service.federation_call('proposal',{'candidate_id':result['items'][0]['id'],'offer_id':offer['offer_id']})
        self.assertEqual(proposal['delivery_status'],'draft_not_sent')
        self.assertIn('Карточка',proposal['warning'])
        self.assertNotIn('invite_token',json.dumps(self.service.federation_call('export')))
        with self.assertRaises(ServiceError):self.service.federation_call('discover',{'query':'x','online':True,'data_class':'sensitive_genomic'})
    def test_local_api_and_frontend_delivery(self):
        ui=make_server(self.service,port=0);t=threading.Thread(target=ui.serve_forever,daemon=True);t.start()
        try:
            c=http.client.HTTPConnection(*ui.server_address,timeout=3)
            c.request('GET','/api/federation');r=c.getresponse();self.assertEqual(r.status,200);self.assertIn('exchange',json.loads(r.read()))
            c.close()
            c=http.client.HTTPConnection(*ui.server_address,timeout=3)
            c.request('GET','/federation.html');r=c.getresponse();self.assertEqual(r.status,200);self.assertIn(b'federation.js',r.read());c.close()
        finally:ui.shutdown();ui.server_close();t.join(timeout=2)

    def test_mcp_discovery_is_offline_and_draft_does_not_enroll(self):
        import io
        from workbench.mcp_server import serve_stdio
        candidate=self.service.federation_call('discover',{'query':'research','providers':['agentverse'],'online':False,'limit':1})['items'][0]
        offer=self.exchange.create_offer({'title':'Review sources','description':'Check public documentation.',
          'task_type':'literature_review','requirements':['Source attribution'],'reward_credits':1,'max_assignments':1,'data_class':'public'})
        frames=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},
                {'jsonrpc':'2.0','method':'notifications/initialized'},
                {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'discover_agent_candidates','arguments':{'query':'research','providers':['agentverse'],'limit':1}}},
                {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'draft_contribution_proposal','arguments':{'candidate_id':candidate['id'],'offer_id':offer['offer_id']}}},
                {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'federation_status','arguments':{}}}]
        output=io.StringIO()
        serve_stdio(self.service,io.StringIO('\n'.join(json.dumps(x) for x in frames)+'\n'),output)
        responses=[json.loads(x) for x in output.getvalue().splitlines()]
        self.assertTrue(all(not r.get('result',{}).get('isError',False) and 'error' not in r for r in responses))
        payloads=[json.loads(r['result']['content'][0]['text']) for r in responses[1:]]
        self.assertEqual(payloads[0]['requests'],0)
        self.assertEqual(payloads[1]['delivery_status'],'draft_not_sent')
        self.assertNotIn('member_token',json.dumps(payloads))
        self.assertEqual(self.exchange.export()['members'],[])

if __name__=='__main__':unittest.main()

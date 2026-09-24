import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from workbench.service import Service
from workbench.server import make_server
from workbench.mcp_server import serve_stdio
from workbench.network.discovery import select_builtin

ROOT=Path(__file__).resolve().parents[1]


class BrainInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=Service(ROOT,Path(self.tmp.name)/'workbench.sqlite3')
        self.server=make_server(self.service,port=0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        self.service.close();self.tmp.cleanup()

    def request(self,path,body=None):
        c=http.client.HTTPConnection(*self.server.server_address,timeout=10)
        c.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),
                  {} if body is None else {'Content-Type':'application/json'})
        r=c.getresponse();data=json.loads(r.read());c.close();return r.status,data

    def test_http_start_read_cancel_and_private_question_not_in_job(self):
        code,status=self.request('/api/brain')
        self.assertEqual(code,200);self.assertEqual(status['counts']['llm_agents_configured'],0)
        code,session=self.request('/api/brain/start',{'question':'Локальный внутренний вопрос KAN','data_class':'internal','plugins':['kan_benchmark']})
        self.assertEqual(code,200)
        self.assertEqual(session['job_details'][0]['payload']['plugin_id'],'kan_benchmark')
        self.assertNotIn('вопрос',json.dumps(session['job_details'][0]['payload'],ensure_ascii=False))
        self.assertEqual(self.request('/api/brain/session?id='+session['id'])[1]['id'],session['id'])
        code,cancelled=self.request('/api/brain/cancel',{'id':session['id']})
        self.assertEqual(code,200);self.assertEqual(cancelled['status'],'cancelled')
        self.assertEqual(cancelled['job_details'][0]['status'],'cancelled')

    def test_model_http_execution_and_enum_rejection(self):
        code,_=self.request('/api/run',{'plugin_id':'kan_benchmark','parameters':{'basis':'unapproved'}})
        self.assertEqual(code,400)
        # A valid JSON integer may exceed the range of a C double. Reject it
        # as a parameter error without overflowing math.isfinite at any entry.
        from workbench import kan, cortical
        for module in [kan, cortical]:
            with self.assertRaises(ValueError):
                module.validate({'seed':10**1000})
        for plugin in ['kan_benchmark','cortical_sequence']:
            code,error=self.request('/api/run',{'plugin_id':plugin,'parameters':{'seed':10**1000}})
            self.assertEqual(code,400)
        for plugin in ['kan_benchmark','cortical_sequence']:
            code,run=self.request('/api/run',{'plugin_id':plugin,'parameters':{'replicates':1}})
            self.assertEqual(code,200);self.assertEqual(run['status'],'completed')
            self.assertTrue(run['result']['table'])
            self.assertIn('workbench/'+('kan' if plugin=='kan_benchmark' else 'cortical')+'.py',run['provenance']['builtin_hashes'])

    def test_mcp_routes_to_same_durable_sessions(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'start_brain_session','arguments':{'question':'Проверить контекст последовательности','plugins':['cortical_sequence']}}},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'brain_status','arguments':{}}}]
        output=io.StringIO();serve_stdio(self.service,io.StringIO('\n'.join(json.dumps(m) for m in messages)+'\n'),output)
        replies=[json.loads(x) for x in output.getvalue().splitlines()]
        self.assertFalse(replies[1]['result']['isError'])
        session=json.loads(replies[1]['result']['content'][0]['text'])
        status=json.loads(replies[2]['result']['content'][0]['text'])
        self.assertIn(session['id'],[s['id'] for s in status['sessions']])
        self.assertEqual(self.service.brain.get(session['id'])['jobs'],session['jobs'])

    def test_new_topic_routes_and_bounded_session_inputs(self):
        self.assertEqual(select_builtin('Сети Колмогорова–Арнольда'),'kan_benchmark')
        self.assertEqual(select_builtin('кортикоморфные сети'),'cortical_sequence')
        self.assertEqual(select_builtin('quantum'),'quantum_circuit')
        for body in [[],{'question':'x','plugins':['arbitrary_code']},{'question':'x','seed':True},{'question':'x','data_class':'sensitive_genomic'}]:
            self.assertEqual(self.request('/api/brain/start',body)[0],400)


if __name__=='__main__':unittest.main()

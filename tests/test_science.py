import json
from pathlib import Path
import tempfile
import unittest
from workbench.science import catalogue, protocol_task
from workbench.service import Service, ServiceError


class ScienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(db_path=Path(self.tmp.name)/'db.sqlite')
        self.project = self.service.studio.save({'kind':'project','title':'Study','idempotency_key':'project'})
        self.cat = {'items':[{'id':'example','name':'Example','url':'https://example.org/'}]}
        self.data = dict(project_id=self.project['id'],idempotency_key='protocol-one',
                         template_id='literature-review',platform_id='example',title='Study',
                         question='Does the evidence support H?',hypothesis='H',method='Read original sources',
                         control='Look for a contradictory primary source',criteria='Every claim has evidence and limitations')

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def test_shipped_catalogue_has_unique_ids_sources_and_no_execution_flag(self):
        cat = catalogue()
        self.assertGreater(len(cat['items']),10)
        self.assertEqual(len(cat['catalog_digest']),64)
        self.assertEqual(len(cat['templates']),3)
        for row in cat['items']:
            self.assertNotEqual(row['status'],'scientifically_validated')
            self.assertNotIn('credentials',row)

    def test_protocol_is_deterministic_planned_and_inert(self):
        self.data['method'] = 'DO NOT EXECUTE: __import__("os").system("unexpected-command")'
        first = protocol_task(self.data,self.cat)
        self.assertEqual(first,protocol_task(self.data,self.cat))
        self.assertEqual(first['status'],'planned')
        self.assertIn('unexpected-command',first['body'])
        self.assertIn('Контроль / контрпример:',first['body'])
        self.assertEqual(self.service.studio.snapshot()['tasks'],[])

    def test_retries_create_only_one_persistent_task(self):
        task = protocol_task(self.data,self.cat)
        a = self.service.studio.save(task)
        b = self.service.studio.save(task)
        self.assertEqual(a,b)
        self.assertEqual(len(self.service.studio.snapshot()['tasks']),1)
        self.data['control']='Changed control'
        with self.assertRaises(ServiceError) as caught:
            self.service.studio.save(protocol_task(self.data,self.cat))
        self.assertEqual(caught.exception.code,'idempotency_conflict')

    def test_incomplete_protocol_never_saves(self):
        for field in self.data:
            for value in ('',None,False):
                with self.subTest(field=field,value=value),self.assertRaises(ServiceError):
                    protocol_task({**self.data,field:value},self.cat)
        self.assertEqual(self.service.studio.snapshot()['tasks'],[])

    def test_unknown_fields_and_ids_rejected(self):
        for change in ({'execute':True},{'platform_id':'unknown'},{'template_id':'unknown'},
                       {'project_id':'../other'},{'method':'x'*2001},{'criteria':'x'*4001}):
            with self.subTest(change=change),self.assertRaises(ServiceError):
                protocol_task({**self.data,**change},self.cat)

    def test_protocol_respects_owner_scope(self):
        task = protocol_task(self.data,self.cat)
        with self.assertRaises(ServiceError):
            self.service.studio.save(task,'another-owner')
        self.assertEqual(self.service.studio.snapshot('another-owner')['tasks'],[])

    def test_corrupt_catalogue_fails_closed_without_remote_fetch(self):
        path=Path(self.tmp.name)/'catalog.json'
        for raw in ('{}','[]','not json','{"schema_version":1,"items":[]}', '['*3000+']'*3000):
            path.write_text(raw)
            with self.assertRaises(ServiceError) as caught:
                catalogue(path)
            self.assertEqual(caught.exception.code,'catalog_unavailable')

    def test_catalogue_rejects_duplicate_ids_and_unsafe_links(self):
        path=Path(self.tmp.name)/'catalog.json'
        original=catalogue()
        original.pop('templates');original.pop('catalog_digest');original.pop('notice')
        for url in ('javascript:alert(1)','http://example.org','https://user:pass@example.org','https://example.org/#token',123,{},[]):
            changed=json.loads(json.dumps(original));changed['items'][0]['url']=url
            path.write_text(json.dumps(changed))
            with self.assertRaises(ServiceError):catalogue(path)
        original['items'].append(original['items'][0]);path.write_text(json.dumps(original))
        with self.assertRaises(ServiceError):catalogue(path)

    def test_catalog_metadata_changes_do_not_change_request_identity(self):
        original=protocol_task(self.data,self.cat)
        self.cat['items'][0]['name']='Renamed'
        self.cat['items'][0]['url']='https://example.org/new'
        self.assertEqual(original,protocol_task(self.data,self.cat))
        self.cat['items'].append({**self.cat['items'][0],'id':'another-platform'})
        changed=protocol_task({**self.data,'platform_id':'another-platform'},self.cat)
        self.assertNotEqual(original['body'],changed['body'])


if __name__=='__main__':unittest.main()

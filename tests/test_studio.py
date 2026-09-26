import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from workbench.store import Store
from workbench.studio import StudioWorkspace
from workbench.service import ServiceError


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'workspace.db'
        self.store = Store(self.path)
        self.workspace = StudioWorkspace(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def project(self, key='project-key', owner='local'):
        return self.workspace.save({'kind':'project','title':'Research','body':'Hypothesis','idempotency_key':key}, owner)

    def test_create_is_durable_and_idempotent(self):
        first = self.project()
        self.assertEqual(first, self.project())
        self.assertEqual(len(self.workspace.snapshot()['events']),1)
        second_store = Store(self.path)
        try:
            self.assertEqual(StudioWorkspace(second_store).snapshot()['projects'],[first])
        finally:
            second_store.close()

    def test_idempotency_collision_rejected(self):
        self.project()
        with self.assertRaises(ServiceError) as ctx:
            self.workspace.save({'kind':'project','title':'Different','idempotency_key':'project-key'})
        self.assertEqual(ctx.exception.status,409)
        self.assertEqual(len(self.workspace.snapshot()['projects']),1)

    def test_concurrent_create_yields_one_record(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            rows = list(pool.map(lambda _: self.project(),range(12)))
        self.assertEqual(len({r['id'] for r in rows}),1)
        self.assertEqual(len(self.workspace.snapshot()['events']),1)

    def test_concurrent_edit_rejects_stale_version(self):
        item = self.project()
        def update(title):
            try:
                self.workspace.save({'kind':'project','id':item['id'],'version':1,'title':title})
                return 'saved'
            except ServiceError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update,['First','Second']))
        self.assertCountEqual(outcomes,['saved','version_conflict'])
        self.assertEqual(self.workspace.snapshot()['projects'][0]['version'],2)

    def test_owner_isolation_and_cross_project_write(self):
        project = self.project(owner='alice')
        self.assertEqual(self.workspace.snapshot('bob')['projects'],[])
        with self.assertRaises(ServiceError) as ctx:
            self.workspace.save({'kind':'task','project_id':project['id'],'title':'Leaked','idempotency_key':'x'},'bob')
        self.assertEqual(ctx.exception.status,404)
        with self.assertRaises(ServiceError):
            self.workspace.save({'kind':'project','id':project['id'],'version':1,'title':'Changed'},'bob')
        self.assertEqual(self.workspace.snapshot('alice')['projects'][0]['title'],'Research')

    def test_task_states_are_records_not_execution(self):
        p = self.project()
        task = self.workspace.save({'kind':'task','title':'Test hypothesis','project_id':p['id'],
            'question':'Does it reproduce?','criteria':'Bounded experiment','idempotency_key':'t'})
        self.assertEqual(task['status'],'planned')
        self.assertFalse(self.workspace.snapshot()['capabilities']['model_calls'])
        self.assertEqual(self.workspace.snapshot()['events'][0]['actor'],'operator')

    def test_reject_secrets_in_url_and_unknown_fields(self):
        p = self.project()
        for url in ['javascript:alert(1)','http://example.org','https://user:pass@example.org','https://example.org:90']:
            with self.assertRaises(ServiceError):
                self.workspace.save({'kind':'artifact','title':'A','project_id':p['id'],'source_url':url,'idempotency_key':'a'})
        with self.assertRaises(ServiceError):
            self.workspace.save({'kind':'project','title':'A','owner':'other','idempotency_key':'x'})
        self.assertEqual(self.workspace.snapshot()['artifacts'],[])

    def test_invalid_request_does_not_poison_transaction(self):
        self.project()
        with self.assertRaises(ServiceError):
            self.workspace.save({'kind':'task','title':'A','project_id':'missing','idempotency_key':'x'})
        self.project('second')
        self.assertEqual(len(self.workspace.snapshot()['projects']),2)


if __name__ == '__main__':
    unittest.main()

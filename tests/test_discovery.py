import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from workbench.network import discovery as d

ROOT = Path(__file__).resolve().parents[1]


class Response(io.BytesIO):
    def __init__(self, content, url, content_type="application/json", length=None):
        super().__init__(content)
        self.url = url
        self.headers = Message()
        self.headers['Content-Type'] = content_type
        if length is not None:
            self.headers['Content-Length'] = str(length)

    def geturl(self):
        return self.url


class DiscoveryTests(unittest.TestCase):
    def test_catalog_normalizes_old_sources_and_adapter_truth(self):
        resources = d.catalog(ROOT, [{"id": "fake", "title": "Injected", "adapter": {"available": True, "plugin_id": "arbitrary"}}])
        self.assertGreater(len(resources), 45)
        self.assertTrue(all(all(k in r for k in ['id','title','url','description','capabilities','kind','access','status','provenance','limitations']) for r in resources))
        self.assertEqual(sum(r['adapter']['available'] for r in resources), len(d.BUILTINS))
        self.assertFalse(next(r for r in resources if r['id'] == 'fake')['adapter']['available'])

    def test_offline_ranking_has_no_network_and_no_false_match(self):
        with patch.object(d, '_fetch_json', side_effect=AssertionError('No network')):
            result = d.discover('эпигенетика метилирование', offline=True, root=ROOT)
            self.assertEqual(result['requests'], 0)
            self.assertTrue(result['items'])
            self.assertTrue(all(r['score'] > 0 for r in result['items']))
            self.assertEqual(d.discover('zzzzunfindable', offline=True, root=ROOT)['items'], [])

    def test_ranking_relevance_not_installation(self):
        result = d.rank_resources('эпигенетика single-cell scvi', d.catalog(ROOT))
        self.assertEqual(result[0]['id'], 'platform-scvi')
        self.assertFalse(result[0]['computability']['adapter_available'])
        self.assertIn('epigenomics', result[0]['match_explanation']['capabilities'])

    def test_public_metadata_deduplication_and_limit(self):
        fixtures = {
            'europepmc': {'resultList': {'result': [{'id':'123', 'source':'MED', 'title':'Epigenetics A', 'doi':'10.123/a'}, {'title': 'No DOI', 'id': '456', 'source': 'MED'}]}},
            'crossref': {'status':'ok', 'message': {'items': [{'title':['Duplicate A'], 'DOI':'10.123/A', 'URL':'https://example.org/a'}, {'title':['New B'], 'DOI':'10.123/b'}]}}
        }
        with patch.object(d, '_fetch_json', side_effect=lambda p,q,n: fixtures[p]):
            output = d.discover('epigenetics', limit=3)
        self.assertEqual(len(output['items']), 3)
        self.assertEqual(output['requests'], 2)
        self.assertTrue(all(x['status'] == 'metadata_unverified' for x in output['items']))
        self.assertTrue(all(not x['adapter']['available'] for x in output['items']))

    def test_provider_shape_and_item_rejection(self):
        for fixture in [[], {}, {'message': {'items': []}}, {'status': 'ok', 'message': {'items': {}}}]:
            with self.subTest(fixture=fixture), patch.object(d, '_fetch_json', return_value=fixture):
                result = d.discover('model', providers=['crossref'])
                self.assertEqual(result['errors'][0]['code'], 'invalid_response')
                self.assertEqual(result['items'], [])
        self.assertEqual(d._parse('europepmc', {'resultList':{'result':[None, {}, {'title':42}]}}, 3), [])

    def test_errors_do_not_leak_response_content_or_network_details(self):
        for error, code in [(HTTPError('url',429,'private-detail',{},None),'http_error'), (TimeoutError('secret'),'timeout'), (URLError('credential=x'),'network_error')]:
            with self.subTest(code=code), patch.object(d, '_fetch_json', side_effect=error):
                result = d.discover('model', providers=['crossref'])
                self.assertEqual(result['errors'][0]['code'], code)
                self.assertNotIn('secret', json.dumps(result))
                self.assertNotIn('credential=x', json.dumps(result))

    def test_limits_providers_and_queries(self):
        for kwargs in [{'query':''}, {'query':'x', 'limit':True}, {'query':'x','limit':21}, {'query':'x','providers':['https://127.0.0.1']}, {'query':'x','providers':[]}, {'query':'x','offline':'yes'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                d.discover(**kwargs)

    def test_fetch_constructs_fixed_host_request(self):
        class Opener:
            def open(self, req, timeout):
                self_req = req.full_url
                self_assert(self_req.startswith(d.PROVIDERS['crossref'] + '?'))
                self_assert('http%3A%2F%2F127.0.0.1' in self_req)
                self_assert(timeout == d.TIMEOUT_SECONDS)
                return Response(b'{"status":"ok","message":{"items":[]}}', self_req)
        self_assert = self.assertTrue
        with patch.object(d, 'build_opener', return_value=Opener()):
            self.assertEqual(d._fetch_json('crossref','http://127.0.0.1',1)['status'], 'ok')

    def test_fetch_rejects_redirect_non_json_size_nonfinite(self):
        cases = [lambda u:Response(b'{}','https://evil.invalid/'), lambda u:Response(b'{}',u,'text/html'),
                 lambda u:Response(b'{}',u,length=d.MAX_RESPONSE_BYTES+1), lambda u:Response(b' '*(d.MAX_RESPONSE_BYTES+1),u),
                 lambda u:Response(b'{"x":NaN}',u)]
        for factory in cases:
            class Opener:
                def open(self, req, timeout):
                    return factory(req.full_url)
            with self.subTest(factory=factory), patch.object(d,'build_opener',return_value=Opener()), self.assertRaises(ValueError):
                d._fetch_json('crossref','test',1)
        with self.assertRaises(ValueError):
            d._NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid')

    def test_local_inspection_no_contents_or_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            (base/'model.py').write_text('raise RuntimeError("MUST NOT EXECUTE")')
            (base/'tokens.json').write_text('SUPER_SECRET')
            (base/'.env').write_text('SUPER_SECRET')
            result=d.inspect_local(base)
            self.assertEqual([x['title'] for x in result['items']], ['model.py'])
            self.assertNotIn('SUPER_SECRET', json.dumps(result))
            self.assertNotIn('RuntimeError', json.dumps(result))

    def test_local_inspection_rejects_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            base=Path(tmp)
            (Path(outside)/'outside.py').write_text('SUPER_SECRET')
            try:
                (base/'symlink').symlink_to(outside,target_is_directory=True)
            except OSError as exc:
                if getattr(exc, 'winerror', None) == 1314:
                    self.skipTest('Windows symlink privilege unavailable (WinError 1314)')
                raise
            result=d.inspect_local(base)
            self.assertEqual(result['items'], [])
            self.assertNotIn('SUPER_SECRET', json.dumps(result))
            with self.assertRaises(ValueError):
                d.inspect_local(base/'symlink')


    def test_local_caps_and_plan_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(140):
                (Path(tmp)/f'model{index}.py').touch()
            result=d.inspect_local(tmp)
            self.assertEqual(len(result['items']),128)
            self.assertTrue(result['truncated'])
        planned=d.plan('Изменения генетики взрослого организма',d.catalog(ROOT))
        self.assertEqual(planned['mode'],'rule_based')
        self.assertTrue(planned['gaps'])
        self.assertTrue(any(step['kind']=='model_validation' for step in planned['steps']))
        self.assertTrue(all('resource_ids' in step for step in planned['steps']))


if __name__ == '__main__':
    unittest.main()

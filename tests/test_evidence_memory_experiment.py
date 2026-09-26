"""Cloud-only execution: independent evaluator and fixed-pack regression checks."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from workbench.experiments import evidence_memory as experiment


class EvidenceMemoryExperimentTests(unittest.TestCase):
    def setUp(self):
        self.bundle = experiment.load_bundle()

    def request(self, case_id):
        case = next(c for c in self.bundle['cases'] if c['id'] == case_id)
        request = {key: case[key] for key in ('project_id', 'question', 'candidate_ids')}
        return case, request

    def retrieve(self, method, request):
        return experiment.retrieve(method, request, self.bundle['sources'], self.bundle['registry'],
                                   self.bundle['policies'], self.bundle['now'])

    def score(self, case, prediction):
        return experiment.score_prediction(case, prediction, self.bundle['sources'],
                                           self.bundle['registry'], self.bundle['now'])

    def test_same_answer_foreign_source_is_not_correct_and_search_continues(self):
        case, request = self.request('h04')
        baseline = self.retrieve('membership', request)
        aware = self.retrieve('provenance', request)
        self.assertEqual(baseline['answer'], aware['answer'])
        self.assertEqual(baseline['source_id'], 'b_shared')
        self.assertEqual(aware['source_id'], 'a_shared')
        bad, good = self.score(case, baseline), self.score(case, aware)
        self.assertTrue(bad['foreign_accept'])
        self.assertTrue(bad['wrong_source'])
        self.assertFalse(bad['correct_answer'])
        self.assertFalse(good['foreign_accept'])
        self.assertTrue(good['correct_answer'])
        self.assertEqual(aware['examined'], 2)

    def test_skip_expired_and_revoked_then_retrieve_valid_and_end_is_exclusive(self):
        for case_id, reason in (('d10', 'not_current'), ('h10', 'revoked')):
            with self.subTest(case=case_id):
                case, request = self.request(case_id)
                result = self.retrieve('provenance', request)
                self.assertEqual(result['source_id'], 'a_color')
                self.assertIn(reason, result['skipped'])
                row = self.score(case, result)
                self.assertFalse(row['expired_accept'])
                self.assertFalse(row['revoked_accept'])
                self.assertTrue(row['correct_answer'])
        case, request = self.request('d05')
        entry = self.bundle['registry']['a_expired']
        self.assertEqual(entry['valid_until'], self.bundle['manifest']['fixed_now'])
        self.assertEqual(self.retrieve('scope_only', request)['decision'], 'answer')
        self.assertEqual(self.retrieve('provenance', request)['decision'], 'refuse')

    def test_valid_integrity_does_not_imply_truth_or_relevance(self):
        case, request = self.request('h12')
        result = self.retrieve('provenance', request)
        self.assertEqual(result['decision'], 'answer')
        row = self.score(case, result)
        self.assertTrue(row['wrong_answer'])
        self.assertFalse(row['wrong_source'])
        self.assertTrue(row['invalid_accept'])
        for case_id in ('d08', 'h11'):
            _, request = self.request(case_id)
            for method in experiment.METHODS:
                self.assertEqual(self.retrieve(method, request)['decision'], 'refuse')

    def test_blob_binds_question_answer_and_source_not_only_answer(self):
        _, request = self.request('d01')
        source = self.bundle['sources']['a_color']
        original = source['blob_utf8']
        changed = json.loads(original)
        changed['question'] = 'What is the swapped question?'
        source['blob_utf8'] = json.dumps(changed, separators=(',', ':'))
        request['question'] = changed['question']
        self.assertEqual(self.retrieve('membership', request)['decision'], 'answer')
        self.assertEqual(self.retrieve('provenance', request)['decision'], 'refuse')
        _, request = self.request('h09')
        for method in experiment.METHODS:
            self.assertEqual(self.retrieve(method, request)['decision'], 'refuse')

    def test_positive_and_negative_regression_split_and_expected_ablation(self):
        result = experiment.evaluate(self.bundle)
        baseline = result['methods']['membership']['overall']
        scope = result['methods']['scope_only']['overall']
        aware = result['methods']['provenance']['overall']
        self.assertEqual((aware['cases'], aware['gold_answers'], aware['gold_refusals']), (24, 10, 14))
        self.assertEqual(aware['correct_answer_rate'], {'numerator': 9, 'denominator': 10, 'rate': 0.9})
        self.assertEqual(aware['correct_refusal_rate']['numerator'], 14)
        self.assertEqual(aware['wrong_answers'], 1)  # Registered false claim is deliberately retained.
        self.assertEqual(aware['foreign_accept_rate']['numerator'], 0)
        self.assertGreater(baseline['foreign_accept_rate']['numerator'], 0)
        self.assertEqual(scope['foreign_accept_rate']['numerator'], 0)
        self.assertGreater(scope['false_accept_rate']['numerator'], 0)
        self.assertGreater(scope['expired_accept_rate']['numerator'], 0)
        self.assertGreater(scope['revoked_accept_rate']['numerator'], 0)
        self.assertTrue(result['acceptance']['passed'])
        for split in experiment.SPLITS:
            split_metrics = result['methods']['provenance']['splits'][split]
            self.assertGreater(split_metrics['correct_answer_rate']['numerator'], 0)
            self.assertGreater(split_metrics['correct_refusal_rate']['numerator'], 0)

    def test_metrics_denominators_and_refuse_all_does_not_pass(self):
        refused = {'decision': 'refuse', 'answer': None, 'source_id': None, 'examined': 0, 'skipped': []}
        rows = [self.score(case, refused) for case in self.bundle['cases']]
        metrics = experiment.summarize(rows)
        self.assertEqual(metrics['correct_answer_rate'], {'numerator': 0, 'denominator': 10, 'rate': 0.0})
        self.assertEqual(metrics['correct_refusal_rate']['numerator'], 14)
        self.assertEqual(metrics['invalid_accept_share'], {'numerator': 0, 'denominator': 0, 'rate': None})
        self.assertEqual(experiment.summarize([])['false_accept_rate']['rate'], None)
        with mock.patch.object(experiment, 'retrieve', return_value=refused):
            result = experiment.evaluate(self.bundle)
        self.assertTrue(result['acceptance']['safety_cases_pass'])
        self.assertFalse(result['acceptance']['positive_coverage_both_splits'])
        self.assertFalse(result['acceptance']['passed'])
        case, request = self.request('h03')
        metrics = experiment.summarize([self.score(case, self.retrieve('membership', request))])
        self.assertEqual(metrics['false_accept_rate']['denominator'], 1)
        self.assertEqual(metrics['invalid_accept_share']['denominator'], 1)
        self.assertEqual(metrics['foreign_accept_rate']['numerator'], 1)

    def test_gold_never_reaches_retriever_or_changes_predictions(self):
        with mock.patch.object(experiment, 'retrieve', wraps=experiment.retrieve) as spy:
            before = experiment.evaluate(self.bundle)
        self.assertTrue(spy.call_args_list)
        for call in spy.call_args_list:
            self.assertEqual(set(call.args[1]), {'project_id', 'question', 'candidate_ids'})
        self.bundle['cases'][0]['expected']['answer'] = 'changed-gold-only'
        after = experiment.evaluate(self.bundle)
        for method in experiment.METHODS:
            self.assertEqual([r['prediction'] for r in before['methods'][method]['cases']],
                             [r['prediction'] for r in after['methods'][method]['cases']])
        self.assertNotEqual(before['methods']['provenance']['overall'], after['methods']['provenance']['overall'])

    def copy_pack(self, directory):
        for filename in ('manifest.json',) + experiment.FILES:
            shutil.copyfile(experiment.DEFAULT_DATA / filename, Path(directory) / filename)

    def rewrite_pack_file(self, directory, name, mutate):
        path = Path(directory) / name
        document = json.loads(path.read_text(encoding='utf-8'))
        mutate(document)
        raw = (json.dumps(document, sort_keys=True) + '\n').encode('utf-8')
        path.write_bytes(raw)
        manifest_path = Path(directory) / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['files'][name] = hashlib.sha256(raw).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

    def test_tamper_detected_even_before_case_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            self.copy_pack(directory)
            path = Path(directory) / 'sources.json'
            path.write_bytes(path.read_bytes() + b' ')
            with self.assertRaises(experiment.PackError):
                experiment.load_bundle(directory)
        _, request = self.request('h07')
        # Self-consistent attacker record digest does not modify the trusted registry.
        self.assertEqual(self.retrieve('scope_only', request)['decision'], 'answer')
        self.assertEqual(self.retrieve('provenance', request)['decision'], 'refuse')

    def test_rehashed_bad_schema_bool_policy_and_unknown_fields_fail_closed(self):
        mutations = [('cases.json', lambda d: d['cases'][0].update({'unexpected': 'not allowed'})),
                     ('registry.json', lambda d: d['policies']['alpha'].update({'min_trust': True})),
                     ('sources.json', lambda d: d['sources'][0].update({'blob_utf8': '{"answer":1}'})),
                     ('cases.json', lambda d: d['cases'][0].update({'candidate_ids': ['missing']}))]
        for name, mutate in mutations:
            with self.subTest(file=name), tempfile.TemporaryDirectory() as directory:
                self.copy_pack(directory)
                self.rewrite_pack_file(directory, name, mutate)
                with self.assertRaises(experiment.PackError):
                    experiment.load_bundle(directory)

    def test_duplicate_json_keys_path_expansion_and_file_size_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            self.copy_pack(directory)
            manifest_path = Path(directory) / 'manifest.json'
            raw = manifest_path.read_text(encoding='utf-8')
            manifest_path.write_text(raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1), encoding='utf-8')
            with self.assertRaises(experiment.PackError):
                experiment.load_bundle(directory)
            self.copy_pack(directory)
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['files']['../private.json'] = '0' * 64
            manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaises(experiment.PackError):
                experiment.load_bundle(directory)
            self.copy_pack(directory)
            manifest_path.write_bytes(b' ' * (experiment.MAX_FILE_BYTES + 1))
            with self.assertRaises(experiment.PackError):
                experiment.load_bundle(directory)

    def test_reproducible_results_and_portable_evidence_artifacts(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one = experiment.run_experiment(first)
            two = experiment.run_experiment(second)
            self.assertEqual(one['results'], two['results'])
            self.assertEqual(one['inputs'], two['inputs'])
            self.assertEqual(one['implementation_sha256'], two['implementation_sha256'])
            self.assertEqual(one['resources']['model_calls'], 0)
            self.assertEqual(one['resources']['network_calls'], 0)
            report = json.loads((Path(first) / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['results'], one['results'])
            self.assertIn('registered_false_claim', (Path(first) / 'report.md').read_text(encoding='utf-8'))
            transported = experiment.load_bundle(Path(first) / 'evidence-pack')
            self.assertEqual(experiment.evaluate(transported), one['results'])
            for name in experiment.FILES:
                raw = (Path(first) / 'evidence-pack' / name).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), one['inputs']['files'][name])


if __name__ == '__main__':
    unittest.main()

"""One finite synthetic retrieval experiment; no model, HTTP or dynamic code.

The accepted repository checkout is the trust root for this fixture registry and
manifest. Hashes detect changes against that manifest; they are not signatures.
Retrievers receive requests without gold labels. Only the evaluator sees gold.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sys
import time

from workbench.provenance_verifier import verify_provenance

DEFAULT_DATA = Path(__file__).resolve().parents[2] / 'data' / 'experiments' / 'evidence_memory'
# parents[2] is workbench's parent (repository root).
METHODS = ('membership', 'scope_only', 'provenance')
SPLITS = ('development', 'synthetic_holdout')
SCOPES = ('alpha', 'beta')
FILES = ('sources.json', 'registry.json', 'cases.json')
MAX_FILE_BYTES = 256 * 1024
MAX_ITEMS = 64
MAX_BLOB_BYTES = 1024
ID = re.compile(r'[a-z][a-z0-9_]{0,63}')
SHA = re.compile(r'[0-9a-f]{64}')
TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')


class PackError(ValueError):
    """Malformed or modified fixed experiment pack (no raw data in errors)."""


def _need(condition):
    if not condition:
        raise PackError('invalid_experiment_pack')


def _shape(value, fields, optional=()):
    _need(type(value) is dict and set(fields) <= value.keys() <= set(fields) | set(optional))


def _text(value, limit=256):
    _need(type(value) is str and 0 < len(value) <= limit and value.strip() == value)
    _need(all(32 <= ord(c) < 127 for c in value))
    return value


def _id(value):
    _need(type(value) is str and ID.fullmatch(value) is not None)
    return value


def _sha(value):
    _need(type(value) is str and SHA.fullmatch(value) is not None)


def _integer(value, low, high):
    _need(type(value) is int and low <= value <= high)


def _instant(value):
    _need(type(value) is str and TIME.fullmatch(value) is not None)
    try:
        return datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError as exc:
        raise PackError('invalid_experiment_pack') from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _bad_number(_):
    raise PackError('invalid_experiment_pack')


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad_number)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PackError('invalid_experiment_pack') from exc


def _read(path):
    try:
        with path.open('rb') as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        _need(len(raw) <= MAX_FILE_BYTES)
        return raw
    except OSError as exc:
        raise PackError('experiment_pack_unavailable') from exc


def _policy(policy, project):
    _shape(policy, ('project_id', 'purpose', 'min_trust', 'max_blob_bytes'))
    _need(policy == {'project_id': project, 'purpose': 'synthetic-recall',
                     'min_trust': 1, 'max_blob_bytes': MAX_BLOB_BYTES})
    _integer(policy['min_trust'], 1, 1)
    _integer(policy['max_blob_bytes'], MAX_BLOB_BYTES, MAX_BLOB_BYTES)


def _record(record):
    _shape(record, ('record_id', 'project_id', 'purpose', 'source_uri', 'digest', 'source_ts'),
           ('required_trust',))
    _id(record['record_id'])
    _need(record['project_id'] in SCOPES)
    _text(record['purpose'], 64)
    _text(record['source_uri'], 256)
    _sha(record['digest'])
    _instant(record['source_ts'])
    if 'required_trust' in record:
        _integer(record['required_trust'], -100, 100)


def _payload(source):
    value = _json(source['blob_utf8'])
    _shape(value, ('source_id', 'question', 'answer'))
    _id(value['source_id'])
    _text(value['question'])
    _text(value['answer'], 128)
    return value


def load_bundle(directory=DEFAULT_DATA):
    """Read only fixed filenames with size limits, hashes and strict schemas."""
    directory = Path(directory)
    raw_manifest = _read(directory / 'manifest.json')
    manifest = _json(raw_manifest)
    _shape(manifest, ('schema_version', 'experiment', 'license', 'origin', 'seed',
                      'label_rule', 'fixed_now', 'scopes', 'splits', 'methods', 'parameters', 'files'))
    _integer(manifest['schema_version'], 1, 1)
    _need(manifest['experiment'] == 'evidence-memory-v1' and manifest['license'] == 'CC0-1.0'
          and manifest['origin'] == 'generated-synthetic')
    _integer(manifest['seed'], 20260926, 20260926)
    _need(manifest['label_rule'] == 'first12hex(sha256(UTF8(str(seed)+":"+label_key)))')
    _need(manifest['fixed_now'] == '2026-09-26T00:00:00Z')
    _need(manifest['scopes'] == list(SCOPES) and manifest['splits'] == list(SPLITS)
          and manifest['methods'] == list(METHODS))
    _need(manifest['parameters'] == {'candidate_order': 'as_listed', 'match': 'exact_question',
                                    'max_candidates': 8, 'max_blob_bytes': MAX_BLOB_BYTES})
    _integer(manifest['parameters']['max_candidates'], 8, 8)
    _integer(manifest['parameters']['max_blob_bytes'], MAX_BLOB_BYTES, MAX_BLOB_BYTES)
    _shape(manifest['files'], FILES)
    raw_files = {}
    documents = {}
    for name in FILES:
        _sha(manifest['files'][name])
        raw_files[name] = _read(directory / name)
        _need(hashlib.sha256(raw_files[name]).hexdigest() == manifest['files'][name])
        documents[name] = _json(raw_files[name])
    for name, field in (('sources.json', 'sources'), ('cases.json', 'cases')):
        _shape(documents[name], ('schema_version', field))
        _integer(documents[name]['schema_version'], 1, 1)
        _need(type(documents[name][field]) is list and 1 <= len(documents[name][field]) <= MAX_ITEMS)
    trusted = documents['registry.json']
    _shape(trusted, ('schema_version', 'registry', 'policies'))
    _integer(trusted['schema_version'], 1, 1)
    _shape(trusted['policies'], SCOPES)
    for project in SCOPES:
        _policy(trusted['policies'][project], project)
    registry = trusted['registry']
    _need(type(registry) is dict and 1 <= len(registry) <= MAX_ITEMS)
    for record_id, entry in registry.items():
        _id(record_id)
        _shape(entry, ('project_id', 'purpose', 'source_uri', 'digest', 'source_ts',
                       'valid_from', 'valid_until', 'trust', 'revoked'))
        _record({key: entry[key] for key in ('project_id', 'purpose', 'source_uri', 'digest', 'source_ts')}
                | {'record_id': record_id})
        _integer(entry['trust'], 0, 100)
        _need(type(entry['revoked']) is bool)
        _need(_instant(entry['valid_from']) <= _instant(entry['source_ts']) < _instant(entry['valid_until']))
    sources = {}
    for source in documents['sources.json']['sources']:
        _shape(source, ('id', 'record', 'blob_utf8'))
        source_id = _id(source['id'])
        _need(source_id not in sources)
        _record(source['record'])
        _need(type(source['blob_utf8']) is str)
        try:
            _need(1 <= len(source['blob_utf8'].encode('utf-8')) <= MAX_BLOB_BYTES)
        except UnicodeError as exc:
            raise PackError('invalid_experiment_pack') from exc
        _payload(source)  # Types are valid even for deliberately tampered bytes.
        sources[source_id] = source
    cases = documents['cases.json']['cases']
    seen = set()
    for case in cases:
        _shape(case, ('id', 'split', 'category', 'project_id', 'question', 'candidate_ids', 'expected'))
        _id(case['id'])
        _need(case['id'] not in seen)
        seen.add(case['id'])
        _need(case['split'] in SPLITS and case['project_id'] in SCOPES)
        _id(case['category'])
        _text(case['question'])
        ids = case['candidate_ids']
        _need(type(ids) is list and len(ids) <= 8 and all(type(x) is str and x in sources for x in ids))
        _need(len(ids) == len(set(ids)))
        gold = case['expected']
        _shape(gold, ('decision', 'answer', 'source_ids'))
        _need(gold['decision'] in ('answer', 'refuse') and type(gold['source_ids']) is list)
        _need(all(type(x) is str and x in ids for x in gold['source_ids']))
        _need(len(gold['source_ids']) == len(set(gold['source_ids'])))
        if gold['decision'] == 'answer':
            _text(gold['answer'], 128)
            _need(bool(gold['source_ids']))
        else:
            _need(gold['answer'] is None and gold['source_ids'] == [])
    # Both splits must contain positive and negative cases: always-refuse cannot pass unnoticed.
    for split in SPLITS:
        _need({c['expected']['decision'] for c in cases if c['split'] == split} == {'answer', 'refuse'})
    return {'manifest': manifest, 'manifest_sha256': hashlib.sha256(raw_manifest).hexdigest(),
            'sources': sources, 'registry': registry, 'policies': trusted['policies'], 'cases': cases,
            'now': _instant(manifest['fixed_now']), 'raw_files': raw_files, 'raw_manifest': raw_manifest}


def retrieve(method, request, sources, registry, policies, now):
    """No gold labels or training; deterministic ordered exact-question lookup."""
    if method not in METHODS:
        raise ValueError('unknown_method')
    _shape(request, ('project_id', 'question', 'candidate_ids'))
    policy = policies[request['project_id']]
    examined = 0
    skipped = []
    for source_id in request['candidate_ids']:
        source = sources[source_id]
        payload = _payload(source)
        examined += 1
        if payload['source_id'] != source['record']['record_id']:
            skipped.append('payload_source_binding')
            continue
        if payload['question'] != request['question']:
            skipped.append('irrelevant_question')
            continue
        if method == 'scope_only':
            entry = registry.get(source['record']['record_id'])
            if entry is None or any(entry[key] != policy[key] or source['record'][key] != entry[key]
                                    for key in ('project_id', 'purpose')):
                skipped.append('scope_or_membership')
                continue
        elif method == 'provenance':
            check = verify_provenance(source['blob_utf8'].encode('utf-8'), source['record'],
                                      registry, policy, now)
            if not check.accepted:
                skipped.append(check.reason)
                continue
        return {'decision': 'answer', 'answer': payload['answer'], 'source_id': source_id,
                'examined': examined, 'skipped': skipped}
    return {'decision': 'refuse', 'answer': None, 'source_id': None,
            'examined': examined, 'skipped': skipped}


def _entry(source_id, sources, registry):
    return registry.get(sources[source_id]['record']['record_id']) if source_id is not None else None


def score_prediction(case, prediction, sources, registry, now):
    """Gold comparison does not call the method or verifier to manufacture labels."""
    _shape(prediction, ('decision', 'answer', 'source_id', 'examined', 'skipped'))
    answered = prediction['decision'] == 'answer'
    _need(prediction['decision'] in ('answer', 'refuse'))
    if answered:
        _need(type(prediction['source_id']) is str and prediction['source_id'] in case['candidate_ids'])
        _text(prediction['answer'], 128)
    else:
        _need(prediction['source_id'] is None and prediction['answer'] is None)
    gold = case['expected']
    answerable = gold['decision'] == 'answer'
    right_source = answered and prediction['source_id'] in gold['source_ids']
    right_text = answered and prediction['answer'] == gold['answer']
    correct_answer = answerable and right_source and right_text
    entries = [_entry(sid, sources, registry) for sid in case['candidate_ids']]
    chosen = _entry(prediction['source_id'], sources, registry)
    foreign = lambda e: e is not None and e['project_id'] != case['project_id']
    expired = lambda e: e is not None and now >= _instant(e['valid_until'])
    revoked = lambda e: e is not None and e['revoked']
    return {'case_id': case['id'], 'split': case['split'], 'category': case['category'],
            'prediction': prediction, 'gold': gold, 'answerable': answerable, 'answered': answered,
            'correct_answer': correct_answer, 'correct_refusal': not answerable and not answered,
            'false_refusal': answerable and not answered,
            'false_accept': not answerable and answered,
            'invalid_accept': answered and not correct_answer,
            'wrong_answer': answerable and answered and not right_text,
            'wrong_source': answerable and answered and not right_source,
            'foreign_probe': any(foreign(e) for e in entries), 'foreign_accept': answered and foreign(chosen),
            'expired_probe': any(expired(e) for e in entries), 'expired_accept': answered and expired(chosen),
            'revoked_probe': any(revoked(e) for e in entries), 'revoked_accept': answered and revoked(chosen),
            'unknown_source_accept': answered and chosen is None}


def _rate(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'rate': numerator / denominator if denominator else None}


def summarize(rows):
    count = lambda key: sum(bool(row[key]) for row in rows)
    positives, accepted = count('answerable'), count('answered')
    negatives = len(rows) - positives
    return {'cases': len(rows), 'answers': accepted, 'gold_answers': positives, 'gold_refusals': negatives,
            'correct_answer_rate': _rate(count('correct_answer'), positives),
            'correct_refusal_rate': _rate(count('correct_refusal'), negatives),
            'false_accept_rate': _rate(count('false_accept'), negatives),
            'invalid_accept_share': _rate(count('invalid_accept'), accepted),
            'foreign_accept_rate': _rate(count('foreign_accept'), count('foreign_probe')),
            'expired_accept_rate': _rate(count('expired_accept'), count('expired_probe')),
            'revoked_accept_rate': _rate(count('revoked_accept'), count('revoked_probe')),
            'wrong_answers': count('wrong_answer'), 'wrong_sources': count('wrong_source'),
            'false_refusals': count('false_refusal'), 'unknown_source_accepts': count('unknown_source_accept')}


def evaluate(bundle):
    results = {}
    for method in METHODS:
        rows = []
        for case in bundle['cases']:
            # Explicit projection is the only input boundary: labels are never passed to retrieval.
            request = {key: case[key] for key in ('project_id', 'question', 'candidate_ids')}
            prediction = retrieve(method, request, bundle['sources'], bundle['registry'],
                                  bundle['policies'], bundle['now'])
            rows.append(score_prediction(case, prediction, bundle['sources'], bundle['registry'], bundle['now']))
        results[method] = {'overall': summarize(rows),
                           'splits': {split: summarize([r for r in rows if r['split'] == split]) for split in SPLITS},
                           'cases': rows}
    metrics = results['provenance']['overall']
    safety = all(metrics[key]['numerator'] == 0 for key in
                 ('foreign_accept_rate', 'expired_accept_rate', 'revoked_accept_rate', 'false_accept_rate'))
    coverage = all(results['provenance']['splits'][split]['correct_answer_rate']['numerator'] > 0 for split in SPLITS)
    return {'methods': results, 'acceptance': {'zero_foreign_scope_accepts': metrics['foreign_accept_rate']['numerator'] == 0,
            'safety_cases_pass': safety, 'positive_coverage_both_splits': coverage,
            'passed': safety and coverage},
            'limitations': ['synthetic regression only; split visible to developer, not blinded',
                'gold labels written by fixture authors, not independently validated',
                'integrity and scoped admission do not establish truth: registered_false_claim is deliberately wrong',
                'all synthetic bytes loaded; measured leakage is selected output evidence, not read isolation',
                'trusted checkout supplies registry and manifest; no signature or remote trust anchor',
                'exact question matching only; no LLM, embeddings, semantic retrieval or human-memory claim']}


def markdown_report(report):
    lines = ['# Synthetic evidence memory experiment', '',
             'Finite CC0 fixture; no LLM or network. Rates retain numerator/denominator; null means not estimable.', '',
             '| Method | Correct answer | Correct refusal | False accept | Foreign output | Expired output | Revoked output |',
             '|---|---:|---:|---:|---:|---:|---:|']
    keys = ('correct_answer_rate', 'correct_refusal_rate', 'false_accept_rate', 'foreign_accept_rate',
            'expired_accept_rate', 'revoked_accept_rate')
    for method in METHODS:
        metrics = report['results']['methods'][method]['overall']
        cells = [str(metrics[key]['numerator']) + '/' + str(metrics[key]['denominator']) for key in keys]
        lines.append('| ' + method + ' | ' + ' | '.join(cells) + ' |')
    lines += ['', 'Acceptance: ' + json.dumps(report['results']['acceptance'], sort_keys=True), '',
              'Acceptance covers the named safety cases and nonzero positive coverage; it does not require or claim factual truth.',
              '', '## Limits', '']
    lines += ['- ' + item for item in report['results']['limitations']]
    lines += ['', '## Incorrect decisions (including the intentionally false registered claim)', '']
    for method in METHODS:
        for row in report['results']['methods'][method]['cases']:
            if not (row['correct_answer'] or row['correct_refusal']):
                lines.append('- ' + method + ' / ' + row['case_id'] + ' / ' + row['category']
                             + ': ' + row['prediction']['decision'] + ', source=' + str(row['prediction']['source_id']))
    lines += ['', 'Full per-case predictions, gold, split metrics, hashes and execution cost: report.json.',
              'Wall seconds: ' + str(report['resources']['wall_seconds']) + '; process CPU seconds: '
              + str(report['resources']['process_cpu_seconds']) + '.', '']
    return '\n'.join(lines)


def run_experiment(output_dir, data_dir=DEFAULT_DATA):
    start_wall, start_cpu = time.perf_counter(), time.process_time()
    bundle = load_bundle(data_dir)
    results = evaluate(bundle)
    report = {'schema_version': 1, 'experiment': 'evidence-memory-v1',
              'inputs': {'manifest_sha256': bundle['manifest_sha256'], 'files': bundle['manifest']['files'],
                         'seed': bundle['manifest']['seed'], 'fixed_now': bundle['manifest']['fixed_now']},
              'implementation_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in
                  (Path(__file__), Path(__file__).resolve().parents[1] / 'provenance_verifier.py')},
              'results': results, 'resources': {'wall_seconds': time.perf_counter() - start_wall,
                  'process_cpu_seconds': time.process_time() - start_cpu, 'timing_scope': 'load_and_evaluate_before_report_write',
                  'python': platform.python_version(), 'platform': platform.system(),
                  'model_calls': 0, 'network_calls': 0, 'paid_inference_usd': 0,
                  'host_billing': 'not measured; execution uses caller-authorized cloud runner'}}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pack_dir = output_dir / 'evidence-pack'
    pack_dir.mkdir(exist_ok=True)
    for name, raw in bundle['raw_files'].items():
        (pack_dir / name).write_bytes(raw)
    (pack_dir / 'manifest.json').write_bytes(bundle['raw_manifest'])
    (output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    (output_dir / 'report.md').write_text(markdown_report(report), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_experiment(args.output_dir)
    except (PackError, OSError) as exc:
        print(json.dumps({'status': 'experiment_failed', 'reason': type(exc).__name__}))
        return 2
    print(json.dumps({'acceptance': report['results']['acceptance'],
                     'methods': {method: report['results']['methods'][method]['overall'] for method in METHODS}},
                     sort_keys=True, allow_nan=False))
    return 0 if report['results']['acceptance']['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())

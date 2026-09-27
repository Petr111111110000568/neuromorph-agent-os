import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const {repository, sourceURL, validateCatalog, validateResponse, Submission, RequestEpoch} = require('../web/runs.js');

function fixture() {
  const make = (phase, run, hash) => {
    const identity = {repository, workflow_id: '.github/workflows/m02-checkpoint.yml', branch: 'main', commit: 'c'.repeat(40), run_id: run};
    return {receipt_id: 'm02-' + phase + '-' + run, title: phase === 'seed' ? 'Исходный запуск' : 'Восстановление',
      phase, identity, source_url: 'https://github.com/' + repository + '/actions/runs/' + run,
      canonical_sha256: hash.repeat(64), receipt_representation: 'bounded_projection',
      limitations: ['Synthetic fixed results; no scientific validation.'],
      summary: {task_count: 2, accepted_tasks: phase === 'seed' ? 1 : 2,
        actual_attempts: phase === 'seed' ? 1 : 3, reserved_attempts: phase === 'seed' ? 4 : 6,
        max_reserved_attempts: 8, review_count: phase === 'seed' ? 1 : 3, transition_count: 8,
        model_calls: 0, additional_spend_usd: 0},
      receipt: {identity: {...identity}, phase, previous_run_id: phase === 'resume' ? '123' : null,
        previous_manifest_sha256: phase === 'resume' ? 'd'.repeat(64) : null,
        state: {reviews: [{task_id: 'synthetic-task', revision: 1, decision: 'accepted'}]}}};
  };
  return [make('seed', '123', 'a'), make('resume', '124', 'b')];
}
function record(entry, project = 'project-one') {
  return {id: 'import-' + project + '-' + entry.phase, project_id: project, receipt_id: entry.receipt_id,
    canonical_sha256: entry.canonical_sha256, identity: {...entry.identity}, source_url: entry.source_url,
    phase: entry.phase, title: entry.title, summary: {...entry.summary}, imported_at: '2026-09-27T03:00:00Z'};
}
function response(catalog, imports = []) {
  return {catalog, imports, capabilities: {import_receipts: true, historical: true, live_verification: false,
    execution: false, model_calls: false, network_calls: false, scientific_validation: false}};
}

test('catalog admits the seed and resume projections with distinct source and manifest digests', () => {
  const catalog = fixture();
  assert.deepEqual(validateCatalog([catalog[1], catalog[0]]), catalog);
  assert.notEqual(catalog[0].canonical_sha256, catalog[1].receipt.previous_manifest_sha256);
  assert.equal(validateResponse(response(catalog)).catalog.length, 2);
});

test('catalog refuses broken provenance, wrong source URL, full receipt claims and broken lineage', () => {
  const mutations = [
    catalog => { catalog[1].receipt.previous_run_id = 'foreign-run'; },
    catalog => { catalog[1].receipt.previous_manifest_sha256 = ''; },
    catalog => { catalog[1].identity.commit = 'e'.repeat(40); catalog[1].receipt.identity.commit = 'e'.repeat(40); },
    catalog => { catalog[1].receipt.identity.run_id = '999'; },
    catalog => { catalog[0].receipt_representation = 'full_receipt'; },
    catalog => { catalog[0].source_url = 'https://example.com/external'; },
    catalog => { catalog[0].canonical_sha256 = 'not-a-sha'; },
    catalog => { catalog[1].phase = 'seed'; catalog[1].receipt.phase = 'seed'; },
  ];
  for (const mutate of mutations) { const catalog = fixture(); mutate(catalog); assert.throws(() => validateCatalog(catalog)); }
  assert.throws(() => validateCatalog(fixture().slice(0, 1)));
});

test('catalog display never converts current execution, model calls or live verification into historical success', () => {
  for (const key of ['execution', 'live_verification']) {
    const data = response(fixture()); data.capabilities[key] = true;
    assert.throws(() => validateResponse(data));
  }
  const data = response(fixture()); data.catalog[0].summary.model_calls = 1;
  assert.throws(() => validateResponse(data));
});

test('source links are exact GitHub run identities without credentials or executable schemes', () => {
  const entry = fixture()[0];
  assert.equal(sourceURL(entry.source_url, entry.identity), entry.source_url);
  for (const url of ['javascript:alert(1)', '/relative', 'http://github.com/x',
    entry.source_url + '?redirect=other', entry.source_url + '#fragment',
    entry.source_url.replace('https://', 'https://user:password@')]) assert.equal(sourceURL(url, entry.identity), null);
});

test('valid backend import capacity remains readable through 4000 associations', () => {
  const catalog = fixture();
  const imports = Array.from({length: 4000}, (_, index) => record(catalog[index % 2], 'project-' + Math.floor(index / 2)));
  assert.equal(validateResponse(response(catalog, imports)).imports.length, 4000);
  assert.throws(() => validateResponse(response(catalog, [...imports, record(catalog[0], 'one-too-many')])));
});

test('ambiguous timeout or lost response preserves one immutable request and key', () => {
  const submission = new Submission(), catalog = fixture(); let calls = 0;
  const request = submission.capture('project-one', catalog[0], () => 'key-' + ++calls);
  for (let retry = 0; retry < 3; retry++) {
    assert.equal(submission.capture('project-one', catalog[0], () => 'key-' + ++calls), request);
  }
  assert.equal(calls, 1);
  assert.equal(request.idempotency_key, 'key-1');
  assert.throws(() => { request.receipt_id = 'other'; });
  assert.throws(() => submission.reset());
});

test('pending owner project, receipt or source digest cannot change under the old key', () => {
  const submission = new Submission(), catalog = fixture();
  const request = submission.capture('project-one', catalog[0], () => 'original-key');
  assert.throws(() => submission.capture('another-project', catalog[0], () => 'replacement-key'));
  assert.throws(() => submission.capture('project-one', catalog[1], () => 'replacement-key'));
  assert.throws(() => submission.capture('project-one', {...catalog[0], canonical_sha256: 'f'.repeat(64)}, () => 'replacement-key'));
  assert.equal(submission.pending.payload, request);
});

test('unknown or mismatched acknowledgment cannot unlock pending selection', () => {
  const submission = new Submission(), catalog = fixture();
  const request = submission.capture('project-one', catalog[0], () => 'key');
  for (const bad of [null, {}, {id: 'not-an-import'}, record(catalog[0], 'foreign-project'), record(catalog[1])]) {
    assert.throws(() => submission.acknowledge(bad, catalog));
    assert.equal(submission.pending.payload, request);
    assert.equal(submission.completed, null);
  }
  const altered = record(catalog[0]); altered.identity.run_id = '999';
  assert.throws(() => submission.acknowledge(altered, catalog));
  assert.throws(() => submission.reset());
});

test('refresh can reconcile a server-committed import after lost POST acknowledgment', () => {
  const submission = new Submission(), catalog = fixture();
  submission.capture('project-one', catalog[0], () => 'key');
  const saved = record(catalog[0]);
  assert.equal(submission.reconcile([record(catalog[0], 'foreign-project')], catalog), null);
  assert.equal(submission.completed, null);
  assert.deepEqual(submission.reconcile([saved], catalog), saved);
  assert.equal(submission.pending.payload.idempotency_key, 'key');
});

test('conflicting provenance does not become a completed import during reconciliation', () => {
  const submission = new Submission(), catalog = fixture();
  submission.capture('project-one', catalog[0], () => 'old-key');
  const saved = record(catalog[0]); saved.canonical_sha256 = 'f'.repeat(64);
  assert.equal(submission.reconcile([saved], catalog), null);
  assert.equal(submission.pending.payload.idempotency_key, 'old-key');
  assert.throws(() => submission.reset());
});

test('only a confirmed import permits explicit reset and a new association', () => {
  const submission = new Submission(), catalog = fixture();
  submission.capture('project-one', catalog[0], () => 'old-key');
  submission.acknowledge(record(catalog[0]), catalog);
  assert.throws(() => submission.capture('project-one', catalog[1], () => 'new-key'));
  submission.reset();
  assert.equal(submission.capture('project-one', catalog[1], () => 'new-key').idempotency_key, 'new-key');
});

test('refresh drops the previous confirmation until the current owner snapshot confirms it', () => {
  const submission = new Submission(), catalog = fixture();
  const payload = submission.capture('project-one', catalog[0], () => 'original-key');
  submission.acknowledge(record(catalog[0]), catalog);
  submission.clearConfirmation();
  assert.equal(submission.completed, null);
  assert.equal(submission.pending.payload, payload);
  assert.equal(submission.reconcile([record(catalog[0], 'another-owner-project')], catalog), null);
  assert.throws(() => submission.reset());
  assert.deepEqual(submission.reconcile([record(catalog[0])], catalog), record(catalog[0]));
  // Even without clearConfirmation, reconciliation must not reuse a stale ack.
  assert.equal(submission.reconcile([], catalog), null);
  assert.equal(submission.pending.payload.idempotency_key, 'original-key');
});

test('late failure from an abandoned refresh cannot clear a newer authenticated view', async () => {
  const epochs = new RequestEpoch();
  const state = {session: null, projects: [], busy: false};
  const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
  const firstCatalog = deferred(), firstProjects = deferred();
  const old = epochs.begin();
  state.busy = true;
  const oldFlow = Promise.all([firstCatalog.promise, firstProjects.promise])
    .catch(() => epochs.apply(old, () => { state.projects = []; }))
    .finally(() => epochs.apply(old, () => { state.busy = false; }));
  firstCatalog.reject(new Error('503 before the sibling request finishes'));
  await oldFlow;
  assert.equal(state.busy, false);
  const current = epochs.begin();
  epochs.apply(current, () => { state.session = 'new-owner'; state.projects = ['new-project']; state.busy = false; });
  firstProjects.reject(Object.assign(new Error('late 401 from previous refresh'), {status: 401}));
  await Promise.resolve();
  assert.equal(epochs.apply(old, () => { state.session = null; state.projects = []; state.busy = true; }), false);
  assert.deepEqual(state, {session: 'new-owner', projects: ['new-project'], busy: false});
  assert.equal(epochs.isCurrent(current), true);
});

test('late success and finally from a replaced request cannot acknowledge or unlock current work', () => {
  const epochs = new RequestEpoch(), state = {saved: false, busy: true};
  const old = epochs.begin();
  const current = epochs.begin();
  assert.equal(epochs.apply(old, () => { state.saved = true; state.busy = false; }), false);
  assert.deepEqual(state, {saved: false, busy: true});
  assert.equal(epochs.apply(current, () => { state.saved = true; state.busy = false; }), true);
  assert.deepEqual(state, {saved: true, busy: false});
});

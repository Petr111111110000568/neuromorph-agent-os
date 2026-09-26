import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const {build, safeURL, Submission} = require('../web/science.js');
const catalog = {items:[{id:'hermes'}],templates:[{id:'literature-review'}]};
const projects = [{id:'project1'}];
const fields = {project_id:'project1',template_id:'literature-review',platform_id:'hermes',title:'Study',question:'Question',hypothesis:'Hypothesis',method:'Method',control:'Control',criteria:'Criteria'};
test('scientific protocol requires known project, template and platform', () => {
  assert.deepEqual(build(fields,catalog,projects),fields);
  for (const key of ['project_id','template_id','platform_id']) assert.throws(() => build({...fields,[key]:'unknown'},catalog,projects));
});
test('scientific protocol requires substantive bounded fields', () => {
  for (const key of ['title','question','hypothesis','method','control','criteria']) assert.throws(() => build({...fields,[key]:'   '},catalog,projects));
  assert.throws(() => build({...fields,title:'a'.repeat(201)},catalog,projects));
  assert.throws(() => build({...fields,control:'bad\0control'},catalog,projects));
});
test('ambiguous save retry keeps immutable identity and disallows changed payload', () => {
  const submission = new Submission(); let sequence = 0;
  const request = submission.capture(fields,() => 'key' + ++sequence);
  fields.extra = undefined;
  assert.equal(submission.capture({...fields},() => 'key' + ++sequence),request);
  assert.equal(sequence,1);
  assert.throws(() => submission.capture({...fields,title:'Different'},() => 'new-key'));
  assert.throws(() => { request.title = 'tampered'; });
  assert.throws(() => submission.reset());
  delete fields.extra;
});
test('only actual task acknowledgment permits a new submission', () => {
  const s = new Submission(); s.capture(fields,() => 'old-key');
  assert.throws(() => s.acknowledge({id:'x',kind:'project'}));
  assert.throws(() => s.acknowledge({kind:'task'}));
  assert.throws(() => s.acknowledge({id:'task-id',version:1,status:'planned',project_id:'foreign'}));
  s.acknowledge({id:'task-id',version:1,status:'planned',project_id:'project1'});
  assert.throws(() => s.capture(fields,() => 'new-key'));
  s.reset(); assert.equal(s.capture(fields,() => 'new-key').idempotency_key,'new-key');
});
test('auth, conflict and server errors retain pending identity', () => {
  const s = new Submission(), request = s.capture(fields,() => 'key');
  for (const status of [undefined,401,403,409,500,503]) { assert.equal(s.rejectValidation(status),false); assert.equal(s.pending.payload,request); }
});
test('definitive validation rejection permits corrected submission', () => {
  const s = new Submission(); s.capture(fields,() => 'old-key');
  assert.equal(s.rejectValidation(400),true);
  assert.equal(s.capture({...fields,title:'Corrected'},() => 'new-key').idempotency_key,'new-key');
});
test('catalog links reject executable schemes and embedded credentials', () => {
  for (const url of ['javascript:alert(1)','http://example.com','https://user:pass@example.com','https://example.com:444','/relative']) assert.equal(safeURL(url),null);
  assert.equal(safeURL('https://example.com/path'),'https://example.com/path');
});

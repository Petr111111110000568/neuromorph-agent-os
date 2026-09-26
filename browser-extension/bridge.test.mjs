import test from 'node:test';
import assert from 'node:assert/strict';
let listener, injections=0, grant=true, failInjection=false, injectionGate=null;
function area(){const data={};return {data,async get(keys){if(typeof keys==='string')return {[keys]:data[keys]};if(Array.isArray(keys))return Object.fromEntries(keys.map(k=>[k,data[k]]));return {...keys,...data};},async set(value){Object.assign(data,structuredClone(value));},async remove(key){delete data[key];}};}
const local=area(), session=area();
globalThis.chrome={
  storage:{local,session},
  runtime:{onMessage:{addListener(fn){listener=fn;}},onInstalled:{addListener(){}},onStartup:{addListener(){}}},
  alarms:{onAlarm:{addListener(){}},create(){}},
  permissions:{async contains(){return grant;}},
  tabs:{async get(id){return {id,url:'https://chat.deepseek.com/a/chat/s/test'};},async query(){return[];}},
  scripting:{async executeScript(){injections++;if(injectionGate)await injectionGate;if(failInjection)throw new Error('simulated interruption');return [{result:{ok:true,status:'send_clicked_unconfirmed',baseline_responses:0}}];}}
};
await import('./background.js');
const sender={tab:{id:4},frameId:0,url:'http://127.0.0.1:8765/assistants.html'};
const payload={task_id:'task-12345678',tab_id:8,provider:'deepseek',url:'https://chat.deepseek.com/a/chat/s/test',prompt:'public question'};
function request(action,p=payload,s=sender){return new Promise(resolve=>listener({action,payload:p},s,resolve));}
function reset(){for(const k of Object.keys(local.data))delete local.data[k];for(const k of Object.keys(session.data))delete session.data[k];local.data.enabled=['deepseek'];local.data.scan=true;injections=0;grant=true;failInjection=false;injectionGate=null;}
test('foreign pages, frames and revoked grants cannot invoke provider UI',async()=>{
  reset();
  assert.equal((await request('dispatch',payload,{...sender,url:'https://evil.test/assistants.html'})).ok,false);
  assert.equal((await request('dispatch',payload,{...sender,frameId:7})).ok,false);
  grant=false;assert.equal((await request('dispatch')).ok,false);assert.equal(injections,0);
});
test('one reserved task blocks concurrent and repeated dispatch, even after worker session loss',async()=>{
  reset();
  const results=await Promise.all([request('dispatch'),request('dispatch',{...payload,task_id:'task-87654321'})]);
  assert.equal(results.filter(x=>x.ok).length,1);assert.equal(injections,1);assert.equal(local.data.history.length,1);
  for(const key of Object.keys(session.data))delete session.data[key];
  assert.equal((await request('dispatch',{...payload,task_id:'task-22222222'})).ok,false);assert.equal(injections,1);
  assert.equal((await request('resolve',{task_id:payload.task_id,confirmed:true})).ok,true);
  assert.equal((await request('dispatch')).ok,false);assert.equal(injections,1);
});
test('uncertain injection leaves a durable nonrefundable fence',async()=>{
  reset();failInjection=true;
  assert.equal((await request('dispatch')).ok,false);
  assert.equal(local.data.pending.task_id,payload.task_id);assert.equal(local.data.history.length,1);
  assert.equal((await request('dispatch',{...payload,task_id:'task-33333333'})).ok,false);assert.equal(injections,1);
});
test('result capture requires the matching task, tab and URL',async()=>{
  reset();await request('dispatch');
  assert.equal((await request('capture',{...payload,task_id:'task-99999999'})).ok,false);
  assert.equal((await request('capture',{...payload,url:'https://chat.deepseek.com/a/chat/s/other'})).ok,false);
  assert.equal(injections,1);
});
test('resolve is rejected while injection is active and stale final write cannot replace another task',async()=>{
  reset();let finish;injectionGate=new Promise(resolve=>{finish=resolve;});const pending=request('dispatch');
  while(!injections)await new Promise(resolve=>setImmediate(resolve));
  assert.equal((await request('resolve',{task_id:payload.task_id,confirmed:true})).ok,false);
  local.data.pending={task_id:'replacement-task',chat_url:payload.url,tab_id:8};finish();
  assert.equal((await pending).ok,false);assert.equal(local.data.pending.task_id,'replacement-task');
});
test('resolve owns the same lock across storage await, rejecting second resolve and new dispatch',async()=>{
  reset();local.data.pending={task_id:payload.task_id,chat_url:payload.url,tab_id:8};
  const originalRemove=local.remove;let finish,entered=false;
  local.remove=async key=>{entered=true;await new Promise(resolve=>{finish=resolve;});await originalRemove(key);};
  try {
    const first=request('resolve',{task_id:payload.task_id,confirmed:true});
    while(!entered)await new Promise(resolve=>setImmediate(resolve));
    assert.equal((await request('resolve',{task_id:payload.task_id,confirmed:true})).ok,false);
    assert.equal((await request('dispatch',{...payload,task_id:'task-new-1234'})).ok,false);
    assert.equal(injections,0);finish();assert.equal((await first).ok,true);
    assert.equal((await request('dispatch',{...payload,task_id:'task-new-1234'})).ok,true);
    assert.equal(local.data.pending.task_id,'task-new-1234');
  } finally {local.remove=originalRemove;}
});

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
function app(){
  const elements=new Map();
  const element=()=>({value:'',textContent:'',checked:false,disabled:false,options:[],classList:{toggle(){}},replaceChildren(...items){this.options=items;},append(...items){this.options.push(...items);}});
  const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  const context={console,URL,Blob,Map,Set,Date,JSON,Object,Error,Promise,crypto:{randomUUID:()=> 'test-random-12345'},location:{origin:'http://127.0.0.1:8765'},window:{addEventListener(){},postMessage(){}},document:{hidden:false,getElementById:get,createElement:element},localStorage:{getItem:()=>null,setItem(){},removeItem(){}},Option:function(text,value){return {text,value};},setTimeout:()=>1,clearTimeout(){},setInterval(){},confirm:()=>true,navigator:{},fetch:async()=>({ok:true,json:async()=>({projects:[]})})};
  vm.createContext(context);
  const source=fs.readFileSync(new URL('../web/assistants.js',import.meta.url),'utf8');
  vm.runInContext(source+'\nglobalThis.hooks={setTask(t){advance();current=t;},snapshot,poll,replaceRequest(fn){request=fn;},selection(){return $("get-selection").onclick();},edit(){return $("answer").oninput();},bound};',context);
  return {hooks:context.hooks,get};
}
const task=id=>({task_id:id,tab_id:8,provider:'deepseek',chat_url:'https://chat.deepseek.com/a/chat/s/'+id});
const result=t=>({ok:true,...t,status:'captured_unconfirmed',text:'answer '+t.task_id});
test('old capture cannot overwrite a new task after resolve or send changes generation',async()=>{
  const {hooks,get}=app(), a=task('task-old'),b=task('task-new');hooks.setTask(a);const snapshot=hooks.snapshot();let finish;
  hooks.replaceRequest(()=>new Promise(resolve=>{finish=resolve;}));const pending=hooks.poll(snapshot);hooks.setTask(b);finish(result(a));await pending;
  assert.equal(get('answer').value,'');
});
test('capture validates task, provider and chat URL before accepting text',async()=>{
  const {hooks,get}=app(),a=task('task-current');hooks.setTask(a);hooks.replaceRequest(async()=>({...result(a),chat_url:'https://chat.deepseek.com/other'}));await hooks.poll(hooks.snapshot());assert.equal(get('answer').value,'');
  hooks.replaceRequest(async()=>result(a));await hooks.poll(hooks.snapshot());assert.equal(get('answer').value,'answer task-current');assert.equal(get('verified').checked,false);
});
test('late selection cannot be attributed to a replacement task',async()=>{
  const {hooks,get}=app(),a=task('task-old');hooks.setTask(a);let finish;hooks.replaceRequest(()=>new Promise(resolve=>{finish=resolve;}));const pending=hooks.selection();hooks.setTask(task('task-new'));finish(result(a));await pending;assert.equal(get('answer').value,'');
});
test('manual editing invalidates an in-flight capture',async()=>{
  const {hooks,get}=app(),a=task('task-current');hooks.setTask(a);let finish;hooks.replaceRequest(()=>new Promise(resolve=>{finish=resolve;}));const pending=hooks.poll(hooks.snapshot());get('answer').value='my edited result';hooks.edit();finish(result(a));await pending;assert.equal(get('answer').value,'my edited result');
});

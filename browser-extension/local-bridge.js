'use strict';
// Runs only on the exact local assistant page; never in a provider's main world.
window.addEventListener('message', async event=>{
  if(event.source!==window || event.origin!==location.origin || window!==window.top) return;
  const m=event.data;
  if(!m || m.channel!=='neuromorph-request-v1' || typeof m.id!=='string' || m.id.length>100) return;
  if(!['status','scan','dispatch','selection','capture','resolve'].includes(m.action)) return;
  let result;
  try {result=await chrome.runtime.sendMessage({action:m.action,payload:m.payload});}
  catch {result={ok:false,error:'Расширение недоступно. Перезагрузите эту страницу после его установки.'};}
  window.postMessage({channel:'neuromorph-response-v1',id:m.id,result},location.origin);
});

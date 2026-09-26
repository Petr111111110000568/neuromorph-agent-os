import {PROVIDERS} from './policy.mjs';
const status=document.querySelector('#status');
async function render() {
  const s=await chrome.storage.local.get({enabled:[],scan:false});
  document.querySelector('#scan').checked=s.scan;
  const root=document.querySelector('#providers');root.replaceChildren();
  for(const [id,p] of Object.entries(PROVIDERS)) {
    const row=document.createElement('div');row.className='row';
    const name=document.createElement('span');name.textContent=p.name;
    const button=document.createElement('button');const enabled=s.enabled.includes(id);button.textContent=enabled?'Отключить':'Разрешить сайт';
    button.onclick=async()=>{
      try {
        if(enabled) {await chrome.permissions.remove({origins:p.origins});await chrome.storage.local.set({enabled:s.enabled.filter(x=>x!==id)});}
        else if(await chrome.permissions.request({origins:p.origins})) {await chrome.storage.local.set({enabled:[...s.enabled,id]});}
        await render();
      } catch {status.textContent='Разрешение не изменено.';}
    };row.append(name,button);root.append(row);
  }
}
document.querySelector('#scan').onchange=async e=>{await chrome.storage.local.set({scan:e.target.checked});status.textContent=e.target.checked?'Проверка включена только для разрешённых сайтов.':'Проверка остановлена.';};
render();

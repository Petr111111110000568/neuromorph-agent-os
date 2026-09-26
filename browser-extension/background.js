import {PROVIDERS,providerFor,localPage,validTask,reserveAllowed} from './policy.mjs';
let busy=false;
const fail=message=>{throw new Error(message);};
const now=()=>new Date().toISOString();

// This function is serialized by scripting.executeScript into an isolated world.
// It only sees rendered controls. No cookies, tokens, hidden state or provider APIs.
async function pageAdapter(action, prompt, expectedURL) {
  if(location.href!==expectedURL || window!==window.top) return {ok:false,error:'Адрес страницы изменился. Действие отменено.'};
  const visible=e=>!!(e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
  const composers=[...document.querySelectorAll('textarea,[contenteditable="true"][role="textbox"],[contenteditable="true"]')].filter(e=>visible(e) && !e.disabled && e.getAttribute('aria-disabled')!=='true' && !e.closest('[hidden]'));
  const unique=composers.filter(e=>!composers.some(other=>other!==e && other.contains(e)));
  const body=(document.body?.innerText||'').slice(0,120000);
  const challenged=/captcha|verify you are human|провер.{0,12}(робот|человек)|滑块验证|人机验证/i.test(body);
  const limited=/rate limit|usage limit|too many requests|лимит.{0,35}(достиг|исчерпан)|达到.{0,15}上限/i.test(body);
  const responses=location.hostname==='chat.deepseek.com' ? [...document.querySelectorAll('.ds-assistant-message-main-content')].filter(visible) : [];
  if(action==='capture') {
    if(challenged || limited) return {blocked:true};
    const buttons=[...document.querySelectorAll('button,[role="button"]')].filter(visible);
    const stopping=buttons.some(e=>/^(stop|stop generating|остановить|остановить генерацию|停止生成)$/i.test((e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||'').trim()));
    const text=responses.at(-1)?.innerText.trim()||'';
    return {count:responses.length,text:text.length<=10000?text:'',stopping,observed_at:new Date().toISOString()};
  }
  if(action==='probe') {
    const login=[...document.querySelectorAll('button,a')].some(e=>visible(e)&&/^(sign in|log in|войти|登录)$/i.test(e.innerText.trim()));
    return {observation:challenged?'challenge_visible':limited?'limit_visible':login?'sign_in_control_visible':unique.length===1?'composer_visible':'unknown',authenticated:'not_verified',observed_at:new Date().toISOString()};
  }
  if(action==='selection') {
    const text=window.getSelection()?.toString().trim()||'';
    if(!text || text.length>10000) return {ok:false,error:'Выделите только ответ ассистента во вкладке (1–10000 символов).'};
    return {ok:true,text,verification:'user_selected_unverified',observed_at:new Date().toISOString()};
  }
  if(challenged || limited) return {ok:false,error:'Обнаружено ограничение или проверка. Автоматические повторы запрещены.'};
  if(unique.length!==1) return {ok:false,error:'Нет единственного видимого поля ввода. Перенесите вопрос вручную.'};
  const input=unique[0];
  if((input.value??input.innerText??'').trim()) return {ok:false,error:'В чате уже есть черновик. Он сохранён; отправка не выполнена.'};
  input.focus();
  if(input.tagName==='TEXTAREA') {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,prompt);
    input.dispatchEvent(new Event('input',{bubbles:true}));
  } else {
    input.textContent=prompt;
    input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:prompt}));
  }
  await new Promise(resolve=>setTimeout(resolve,300));
  if(location.href!==expectedURL) return {ok:false,error:'Адрес изменился после подготовки черновика. Отправка отменена.',draft_prepared:true};
  if((input.value??input.innerText??'').trim()!==prompt.trim()) return {ok:false,error:'Редактор не принял текст точно. Проверьте черновик вручную.',draft_prepared:true};
  let sends=[...document.querySelectorAll('button,[role="button"]')].filter(e=>visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true').filter(e=>{
    const label=(e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||'').trim();
    return /^(send|send message|send prompt|отправить|отправить сообщение|发送|发送消息)$/i.test(label);
  });
  // Structural selector observed in the actual DeepSeek UI, without hashed classes.
  if(!sends.length && location.hostname==='chat.deepseek.com') sends=[...document.querySelectorAll('[role="button"].ds-button--primary.ds-button--circle')].filter(e=>visible(e)&&!e.classList.contains('ds-button--disabled')&&e.getAttribute('aria-disabled')!=='true');
  if(sends.length!==1) return {ok:false,error:'Текст подготовлен. Не найдено однозначной кнопки отправки — нажмите её в чате самостоятельно.',draft_prepared:true};
  sends[0].click();
  return {ok:true,status:'send_clicked_unconfirmed',baseline_responses:responses.length,draft_prepared:true,notice:'Кнопка нажата один раз. Приём сервисом, модель и завершение ответа ещё не подтверждены.'};
}
async function settings() {return (await chrome.storage.local.get({enabled:[],scan:false}));}
async function permitted(tab,provider) {
  const s=await settings();
  return tab && providerFor(tab.url)===provider && s.enabled.includes(provider) && await chrome.permissions.contains({origins:PROVIDERS[provider].origins});
}
async function inject(tab,provider,action,prompt) {
  const current=await chrome.tabs.get(tab.id);
  if(current.url!==tab.url || !await permitted(current,provider)) fail('Вкладка изменилась или доступ не разрешён.');
  const result=await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},func:pageAdapter,args:[action,prompt||'',tab.url]});
  const after=await chrome.tabs.get(tab.id);
  if(after.url!==tab.url) fail('Адрес изменился во время операции. Результат не подтверждён; повтор не выполнен.');
  return result[0]?.result;
}
async function scan() {
  const s=await settings();
  if(!s.scan) return [];
  const candidates=[];
  for(const tab of await chrome.tabs.query({})) {
    const provider=providerFor(tab.url);
    if(!provider || !await permitted(tab,provider)) continue;
    let observation={observation:'unavailable',authenticated:'not_verified',observed_at:now()};
    try {observation=await inject(tab,provider,'probe');} catch {}
    candidates.push({tab_id:tab.id,provider,url:tab.url,...observation});
  }
  await chrome.storage.session.set({candidates:candidates.slice(0,40),scanned_at:now()});
  return candidates;
}
async function command(m,sender) {
  if(!sender.tab || sender.frameId!==0 || !localPage(sender.url)) fail('Недопустимый источник команды.');
  const p=m.payload||{};
  if(m.action==='status') return {ok:true,...await settings(),...await chrome.storage.session.get(['candidates','scanned_at']),...await chrome.storage.local.get(['pending','history'])};
  if(m.action==='scan') return {ok:true,candidates:await scan()};
  if(m.action==='resolve') {
    const {pending}=await chrome.storage.local.get('pending');
    if(!pending || pending.task_id!==p.task_id || p.confirmed!==true) fail('Укажите проверенную незавершённую задачу.');
    await chrome.storage.local.remove('pending');
    await chrome.storage.session.remove('capture');
    return {ok:true};
  }
  if(!Number.isInteger(p.tab_id) || !validTask(p.task_id)) fail('Некорректная задача или вкладка.');
  const tab=await chrome.tabs.get(p.tab_id), provider=providerFor(tab.url);
  if(!provider || p.provider!==provider || tab.url!==p.url || !await permitted(tab,provider)) fail('Сервис, адрес или разрешение не совпадают.');
  if(m.action==='selection') return {task_id:p.task_id,provider,chat_url:tab.url,...await inject(tab,provider,'selection')};
  if(m.action==='capture') {
    const {pending}=await chrome.storage.local.get('pending');
    if(!pending || pending.task_id!==p.task_id || pending.tab_id!==p.tab_id || pending.chat_url!==p.url) fail('Нет соответствующей ожидающей задачи.');
    if(provider!=='deepseek') return {ok:true,status:'manual_import_required'};
    if(Date.now()-pending.reserved_at>300000) return {ok:true,status:'capture_timeout_unconfirmed'};
    if(pending.status!=='send_clicked_unconfirmed') return {ok:true,status:'manual_import_required'};
    const observed=await inject(tab,provider,'capture');
    if(observed?.blocked) return {ok:true,status:'provider_blocked'};
    if(!observed?.text || observed.count!==pending.baseline_responses+1 || observed.stopping) return {ok:true,status:'waiting_unconfirmed'};
    const {capture}=await chrome.storage.session.get('capture');
    const same=capture?.task_id===p.task_id && observed.text===capture?.text;
    const stableSince=same?capture.since:Date.now();
    await chrome.storage.session.set({capture:{task_id:p.task_id,text:observed.text,since:stableSince}});
    return same && Date.now()-stableSince>=8000 ? {ok:true,status:'captured_unconfirmed',text:observed.text,task_id:p.task_id,provider,chat_url:tab.url,observed_at:now(),verification:'candidate_needs_human_confirmation'} : {ok:true,status:'waiting_unconfirmed'};
  }
  if(m.action!=='dispatch' || typeof p.prompt!=='string' || !p.prompt.trim() || p.prompt.length>8000) fail('Некорректный запрос.');
  if(busy) fail('Другая операция уже выполняется.');
  busy=true;
  try {
    const {pending}=await chrome.storage.local.get('pending');
    const {history=[]}=await chrome.storage.local.get('history');
    if(pending) fail('Предыдущая отправка требует проверки. Автоматического повтора нет.');
    if(history.some(x=>x.task_id===p.task_id)) fail('Этот task ID уже использован. Повторная отправка запрещена.');
    if(!reserveAllowed(history,provider)) fail('Локальная граница: 4 отправки/24 ч на сервис и минимум 60 с между отправками.');
    const receipt={task_id:p.task_id,provider,chat_url:tab.url,tab_id:tab.id,reserved_at:Date.now(),created_at:now(),status:'reserved_unconfirmed'};
    // Reserve before interacting. Worker termination leaves a pending fence.
    await chrome.storage.local.set({pending:receipt,history:[...history.filter(x=>x.reserved_at>Date.now()-172800000),receipt].slice(-100)});
    const result=await inject(tab,provider,'dispatch',p.prompt);
    const final={...receipt,...result,updated_at:now()};
    await chrome.storage.local.set({pending:final});
    return final;
  } finally {busy=false;}
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  command(message,sender).then(reply).catch(error=>reply({ok:false,error:error.message}));
  return true;
});
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='candidate-scan') scan().catch(()=>{});});
chrome.runtime.onInstalled.addListener(()=>chrome.alarms.create('candidate-scan',{periodInMinutes:1}));
chrome.runtime.onStartup.addListener(()=>chrome.alarms.create('candidate-scan',{periodInMinutes:1}));

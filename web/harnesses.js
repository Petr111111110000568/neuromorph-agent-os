(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let latest = null, busy = false, enabled = false;
  const labels = {completed: 'Ответ получен', blocked_model_calls: 'Вызовы модели выключены', blocked_provider_missing: 'Нет ключа провайдера', not_installed: 'Не установлен', invalid_response: 'Некорректный ответ', timeout: 'Превышен срок'};
  function node(tag, text, cls) { const n = document.createElement(tag); n.textContent = text; if(cls) n.className = cls; return n; }
  async function api(path, body) {
    const r = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json(); if(!r.ok) throw new Error(d.error?.message || `HTTP ${r.status}`); return d;
  }
  function error(e) { $('error').hidden = !e; $('error').textContent = e ? String(e.message || e) : ''; }
  function show(result) {
    latest = result;
    $('run-status').textContent = `${result.harness_id || ''}: ${labels[result.status] || result.status || 'Статус не указан'}`;
    $('answer').textContent = result.output_text || result.reason || 'Ответ модели отсутствует. Проверьте данные запуска и настройку исполнителя.';
    $('raw').textContent = JSON.stringify(result,null,2); $('download').disabled = false;
  }
  async function refresh() {
    error(null);
    try {
      const [status, runs] = await Promise.all([api('/api/harnesses'), api('/api/harnesses/runs')]);
      enabled = status.model_calls_enabled === true;
      $('permission').textContent = enabled ? 'Вызовы модели разрешены на сервере; для запуска нужны установленный исполнитель и ключ провайдера.' : 'Вызовы модели выключены. Для подключения задайте ключ провайдера и AUTONOMY_ALLOW_MODEL_CALLS=true в окружении сервера, затем перезапустите его.';
      $('cards').replaceChildren();
      (status.harnesses || []).forEach(h => {
        const c = node('article','','panel'); c.append(node('h2',h.name || h.id));
        c.append(node('p', h.pin_verified ? 'Установлен · контрольная сумма совпадает' : h.detected ? 'Обнаружен · требуется проверка версии' : 'Не установлен'));
        c.append(node('p', h.protocol_tested ? 'Протокол проверен в этом окружении' : 'Живой протокол в этом окружении не подтверждён','form-help'));
        c.append(node('p', h.live_authenticated ? 'Авторизованный вызов подтверждён' : 'Авторизованный вызов не подтверждён','form-help'));
        if(h.upstream_url && /^https:\/\/github\.com\//.test(h.upstream_url)) { const a=node('a','Официальный исходный код ↗');a.href=h.upstream_url;a.target='_blank';a.rel='noopener noreferrer';c.append(a); }
        $('cards').append(c);
      });
      $('history').replaceChildren();
      const items = (runs.items || []).slice(0,50);
      if(!items.length) $('history').append(node('p','Сохранённых задач пока нет.','muted'));
      items.forEach(r => { const b=node('button',`${r.created_at || ''} · ${r.harness_id} · ${labels[r.status] || r.status}`,'text-button');b.type='button';b.addEventListener('click',()=>show(r));$('history').append(b); });
      $('fields').disabled = busy || !enabled;
    } catch(e) { enabled=false;$('fields').disabled=true;error(e); }
  }
  $('task-form').addEventListener('submit',async e=>{
    e.preventDefault();if(busy || !enabled)return;busy=true;$('fields').disabled=true;error(null);
    $('run-status').textContent='Исполнитель обрабатывает задачу…';
    try { show(await api('/api/harnesses/run',{harness_id:$('harness').value,prompt:$('prompt').value,provider:'openai',model:$('model').value,timeout_seconds:Number($('deadline').value)})); }
    catch(err){error(err);$('run-status').textContent='Запрос не завершён.';}
    finally{busy=false;await refresh();}
  });
  $('download').addEventListener('click',()=>{if(!latest)return;const a=document.createElement('a');const url=URL.createObjectURL(new Blob([JSON.stringify(latest,null,2)],{type:'application/json'}));a.href=url;a.download='meta-harness-executor-result.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  $('refresh').addEventListener('click',refresh);refresh();
})();

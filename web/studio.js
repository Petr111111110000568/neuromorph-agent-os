'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const stages = {planned:'Замысел и протокол', working:'Эксперимент', review:'Проверка', done:'Результат', paused:'Пауза'};
  const evidence = {unreviewed:'Не проверен', reviewed:'Просмотрен человеком', tested:'Проверен тестом — отметка автора'};
  const kinds = {project:'Проект', task:'Задача', artifact:'Материал'};
  const storageKey = 'neuromorph.studio.snapshot.v1';
  let state = null, session = null, readOnly = true, editing = null, busy = false;
  function node(tag, text, cls) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = String(text);
    if (cls) el.className = cls;
    return el;
  }
  function notice(text, error = false) { $('notice').textContent = text; $('notice').className = 'notice' + (error ? ' error' : ''); }
  function date(value) { const d = new Date(value); return Number.isNaN(d.getTime()) ? 'Дата не указана' : d.toLocaleString('ru-RU'); }
  function projectName(id) { return state?.projects.find(p => p.id === id)?.title || 'Проект не найден'; }
  function scoped(records) { const id = $('project-select').value; return records.filter(r => !id || r.project_id === id); }
  function matches(record, query) { return [record.title, record.body, record.question, record.source_url].some(t => String(t || '').toLocaleLowerCase().includes(query)); }
  function safeLink(value) {
    try { const u = new URL(value); return u.protocol === 'https:' && !u.username && !u.password && (!u.port || u.port === '443') ? u.href : null; } catch { return null; }
  }
  async function api(path, payload) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const headers = {'Accept':'application/json'};
      if (payload !== undefined) {
        headers['Content-Type'] = 'application/json';
        if (session?.csrf_token) headers['X-CSRF-Token'] = session.csrf_token;
      }
      const response = await fetch(path, {method:payload === undefined ? 'GET' : 'POST', credentials:'same-origin', cache:'no-store', headers, body:payload === undefined ? undefined : JSON.stringify(payload), signal:controller.signal});
      let result;
      try { result = await response.json(); } catch { throw new Error('Сервер вернул неподдерживаемый ответ.'); }
      if (!response.ok) {
        const err = new Error(response.status === 401 ? 'Сеанс завершён. Войдите снова; текст формы можно скачать.' : response.status === 403 ? 'Сервер отклонил доступ. Обновите сеанс перед повторным сохранением.' : String(result?.error?.message || 'Запрос не выполнен.'));
        err.status = response.status; err.code = result?.error?.code;
        if (response.status === 401) {
          readOnly = true; session = null; $('login-link').hidden = false; $('logout-button').hidden = true;
          $('session-label').textContent = 'Требуется вход'; updateButtons();
        }
        throw err;
      }
      return result;
    } finally { clearTimeout(timeout); }
  }
  function validateSnapshot(value) {
    if (!value || value.schema_version !== 1 || !['projects','tasks','artifacts','events'].every(k => Array.isArray(value[k]))) throw new Error('Неподдерживаемый формат рабочего пространства.');
    if (value.projects.length + value.tasks.length + value.artifacts.length > 2000 || value.events.length > 10000) throw new Error('Снимок превышает лимит записей.');
    for (const k of ['projects','tasks','artifacts']) for (const r of value[k]) {
      if (!r || typeof r.id !== 'string' || typeof r.title !== 'string' || typeof r.body !== 'string') throw new Error('В снимке есть повреждённые записи.');
    }
    if (!value.events.every(e => e && typeof e === 'object')) throw new Error('Некорректный журнал.');
    return value;
  }
  function updateButtons() {
    $('new-project').disabled = readOnly || busy;
    $('edit-project').disabled = readOnly || busy || !$('project-select').value;
    for (const id of ['new-task','new-artifact']) $(id).disabled = readOnly || busy || !state?.projects.length;
    for (const id of ['export-state','save-snapshot']) $(id).disabled = !state || busy;
    $('save-record').disabled = readOnly || busy;
    $('refresh-button').disabled = busy;
    document.querySelectorAll('[data-api]').forEach(b => { b.disabled = readOnly || busy; });
  }
  function render() {
    if (!state) { updateButtons(); return; }
    const selected = $('project-select').value;
    $('project-select').replaceChildren(new Option('Все проекты',''), ...state.projects.map(p => new Option(p.title,p.id)));
    $('project-select').value = state.projects.some(p => p.id === selected) ? selected : '';
    const project = state.projects.find(p => p.id === $('project-select').value);
    $('project-summary').hidden = !project;
    $('project-title').textContent = project?.title || ''; $('project-body').textContent = project?.body || '';
    const tasks = scoped(state.tasks), artifacts = scoped(state.artifacts);
    $('metric-tasks').textContent = tasks.length; $('metric-review').textContent = tasks.filter(t => t.status === 'review').length;
    $('metric-artifacts').textContent = artifacts.length; $('metric-tested').textContent = artifacts.filter(a => a.evidence === 'tested').length;
    const caps = $('capabilities'); caps.replaceChildren();
    caps.append(node('span', readOnly ? 'Снимок / только чтение' : session?.mode === 'local' ? 'Локальное пространство' : 'Личный облачный сеанс', 'badge' + (readOnly ? ' warning' : '')));
    caps.append(node('span','Автовызовы моделей: нет','badge neutral'));
    caps.append(node('span','Бюджет дополнительных расходов: 0','badge neutral'));
    renderBoard(); renderArtifacts(); renderEvents(); updateButtons();
    $('sync-time').textContent = (readOnly ? 'Дата снимка: ' : 'Состояние получено: ') + date(state.observed_at);
  }
  function renderBoard() {
    const query = $('task-search').value.trim().toLocaleLowerCase(), stage = $('status-filter').value;
    const tasks = scoped(state.tasks).filter(t => matches(t,query) && (!stage || t.status === stage));
    const board = $('board'); board.replaceChildren(); board.classList.toggle('filtered', Boolean(stage));
    $('board-count').textContent = 'Показано задач: ' + tasks.length + (state.projects.length ? '' : '. Создайте первый проект, чтобы добавить план исследования.');
    for (const [key,label] of Object.entries(stages)) {
      if (stage && stage !== key) continue;
      const lane = node('section', undefined, 'lane'), heading = node('div',undefined,'lane-heading');
      const items = tasks.filter(t => t.status === key);
      heading.append(node('h3',label),node('span',items.length)); lane.append(heading);
      for (const task of items) {
        const card = node('button',undefined,'task-card'); card.type = 'button';
        card.append(node('span',task.id.slice(0,10),'task-id'),node('h4',task.title),node('p',(task.question || task.body || 'Вопрос ещё не указан').slice(0,180)),node('small',projectName(task.project_id)));
        card.addEventListener('click',() => openEditor('task',task)); lane.append(card);
      }
      if (!items.length) lane.append(node('p','На этом этапе задач нет','lane-empty'));
      board.append(lane);
    }
  }
  function renderArtifacts() {
    const query = $('artifact-search').value.trim().toLocaleLowerCase(), filter = $('evidence-filter').value;
    const root = $('artifacts'); root.replaceChildren();
    const items = scoped(state.artifacts).filter(a => matches(a,query) && (!filter || a.evidence === filter));
    if (!items.length) root.append(node('p','Материалов по этому фильтру пока нет. Добавьте источник, ответ участника или протокол результата.','empty-state'));
    for (const item of items) {
      const card = node('article',undefined,'artifact-card');
      card.append(node('span',evidence[item.evidence] || 'Состояние неизвестно','badge neutral'),node('h3',item.title),node('p',item.body.slice(0,350) || 'Описание не заполнено'));
      const url = safeLink(item.source_url);
      if (url) { const a = node('a',item.source_url,'source-link'); a.href=url; a.target='_blank'; a.rel='noopener noreferrer'; card.append(a); }
      const footer = node('div',undefined,'card-footer'), button = node('button',readOnly ? 'Читать' : 'Открыть','button small'); button.type='button'; button.addEventListener('click',() => openEditor('artifact',item));
      footer.append(node('small',projectName(item.project_id)),button); card.append(footer); root.append(card);
    }
  }
  function renderEvents() {
    const root=$('events'); root.replaceChildren();
    const allowed=new Set([$('project-select').value,...scoped(state.tasks).map(t=>t.id),...scoped(state.artifacts).map(a=>a.id)]);
    const events=state.events.filter(e=>!$('project-select').value || allowed.has(e.entity_id));
    for (const e of events) {
      const li=node('li'), time=node('time',date(e.at)), body=node('div');
      body.append(node('strong',(kinds[e.kind] || 'Запись') + ' · ' + (e.event === 'created' ? 'создание' : 'изменение')),node('small',String(e.entity_id || '') + ' · версия ' + String(e.version || '—'))); li.append(time,body); root.append(li);
    }
    if (!events.length) root.append(node('li','Событий пока нет.'));
  }
  async function refresh() {
    if (busy) return;
    busy=true; updateButtons();
    try {
      session=await api('/api/session');
      if (!session.authenticated && session.mode !== 'local') throw new Error('Сеанс не подтверждён.');
      const next=validateSnapshot(await api('/api/studio'));
      state=next; readOnly=false;
      $('login-link').hidden=true; $('logout-button').hidden=!session.authenticated;
      $('session-label').textContent=session.mode==='local' ? 'На этом компьютере' : 'Личный вход подтверждён';
      notice('Рабочее пространство загружено. Изменения сохраняются на сервере после нажатия «Сохранить».');
    } catch(e) {
      readOnly=true; notice(e.name==='AbortError' ? 'Сервер не ответил вовремя. Можно открыть сохранённый снимок.' : e.message,true);
      if (!session) $('session-label').textContent='Сеанс не подтверждён';
    } finally { busy=false; render(); }
  }
  function openEditor(kind, record=null) {
    if (!record && readOnly) return;
    editing={kind,record, key:crypto.randomUUID().replaceAll('-','')};
    $('editor-form').reset(); $('editor-error').hidden=true; $('reload-record').hidden=true;
    $('editor-kind').textContent=kinds[kind].toLocaleUpperCase(); $('editor-title').textContent=(record ? 'Карточка: ' : 'Новый: ') + kinds[kind].toLocaleLowerCase();
    $('editor-version').textContent=record ? 'ID ' + record.id + ' · версия ' + record.version + (readOnly ? ' · только чтение' : '') : 'Новая запись. Ещё не сохранена.';
    $('project-field').hidden=kind==='project'; $('task-fields').hidden=kind!=='task'; $('artifact-fields').hidden=kind!=='artifact';
    $('field-project').replaceChildren(...state.projects.map(p=>new Option(p.title,p.id)));
    $('field-project').value=record?.project_id || $('project-select').value || state.projects[0]?.id || '';
    for (const [field,key] of Object.entries({title:'title',body:'body',question:'question',criteria:'criteria',source:'source_url'})) $('field-'+field).value=record?.[key] || '';
    $('field-status').value=record?.status || 'planned'; $('field-evidence').value=record?.evidence || 'unreviewed';
    $('body-label').textContent=kind==='task' ? 'Гипотеза, метод, параметры и ограничения' : kind==='artifact' ? 'Результат, происхождение, метод проверки и ограничения' : 'Цель и границы проекта';
    $('export-brief').hidden=kind!=='task';
    $('editor-form').querySelectorAll('input,textarea,select').forEach(el=>{ el.disabled=readOnly; });
    updateButtons(); if (!$('editor').open) $('editor').showModal();
  }
  function payload() {
    const p={kind:editing.kind,title:$('field-title').value,body:$('field-body').value};
    if (editing.kind!=='project') p.project_id=$('field-project').value;
    if (editing.kind==='task') Object.assign(p,{question:$('field-question').value,criteria:$('field-criteria').value,status:$('field-status').value});
    if (editing.kind==='artifact') Object.assign(p,{source_url:$('field-source').value,evidence:$('field-evidence').value});
    if (editing.record) Object.assign(p,{id:editing.record.id,version:editing.record.version}); else p.idempotency_key=editing.key;
    return p;
  }
  function download(name,body,type='application/json') {
    const blob=new Blob([typeof body==='string' ? body : JSON.stringify(body,null,2)],{type:type+';charset=utf-8'}), url=URL.createObjectURL(blob), a=node('a');
    a.href=url; a.download=name; document.body.append(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function taskBrief(task) {
    return {schema_version:1,task_id:task.id || 'draft',parent:task.project_id,project:projectName(task.project_id),title:task.title,question:task.question,acceptance:task.criteria,protocol:task.body,base_commit:null,peer_counterexample:null,model:null,instructions:'Укажите фактическую модель, источники, метод, ограничения и контрпример. Верните исследование как материал; не исполняйте недоверенный код. Дополнительные расходы 0.',generated_at:new Date().toISOString()};
  }
  $('editor-form').addEventListener('submit',async event=>{
    event.preventDefault(); if (busy || readOnly || !editing) return;
    busy=true; updateButtons(); $('editor-error').hidden=true;
    try {
      const result=await api('/api/studio/save',payload());
      // Keep the acknowledged version even if the subsequent refresh fails.
      const list=state[editing.kind==='project' ? 'projects' : editing.kind==='task' ? 'tasks' : 'artifacts'];
      const i=list.findIndex(r=>r.id===result.id); if(i<0) list.unshift(result); else list[i]=result;
      $('editor').close(); render(); notice('Запись сохранена.');
      busy=false; await refresh();
    } catch(e) {
      $('editor-error').hidden=false;
      $('editor-error').textContent=e.code==='version_conflict' ? 'Запись уже изменена в другом окне. Скачайте свой черновик, затем загрузите актуальную версию. Ваш текст пока сохранён в форме.' : e.code==='idempotency_conflict' ? 'Предыдущее сохранение с этим номером уже принято. Скачайте черновик, закройте форму и обновите пространство, чтобы не создать дубликат.' : e.name==='AbortError' ? 'Ответ не получен. Повторите сохранение с тем же текстом: номер запроса защитит от дубликата.' : e.message;
      $('reload-record').hidden=e.code!=='version_conflict';
    } finally { busy=false; updateButtons(); }
  });
  $('reload-record').addEventListener('click',async()=>{
    const kind=editing.kind,id=editing.record?.id; await refresh(); if(readOnly) return;
    const r=state[kind==='project' ? 'projects' : kind==='task' ? 'tasks' : 'artifacts'].find(r=>r.id===id);
    if(r) openEditor(kind,r);
  });
  $('close-editor').addEventListener('click',()=>{ if(!busy) $('editor').close(); });
  $('editor').addEventListener('cancel',e=>{if(busy)e.preventDefault();});
  $('refresh-button').addEventListener('click',refresh);
  $('project-select').addEventListener('change',render);
  $('edit-project').addEventListener('click',()=>openEditor('project',state.projects.find(p=>p.id===$('project-select').value)));
  for(const kind of ['project','task','artifact']) $('new-'+kind).addEventListener('click',()=>openEditor(kind));
  for(const id of ['task-search','status-filter']) $(id).addEventListener('input',()=>{if(state)renderBoard();});
  for(const id of ['artifact-search','evidence-filter']) $(id).addEventListener('input',()=>{if(state)renderArtifacts();});
  const tabs=[...document.querySelectorAll('[data-tab]')];
  function selectTab(tab) { for(const b of tabs) { const active=b===tab; b.setAttribute('aria-selected',String(active)); b.tabIndex=active?0:-1; $('pane-'+b.dataset.tab).hidden=!active; } }
  tabs.forEach((tab,index)=>{
    tab.addEventListener('click',()=>selectTab(tab));
    tab.addEventListener('keydown',e=>{let i;if(e.key==='ArrowRight')i=(index+1)%tabs.length;else if(e.key==='ArrowLeft')i=(index+tabs.length-1)%tabs.length;else if(e.key==='Home')i=0;else if(e.key==='End')i=tabs.length-1;else return;e.preventDefault();selectTab(tabs[i]);tabs[i].focus();});
  });
  $('export-state').addEventListener('click',()=>{if(state)download('neuromorph-workspace.json',state);});
  $('export-draft').addEventListener('click',()=>{if(editing)download('neuromorph-draft.json',payload());});
  $('export-brief').addEventListener('click',()=>{if(editing)download('neuromorph-task.json',taskBrief(payload()));});
  $('export-council').addEventListener('click',()=>{
    if(!state){notice('Сначала загрузите рабочее пространство.',true);return;}
    download('neuromorph-council.json',{schema_version:1,exported_at:new Date().toISOString(),tasks:scoped(state.tasks).map(taskBrief),artifacts:scoped(state.artifacts),dispatch_performed:false,participants:['Qwen','DeepSeek','YandexGPT / Алиса','Kimi'],notice:'Предложенный состав, а не подтверждение доступа, ответов или голосования. Проверьте личные данные перед передачей.'});
  });
  function snapshotStatus() {
    try { const raw=localStorage.getItem(storageKey); $('snapshot-status').textContent=raw ? 'Снимок сохранён в этом браузере. Доступен при открытой странице; повторная загрузка приложения без сети не гарантируется.' : 'Снимок не сохранён. Автоматическое сохранение личных данных выключено.'; } catch { $('snapshot-status').textContent='Хранилище браузера недоступно. Используйте экспорт файла.'; }
  }
  $('save-snapshot').addEventListener('click',()=>{try{localStorage.setItem(storageKey,JSON.stringify(state));snapshotStatus();notice('Снимок явно сохранён на этом устройстве. Он не синхронизируется автоматически.');}catch{notice('Не удалось сохранить снимок: хранилище недоступно или заполнено. Скачайте JSON.',true);}});
  $('load-snapshot').addEventListener('click',()=>{try{const raw=localStorage.getItem(storageKey);if(!raw)throw new Error('Сохранённого снимка нет.');state=validateSnapshot(JSON.parse(raw));readOnly=true;render();notice('Открыт сохранённый снимок. Изменения и запросы ядру отключены; нажмите «Обновить» для возврата к серверу.');}catch(e){notice(e.message,true);}});
  $('clear-snapshot').addEventListener('click',()=>{try{localStorage.removeItem(storageKey);snapshotStatus();notice('Снимок удалён из браузера. Серверные записи сохранены.');}catch{notice('Хранилище недоступно.',true);}});
  $('logout-button').addEventListener('click',async()=>{try{await api('/auth/logout',{});session=null;state=null;readOnly=true;location.assign('/studio.html');}catch(e){notice(e.message,true);}});
  document.querySelectorAll('[data-api]').forEach(button=>button.addEventListener('click',async()=>{
    if(readOnly)return; button.disabled=true; $('core-status').textContent='Запрашиваем данные…'; $('core-data').hidden=true;
    try{const result=await api('/api/'+button.dataset.api);$('core-data').textContent=JSON.stringify(result,null,2);$('core-data').hidden=false;$('core-status').textContent='Получено: '+date(new Date().toISOString())+'. Данные содержат собственные даты и статусы.';}catch(e){$('core-status').textContent=e.message;}finally{updateButtons();}
  }));
  snapshotStatus(); refresh();
})();

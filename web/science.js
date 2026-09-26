'use strict';
const ScienceProtocol = (() => {
  const limits = {title:200, question:4000, hypothesis:2000, method:2000, control:2000, criteria:4000};
  function safeURL(value) {
    try { const u = new URL(value); return u.protocol === 'https:' && !u.username && !u.password && (!u.port || u.port === '443') ? u.href : null; } catch { return null; }
  }
  function build(values, catalog, projects) {
    const result = {};
    for (const [key, maximum] of Object.entries(limits)) {
      const value = typeof values[key] === 'string' ? values[key].trim() : '';
      if (!value || value.length > maximum || value.includes('\0')) throw new Error('Заполните все поля протокола и соблюдайте ограничение длины.');
      result[key] = value;
    }
    if (!projects.some(p => p.id === values.project_id)) throw new Error('Выберите существующий проект.');
    if (!catalog.templates.some(t => t.id === values.template_id)) throw new Error('Выберите рабочий процесс из каталога.');
    if (!catalog.items.some(p => p.id === values.platform_id)) throw new Error('Выберите платформу из каталога.');
    return {project_id:values.project_id, template_id:values.template_id, platform_id:values.platform_id, ...result};
  }
  // One immutable request survives ambiguous network failures. Editing never changes its identity.
  class Submission {
    constructor() { this.pending = null; this.completed = null; }
    capture(payload, keyFactory) {
      if (this.completed) throw new Error('Этот протокол уже сохранён. Начните новый протокол.');
      const canonical = JSON.stringify(payload);
      if (this.pending && this.pending.canonical !== canonical) throw new Error('Сначала подтвердите сохранение предыдущего протокола повтором того же запроса.');
      if (!this.pending) this.pending = Object.freeze({canonical, payload:Object.freeze({...payload, idempotency_key:keyFactory()})});
      return this.pending.payload;
    }
    acknowledge(record) {
      if (!this.pending || !record || typeof record.id !== 'string' || !record.id || !Number.isInteger(record.version) || record.version < 1 || record.project_id !== this.pending.payload.project_id || !['planned','working','review','done','paused'].includes(record.status) || (record.kind !== undefined && record.kind !== 'task')) throw new Error('Нет подтверждения сохранения задачи. Повторите тот же запрос.');
      this.completed = Object.freeze({...record});
      return this.completed;
    }
    rejectValidation(status) {
      if (![400, 422].includes(status) || this.completed) return false;
      this.pending = null;
      return true;
    }
    reset() {
      if (this.pending && !this.completed) throw new Error('Результат предыдущего сохранения пока неизвестен. Сначала повторите запрос.');
      this.pending = null; this.completed = null;
    }
  }
  return {safeURL, build, Submission};
})();
if (typeof module !== 'undefined' && module.exports) module.exports = ScienceProtocol;

if (typeof document !== 'undefined') (() => {
  const $ = id => document.getElementById(id);
  const submission = new ScienceProtocol.Submission();
  let catalog = {items:[], templates:[]}, projects = [], session = null, ready = false, busy = false;
  const statuses = {reviewed:'Изучено', installed:'Установлено', tested:'Проверено тестом', validated:'Валидировано', candidate:'Кандидат', research_only:'Исследовательский обзор', not_connected:'Не подключено', unavailable:'Недоступно', requires_setup:'Требуется настройка'};
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = String(text); if (cls) n.className = cls; return n; };
  const asText = value => Array.isArray(value) ? value.join('; ') : value && typeof value === 'object' ? JSON.stringify(value) : String(value ?? 'Не указано');
  const date = value => { const d = new Date(value); return value && !Number.isNaN(d.getTime()) ? d.toLocaleDateString('ru-RU') : 'не указана'; };
  function notice(id, message, error = false) { $(id).textContent = message; $(id).className = 'notice' + (error ? ' error' : ''); }
  function canWrite() { return ready && session && (session.mode === 'local' || session.authenticated === true); }
  function buttons() {
    $('protocol-fields').disabled = !canWrite() || busy || !!submission.pending;
    $('save').disabled = !canWrite() || busy || !!submission.completed || !projects.length;
    $('save').textContent = submission.completed ? 'Задача сохранена' : submission.pending ? 'Подтвердить сохранение повтором' : 'Сохранить задачу';
    $('refresh').disabled = busy;
    $('new-protocol').hidden = !submission.completed;
    $('new-protocol').disabled = busy;
    document.querySelectorAll('[data-platform]').forEach(b => { b.disabled = !canWrite() || busy || !!submission.pending; });
  }
  async function api(path, payload) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
    try {
      const headers = {Accept:'application/json'};
      if (payload) { headers['Content-Type'] = 'application/json'; if (session?.csrf_token) headers['X-CSRF-Token'] = session.csrf_token; }
      const response = await fetch(path, {method:payload ? 'POST' : 'GET', credentials:'same-origin', cache:'no-store', headers, body:payload ? JSON.stringify(payload) : undefined, signal:controller.signal});
      if (response.status === 401) { session = null; $('login-link').hidden = false; $('session-label').textContent = 'Требуется вход'; buttons(); }
      let result; try { result = await response.json(); } catch { throw new Error('Не удалось прочитать ответ сервера. Сохранение, если оно отправлено, требует подтверждения повтором.'); }
      if (!response.ok) {
        const error = new Error(response.status === 401 ? 'Сеанс завершён. Войдите, затем обновите страницу данных кнопкой «Обновить». Черновик можно скачать.' : response.status === 403 ? 'Сервер отклонил доступ. Обновите сеанс перед повтором.' : asText(result?.error?.message || result?.error || 'Запрос отклонён сервером.'));
        error.status = response.status; throw error;
      }
      return result;
    } finally { clearTimeout(timer); }
  }
  function validateCatalog(value) {
    if (!value || value.schema_version !== 1 || !Array.isArray(value.items) || !Array.isArray(value.templates) || value.items.length > 500 || value.templates.length > 50) throw new Error('Каталог имеет неподдерживаемый формат.');
    if (!value.items.every(i => i && typeof i.id === 'string' && typeof i.name === 'string') || !value.templates.every(t => t && typeof t.id === 'string' && typeof t.name === 'string' && Array.isArray(t.steps))) throw new Error('В каталоге есть неполные записи.');
    return value;
  }
  function link(url, label) {
    const safe = ScienceProtocol.safeURL(url); if (!safe) return el('span', label + ' — ссылка не подтверждена');
    const a = el('a',label); a.href = safe; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
  }
  function renderCatalog() {
    const search = $('search').value.trim().toLocaleLowerCase(), category = $('category').value;
    const items = catalog.items.filter(i => (!category || asText(i.category) === category) && [i.name,i.summary,i.category,i.limitations,i.integration].map(asText).join(' ').toLocaleLowerCase().includes(search));
    $('catalog-count').textContent = 'Показано: ' + items.length + ' из ' + catalog.items.length;
    const target = $('catalog'); target.replaceChildren();
    if (!items.length) target.append(el('p', catalog.items.length ? 'По этому запросу ничего не найдено. Измените поиск или категорию.' : 'В каталоге пока нет опубликованных записей.', 'empty-state'));
    for (const item of items) {
      const card = el('article', undefined, 'science-card'); card.dataset.selected = String($('platform').value === item.id);
      card.append(el('span',statuses[item.status] || asText(item.status),'badge neutral'), el('h3',item.name), el('p',asText(item.summary)));
      const facts = el('dl');
      for (const [label,value] of [['Категория',item.category],['Лицензия',item.license],['Доступ',item.access],['Интеграция',item.integration],['Следующий шаг',item.next_action]]) facts.append(el('dt',label),el('dd',asText(value)));
      card.append(facts);
      const details = el('details'); details.append(el('summary','Ограничения и первоисточники'),el('p',asText(item.limitations)));
      const sources = el('ul');
      for (const [index,url] of (Array.isArray(item.primary_sources) ? item.primary_sources : []).entries()) { const li = el('li'); li.append(link(url, 'Источник ' + (index + 1))); sources.append(li); }
      if (!sources.children.length) sources.append(el('li','Первоисточники не указаны.'));
      details.append(sources); card.append(details,el('p','Проверка каталога: ' + date(item.reviewed_at || catalog.reviewed_at),'review-date'));
      const actions = el('div',undefined,'card-links'), choose = el('button','В протокол','button small'); choose.type = 'button'; choose.dataset.platform = item.id;
      choose.addEventListener('click',() => { if (!canWrite() || busy || submission.pending) return; $('platform').value = item.id; renderCatalog(); $('protocol').scrollIntoView({behavior:'auto'}); $('template').focus(); });
      actions.append(link(item.url,'Открыть сайт ↗'), choose); card.append(actions); target.append(card);
    }
    buttons();
  }
  function renderTemplate() {
    const template = catalog.templates.find(t => t.id === $('template').value);
    $('template-name').textContent = template?.name || 'Выберите рабочий процесс';
    $('template-description').textContent = template?.description || 'Шаги появятся после выбора шаблона.';
    $('template-steps').replaceChildren(...(template?.steps || []).map(s => el('li',asText(s))));
  }
  function selects() {
    if (submission.pending) return;
    for (const [id,items,label] of [['project',projects,'Выберите проект'],['template',catalog.templates,'Выберите процесс'],['platform',catalog.items,'Выберите платформу']]) {
      const old = $(id).value; $(id).replaceChildren(new Option(label,''), ...items.map(i => new Option(i.name || i.title,i.id)));
      if (items.some(i => i.id === old)) $(id).value = old;
    }
    if (!projects.length) $('project').replaceChildren(new Option('Сначала создайте проект в студии',''));
  }
  async function load() {
    if (busy) return; busy = true; ready = false; buttons(); notice('notice','Проверяем сеанс, каталог и проекты…');
    try {
      session = await api('/api/session');
      if (!(session?.mode === 'local' || session?.authenticated === true)) throw new Error('Сеанс не подтверждён.');
      $('session-label').textContent = session.mode === 'local' ? 'Локальное пространство' : 'Личный облачный сеанс'; $('login-link').hidden = true;
      const [newCatalog, workspace] = await Promise.all([api('/api/science/catalog'),api('/api/studio')]);
      catalog = validateCatalog(newCatalog);
      if (!workspace || workspace.schema_version !== 1 || !Array.isArray(workspace.projects) || !workspace.projects.every(p => p && typeof p.id === 'string' && typeof p.title === 'string')) throw new Error('Не удалось прочитать список проектов.');
      projects = workspace.projects;
      const oldCategory = $('category').value, categories = [...new Set(catalog.items.map(i => asText(i.category)))].sort();
      $('category').replaceChildren(new Option('Все категории',''), ...categories.map(c => new Option(c,c))); if (categories.includes(oldCategory)) $('category').value = oldCategory;
      selects(); renderTemplate(); ready = true; renderCatalog();
      $('catalog-notice').textContent = asText(catalog.notice || 'Запись в каталоге не подтверждает подключение аккаунта или доступность вычислений.');
      $('reviewed').textContent = 'Обзор: ' + date(catalog.reviewed_at);
      notice('notice','Каталог и проекты загружены. Сохранение протокола создаёт задачу без вызова моделей.');
      if (!submission.pending) notice('form-status',projects.length ? 'Выберите проект и заполните протокол.' : 'Создайте проект в студии, затем нажмите «Обновить».');
    } catch (error) { notice('notice',error.message || 'Не удалось загрузить данные. Повторите обновление.',true); if (!catalog.items.length) $('catalog').replaceChildren(el('p','Каталог недоступен. Нажмите «Обновить» после восстановления соединения.','empty-state')); }
    finally { busy = false; buttons(); }
  }
  function values() {
    return {project_id:$('project').value, template_id:$('template').value, platform_id:$('platform').value, ...Object.fromEntries(['title','question','hypothesis','method','control','criteria'].map(k => [k,$(k).value]))};
  }
  function download(value) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json;charset=utf-8'})), a = el('a');
    a.href = url; a.download = 'neuromorph-research-protocol.json'; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url),1000);
  }
  $('protocol-form').addEventListener('submit',async event => {
    event.preventDefault(); if (!canWrite() || busy || submission.completed) return;
    let payload;
    try { payload = submission.pending?.payload || submission.capture(ScienceProtocol.build(values(),catalog,projects), () => crypto.randomUUID()); }
    catch (error) { notice('form-status',error.message,true); return; }
    busy = true; buttons(); notice('form-status','Сохраняем протокол. Запрос не вызывает модель и не запускает расчёт.');
    try {
      const result = await api('/api/science/protocol',payload); submission.acknowledge(result);
      notice('form-status','Задача ' + result.id + ' сохранена в проекте. Продолжите работу в студии или скачайте протокол для выбранного ассистента.');
    } catch (error) {
      const rejected = submission.rejectValidation(error.status);
      notice('form-status',(error.message || 'Нет подтверждения сервера.') + (rejected ? ' Сервер не принял протокол: исправьте поля перед новой отправкой.' : ' Протокол зафиксирован: повтор использует тот же ключ и содержание. Его можно скачать.'),true);
    }
    finally { busy = false; buttons(); }
  });
  $('export').addEventListener('click',() => download({schema_version:1, exported_at:new Date().toISOString(), type:'research_protocol', execution:'not_dispatched', catalog_reviewed_at:catalog.reviewed_at || null, request:submission.pending?.payload || values(), saved_task_id:submission.completed?.id || null, notice:'Черновик или сохранённый план. Экспорт не подтверждает научный результат, доступ к платформе или выполнение.'}));
  $('new-protocol').addEventListener('click',() => { if (busy) return; try { submission.reset(); $('protocol-form').reset(); selects(); renderTemplate(); renderCatalog(); notice('form-status','Новый протокол. Предыдущая задача сохранена в студии.'); buttons(); } catch (error) { notice('form-status',error.message,true); } });
  $('search').addEventListener('input',renderCatalog); $('category').addEventListener('change',renderCatalog); $('platform').addEventListener('change',renderCatalog); $('template').addEventListener('change',renderTemplate); $('refresh').addEventListener('click',load);
  load();
})();

'use strict';

const M02Runs = (() => {
  const repository = 'Petr111111110000568/neuromorph-agent-os';
  const digest = /^[0-9a-f]{64}$/;
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const text = (value, max = 1000) => typeof value === 'string' && value.length > 0 && value.length <= max;
  const fail = message => { throw new Error(message); };

  function validIdentity(value) {
    return object(value) && value.repository === repository && value.workflow_id === '.github/workflows/m02-checkpoint.yml'
      && value.branch === 'main' && /^[0-9a-f]{40}$/.test(value.commit) && /^[1-9][0-9]{0,19}$/.test(value.run_id);
  }
  function sameIdentity(left, right) {
    return validIdentity(left) && validIdentity(right)
      && ['repository', 'workflow_id', 'branch', 'commit', 'run_id'].every(key => left[key] === right[key]);
  }
  function sourceURL(value, identity) {
    if (!validIdentity(identity)) return null;
    const expected = 'https://github.com/' + repository + '/actions/runs/' + identity.run_id;
    return value === expected ? expected : null;
  }
  function validateSummary(value) {
    const keys = ['task_count', 'accepted_tasks', 'actual_attempts', 'reserved_attempts', 'max_reserved_attempts', 'review_count', 'transition_count'];
    if (!object(value) || !keys.every(key => Number.isInteger(value[key]) && value[key] >= 0 && value[key] <= 10000)
        || value.accepted_tasks > value.task_count || value.reserved_attempts > value.max_reserved_attempts
        || value.model_calls !== 0 || value.additional_spend_usd !== 0) fail('Сводка квитанции имеет неподдерживаемый формат.');
    return value;
  }
  function validateCatalog(catalog) {
    if (!Array.isArray(catalog) || catalog.length !== 2) fail('Ожидался проверенный набор из двух квитанций M02.');
    const ids = new Set();
    for (const entry of catalog) {
      if (!object(entry) || !text(entry.receipt_id, 128) || ids.has(entry.receipt_id) || !text(entry.title, 500)
          || !['seed', 'resume'].includes(entry.phase) || !validIdentity(entry.identity)
          || !sourceURL(entry.source_url, entry.identity) || !digest.test(entry.canonical_sha256)
          || !Array.isArray(entry.limitations) || entry.limitations.length > 30
          || !entry.limitations.every(item => text(item, 2000))
          || entry.receipt_representation !== 'bounded_projection'
          || !object(entry.receipt) || entry.receipt.phase !== entry.phase
          || !sameIdentity(entry.receipt.identity, entry.identity)) fail('Квитанция или её происхождение не подтверждены.');
      validateSummary(entry.summary);
      if (!object(entry.receipt.state) || !Array.isArray(entry.receipt.state.reviews)
          || entry.receipt.state.reviews.length > 30) fail('В квитанции отсутствует ограниченный список решений рецензента.');
      ids.add(entry.receipt_id);
    }
    const seed = catalog.find(entry => entry.phase === 'seed');
    const resume = catalog.find(entry => entry.phase === 'resume');
    if (!seed || !resume || seed.identity.run_id === resume.identity.run_id
        || seed.identity.commit !== resume.identity.commit
        || resume.receipt.previous_run_id !== seed.identity.run_id
        || !digest.test(resume.receipt.previous_manifest_sha256)) fail('Связь исходного запуска и восстановления не подтверждена.');
    return [seed, resume];
  }
  function validateImport(record, catalog) {
    const entry = Array.isArray(catalog) && catalog.find(item => item.receipt_id === record?.receipt_id);
    if (!object(record) || !text(record.id, 200) || !text(record.project_id, 200) || !entry
        || record.canonical_sha256 !== entry.canonical_sha256 || !sameIdentity(record.identity, entry.identity)
        || record.source_url !== entry.source_url || record.phase !== entry.phase
        || !text(record.title, 500) || !text(record.imported_at, 100)) fail('Нет подтверждённой записи импорта с ожидаемым происхождением.');
    validateSummary(record.summary);
    return record;
  }
  function validateResponse(value) {
    if (!object(value) || !Array.isArray(value.imports) || value.imports.length > 4000
        || !object(value.capabilities) || value.capabilities.historical !== true
        || value.capabilities.live_verification !== false || value.capabilities.execution !== false) fail('Ответ сервера не подтверждает режим исторических результатов.');
    const catalog = validateCatalog(value.catalog);
    return {catalog, imports: value.imports.map(record => validateImport(record, catalog)), capabilities: value.capabilities};
  }
  class Submission {
    constructor() { this.pending = null; this.completed = null; }
    capture(projectId, entry, keyFactory) {
      if (this.completed) fail('Этот результат уже сохранён. Выберите следующий импорт.');
      if (!text(projectId, 200) || !entry || !text(entry.receipt_id, 128) || !digest.test(entry.canonical_sha256)) fail('Выберите проект и квитанцию.');
      if (this.pending && (this.pending.payload.project_id !== projectId || this.pending.payload.receipt_id !== entry.receipt_id
          || this.pending.canonical_sha256 !== entry.canonical_sha256)) fail('Сначала подтвердите прежний запрос. Его проект, квитанция и ключ зафиксированы.');
      if (!this.pending) {
        const key = keyFactory();
        if (!text(key, 128)) fail('Не удалось создать ключ запроса.');
        this.pending = Object.freeze({canonical_sha256: entry.canonical_sha256,
          payload: Object.freeze({project_id: projectId, receipt_id: entry.receipt_id, idempotency_key: key})});
      }
      return this.pending.payload;
    }
    acknowledge(record, catalog) {
      validateImport(record, catalog);
      if (!this.pending || record.project_id !== this.pending.payload.project_id
          || record.receipt_id !== this.pending.payload.receipt_id || record.canonical_sha256 !== this.pending.canonical_sha256) fail('Ответ не соответствует незавершённому импорту. Повторите тот же запрос.');
      this.completed = Object.freeze({...record});
      return this.completed;
    }
    reconcile(imports, catalog) {
      this.completed = null;
      if (!this.pending) return null;
      const record = imports.find(item => item.project_id === this.pending.payload.project_id
        && item.receipt_id === this.pending.payload.receipt_id && item.canonical_sha256 === this.pending.canonical_sha256);
      return record ? this.acknowledge(record, catalog) : null;
    }
    clearConfirmation() { this.completed = null; }
    reset() {
      if (this.pending && !this.completed) fail('Ответ на предыдущий импорт ещё неизвестен. Сначала подтвердите его повтором.');
      this.pending = null; this.completed = null;
    }
  }
  class RequestEpoch {
    constructor() { this.generation = 0; }
    begin() { return ++this.generation; }
    isCurrent(generation) { return generation === this.generation; }
    apply(generation, operation) {
      if (!this.isCurrent(generation)) return false;
      operation(); return true;
    }
  }
  return {repository, sourceURL, validateCatalog, validateImport, validateResponse, Submission, RequestEpoch};
})();

if (typeof module !== 'undefined' && module.exports) module.exports = M02Runs;

if (typeof document !== 'undefined') (() => {
  const $ = id => document.getElementById(id);
  const submission = new M02Runs.Submission();
  const requests = new M02Runs.RequestEpoch();
  let catalog = [], imports = [], capabilities = {}, projects = [], session = null;
  let selectedReceipt = '', ready = false, busy = false;
  const el = (tag, value, className) => { const node = document.createElement(tag); if (value !== undefined) node.textContent = String(value); if (className) node.className = className; return node; };
  const date = value => { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? 'Дата не указана' : parsed.toLocaleString('ru-RU'); };
  const phases = {seed: 'Исходный запуск', resume: 'Восстановление'};
  const decisions = {accepted: 'Принято', rejected: 'Отклонено', inconclusive: 'Недостаточно данных'};

  function notice(id, message, error = false) { $(id).textContent = message; $(id).className = 'notice' + (error ? ' error' : ''); }
  function canWrite() { return ready && capabilities.import_receipts === true && session && (session.mode === 'local' || session.authenticated === true); }
  function entrySelected() { return catalog.find(entry => entry.receipt_id === selectedReceipt); }
  function existingSelection() { return imports.find(item => item.project_id === $('project').value && item.receipt_id === selectedReceipt); }
  function buttons() {
    const entry = entrySelected();
    const pending = submission.pending && !submission.completed;
    $('refresh').disabled = busy;
    $('project').disabled = !canWrite() || busy || !!submission.pending;
    document.querySelectorAll('input[name="receipt"]').forEach(input => { input.disabled = !ready || busy || !!submission.pending; });
    $('import-button').disabled = !canWrite() || busy || !!submission.completed || !entry || !projects.some(project => project.id === $('project').value)
      || (!pending && !!existingSelection());
    $('import-button').textContent = submission.completed ? 'Квитанция сохранена' : busy && pending ? 'Сохраняем…'
      : pending ? 'Подтвердить тем же запросом' : existingSelection() ? 'Уже сохранено в этом проекте' : 'Сохранить в проект';
    $('next-import').hidden = !submission.completed;
    $('next-import').disabled = busy;
    $('selected-receipt').textContent = entry ? entry.title : 'Выберите карточку результата';
    $('import-form').setAttribute('aria-busy', busy ? 'true' : 'false');
  }
  function newKey() {
    if (typeof crypto === 'undefined' || !crypto.getRandomValues) throw new Error('Браузер не поддерживает безопасный ключ запроса.');
    if (crypto.randomUUID) return 'm02-' + crypto.randomUUID();
    return 'm02-' + Array.from(crypto.getRandomValues(new Uint8Array(20)), item => item.toString(16).padStart(2, '0')).join('');
  }
  async function api(path, payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const headers = {Accept: 'application/json'};
      if (payload) { headers['Content-Type'] = 'application/json'; if (session?.csrf_token) headers['X-CSRF-Token'] = session.csrf_token; }
      const response = await fetch(path, {method: payload ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
        headers, body: payload ? JSON.stringify(payload) : undefined, signal: controller.signal});
      // Transport has no session/DOM side effects: a late failed sibling from
      // an earlier Promise.all must not invalidate a newer successful load.
      let value;
      try { value = await response.json(); } catch {
        const error = new Error('Сервер вернул нечитаемый ответ. Незавершённый импорт можно подтвердить тем же запросом.');
        error.status = response.status; throw error;
      }
      if (!response.ok) {
        const code = typeof value?.error?.code === 'string' ? value.error.code : '';
        const error = new Error(response.status === 401 ? 'Сеанс завершён. Войдите в новой вкладке, затем нажмите «Обновить данные» здесь. Не закрывайте эту вкладку с незавершённым запросом.'
          : response.status === 403 ? 'Доступ отклонён. Обновите сеанс перед повтором; ключ импорта сохранён.'
          : response.status === 409 ? 'Сервер обнаружил повтор или конфликт импорта. Обновите данные, чтобы проверить сохранённую запись. Ключ запроса не изменён.'
          : 'Сервер отклонил запрос. Обновите данные и проверьте доступность проекта.');
        error.status = response.status; error.code = code; throw error;
      }
      return value;
    } finally { clearTimeout(timer); }
  }
  function external(url, label) {
    const link = el('a', label); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; return link;
  }
  function pair(list, term, content) { list.append(el('dt', term)); const value = el('dd'); value.append(typeof content === 'string' ? el('code', content) : content); list.append(value); }
  function metric(list, label, number, suffix) { const item = el('div'); item.append(el('dt', label)); const value = el('dd', number); if (suffix) value.append(el('small', suffix)); item.append(value); list.append(item); }
  function renderLineage() {
    const list = $('lineage'); list.replaceChildren();
    for (const entry of catalog) {
      const item = el('li'); item.append(el('h3', phases[entry.phase]));
      item.append(el('p', entry.phase === 'seed' ? 'Очередь и состояние проектов сохранены после первого принятого результата.'
        : 'Состояние восстановлено из исходного запуска. Проверка и ограниченная доработка продолжены.'));
      item.append(external(entry.source_url, 'GitHub run ' + entry.identity.run_id + ' ↗')); list.append(item);
    }
    $('lineage-status').textContent = 'Связь previous_run_id проверена в сохранённых квитанциях; SHA исходного manifest указан в восстановлении. Это историческая цепочка, не новый live-запуск.';
  }
  function renderReceipts() {
    const target = $('receipts'); target.replaceChildren();
    for (const entry of catalog) {
      const card = el('article', undefined, 'receipt-card' + (entry.receipt_id === selectedReceipt ? ' selected' : ''));
      const heading = el('div', undefined, 'receipt-heading');
      const label = el('label'); const radio = el('input'); radio.type = 'radio'; radio.name = 'receipt'; radio.value = entry.receipt_id;
      radio.checked = entry.receipt_id === selectedReceipt;
      label.append(radio, el('span', entry.title));
      const title = el('h3'); title.append(label); heading.append(title, el('p', 'Run ' + entry.identity.run_id + ' · ' + phases[entry.phase]));
      radio.addEventListener('change', () => { selectedReceipt = radio.value; selectionChanged(); });
      const badges = el('div', undefined, 'receipt-badges'); badges.append(el('span', 'Историческая квитанция', 'badge neutral'), el('span', 'Синтетический эксперимент', 'badge warning'));
      heading.append(badges); card.append(heading);
      const body = el('div', undefined, 'receipt-body');
      const metrics = el('dl', undefined, 'run-metrics');
      metric(metrics, 'Резерв попыток', entry.summary.reserved_attempts, 'из ' + entry.summary.max_reserved_attempts);
      metric(metrics, 'Фактические попытки', entry.summary.actual_attempts);
      metric(metrics, 'Принятые задачи', entry.summary.accepted_tasks, 'из ' + entry.summary.task_count);
      body.append(metrics, el('p', 'Резерв выделяется до отправки задания; фактическая попытка считается при выдаче аренды. Вызовов моделей: 0. Дополнительные расходы: 0.', 'metric-note'));
      body.append(el('h4', 'Решения рецензента · ' + entry.summary.review_count));
      const reviews = el('ul', undefined, 'reviews-list');
      for (const review of entry.receipt.state.reviews) {
        const item = el('li'); const decision = decisions[review.decision] || 'Неизвестное решение';
        item.append(el('span', decision, 'badge' + (review.decision === 'accepted' ? '' : ' warning')));
        const content = el('div', undefined, 'review-text');
        content.append(el('strong', typeof review.task_id === 'string' ? review.task_id : 'Задача не указана'));
        content.append(el('small', 'Ревизия ' + (Number.isInteger(review.revision) ? review.revision : '—')));
        if (typeof review.note === 'string') content.append(el('p', review.note));
        if (typeof review.result_sha256 === 'string') content.append(el('small', 'Результат SHA-256: ' + review.result_sha256));
        item.append(content); reviews.append(item);
      }
      if (!reviews.childNodes.length) reviews.append(el('li', 'Решений в этой квитанции нет.'));
      body.append(reviews);
      const provenance = el('details', undefined, 'receipt-provenance'); provenance.append(el('summary', 'Происхождение, SHA и ограничения'));
      const fields = el('dl', undefined, 'provenance-list');
      pair(fields, 'ID квитанции', entry.receipt_id); pair(fields, 'Workflow', entry.identity.workflow_id);
      pair(fields, 'Ветка', entry.identity.branch);
      pair(fields, 'Commit', external('https://github.com/' + M02Runs.repository + '/commit/' + entry.identity.commit, entry.identity.commit));
      pair(fields, 'SHA-256 исходной квитанции', entry.canonical_sha256);
      if (entry.phase === 'resume') {
        pair(fields, 'Предыдущий run', external(catalog[0].source_url, entry.receipt.previous_run_id));
        pair(fields, 'SHA-256 его manifest', entry.receipt.previous_manifest_sha256);
      }
      pair(fields, 'Переходы состояния', String(entry.summary.transition_count));
      provenance.append(fields, el('p', 'На странице показана ограниченная проекция: сводка, задачи и решения. SHA-256 относится к исходной закреплённой квитанции, а не к этой проекции.', 'metric-note'));
      const limitations = el('ul', undefined, 'limits-list');
      for (const limitation of entry.limitations) limitations.append(el('li', limitation));
      provenance.append(limitations); body.append(provenance);
      const links = el('div', undefined, 'run-links'); links.append(external(entry.source_url, 'Открыть исходный run ↗')); body.append(links);
      card.append(body); target.append(card);
    }
  }
  function renderProjects() {
    const prior = submission.pending?.payload.project_id || $('project').value;
    $('project').replaceChildren(new Option(projects.length ? 'Выберите свой проект' : 'Сначала создайте проект в студии', ''));
    for (const project of projects) $('project').append(new Option(project.title, project.id));
    if (projects.some(project => project.id === prior)) $('project').value = prior;
  }
  function renderImports() {
    const target = $('imports'); target.replaceChildren();
    if (!imports.length) { target.append(el('p', 'Импортированных квитанций в доступном пространстве пока нет.', 'empty-state')); return; }
    for (const record of imports) {
      const project = projects.find(item => item.id === record.project_id);
      const card = el('article', undefined, 'import-record');
      card.append(el('span', 'Историческая квитанция', 'badge neutral'), el('h3', record.title));
      card.append(el('p', project ? project.title : 'Проект ' + record.project_id));
      card.append(el('p', 'Сохранено: ' + date(record.imported_at), 'meta'));
      card.append(el('p', 'Запись импорта: ' + record.id, 'meta'));
      const link = el('a', 'Открыть студию проектов →'); link.href = '/studio.html'; card.append(link); target.append(card);
    }
  }
  function selectionChanged() {
    document.querySelectorAll('.receipt-card').forEach(card => card.classList.toggle('selected', card.querySelector('input').value === selectedReceipt));
    if (!submission.pending) notice('import-status', existingSelection() ? 'Эта квитанция уже сохранена в выбранном проекте. Повторная запись не нужна.'
      : 'Квитанция привязывается к проекту с её происхождением и ограничениями. Выполнение задачи не возобновляется.');
    buttons();
  }
  function completedNotice() { notice('import-status', 'Квитанция сохранена и привязана к выбранному проекту. Запись доступна ниже в разделе «Сохранено в ваших проектах» на странице облачных результатов.'); }
  function clearPrivateView() {
    projects = []; imports = []; submission.clearConfirmation();
    renderProjects(); renderImports();
    $('imports').replaceChildren(el('p', 'Записи владельца будут показаны после проверки текущего сеанса.', 'empty-state'));
  }
  function requireLogin() {
    ready = false; session = null; clearPrivateView();
    $('login-link').hidden = false; $('session-label').textContent = 'Требуется вход';
  }

  async function load() {
    if (busy) return;
    const generation = requests.begin();
    busy = true; ready = false; session = null; clearPrivateView(); buttons();
    $('session-label').textContent = 'Проверяем сеанс…';
    notice('notice', 'Проверяем сеанс и перечитываем сохранённые результаты…');
    if (submission.pending) notice('import-status', 'Проверяем сохранение прежнего запроса в текущем пространстве. Ключ запроса остаётся прежним.');
    try {
      const nextSession = await api('/api/session');
      if (!requests.isCurrent(generation)) return;
      session = nextSession;
      if (!(session?.mode === 'local' || session?.authenticated === true)) throw new Error('Сеанс не подтверждён. Войдите и обновите данные.');
      $('session-label').textContent = session.mode === 'local' ? 'Локальное пространство' : 'Личный вход подтверждён'; $('login-link').hidden = true;
      const [response, workspace] = await Promise.all([api('/api/studio/m02'), api('/api/studio')]);
      if (!requests.isCurrent(generation)) return;
      const parsed = M02Runs.validateResponse(response);
      if (!workspace || workspace.schema_version !== 1 || !Array.isArray(workspace.projects) || workspace.projects.length > 2000
          || !workspace.projects.every(project => project && typeof project.id === 'string' && typeof project.title === 'string')) throw new Error('Не удалось прочитать список ваших проектов.');
      catalog = parsed.catalog; imports = parsed.imports; capabilities = parsed.capabilities; projects = workspace.projects;
      selectedReceipt = submission.pending?.payload.receipt_id || (catalog.some(entry => entry.receipt_id === selectedReceipt) ? selectedReceipt : '');
      renderProjects(); renderLineage(); renderReceipts(); renderImports();
      ready = true;
      $('receipt-count').textContent = catalog.length;
      $('catalog-status').textContent = 'Сохранённый набор с закреплёнными SHA. Проверка текущих запусков не выполнялась.';
      notice('notice', 'Загружены две исторические квитанции и ваши проекты. Облачные вычисления не запускаются.');
      if (submission.reconcile(imports, catalog)) completedNotice();
      else if (submission.pending) notice('import-status', 'Подтверждение прежнего импорта пока не найдено. Повторите тот же запрос; проект, квитанция и ключ сохранены.');
      else if (!projects.length) notice('import-status', 'Создайте проект в студии, затем обновите данные этой страницы.');
      else selectionChanged();
    } catch (error) {
      if (!requests.isCurrent(generation)) return;
      if (error.status === 401) requireLogin();
      notice('notice', error.name === 'AbortError' ? 'Сервер не ответил вовремя. Сохранённые здесь данные не обновлены; повторите запрос.' : error.message, true);
      if (!session || !(session.mode === 'local' || session.authenticated === true)) { $('session-label').textContent = 'Сеанс не подтверждён'; $('login-link').hidden = false; }
      if (!catalog.length) $('receipts').replaceChildren(el('p', 'Результаты недоступны. Проверьте вход и нажмите «Обновить данные».', 'empty-state'));
    } finally { requests.apply(generation, () => { busy = false; buttons(); }); }
  }
  $('refresh').addEventListener('click', load);
  $('project').addEventListener('change', selectionChanged);
  $('next-import').addEventListener('click', () => {
    try { submission.reset(); selectionChanged(); $('project').focus(); }
    catch (error) { notice('import-status', error.message, true); }
  });
  $('import-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!canWrite() || busy || submission.completed) return;
    let payload;
    try {
      if (!projects.some(project => project.id === $('project').value)) throw new Error('Выберите свой проект из списка.');
      payload = submission.capture($('project').value, entrySelected(), newKey);
    } catch (error) { notice('import-status', error.message, true); return; }
    const generation = requests.begin();
    busy = true; buttons(); notice('import-status', 'Сохраняем выбранную историческую квитанцию…');
    try {
      const record = await api('/api/studio/m02/import', payload);
      if (!requests.isCurrent(generation)) return;
      submission.acknowledge(record, catalog);
      if (!imports.some(item => item.id === record.id)) imports.push(record);
      renderImports(); completedNotice();
    } catch (error) {
      if (!requests.isCurrent(generation)) return;
      if (error.status === 401) requireLogin();
      notice('import-status', error.name === 'AbortError' ? 'Подтверждение не получено вовремя. Запрос мог сохраниться. Повторите его той же кнопкой или обновите данные; ключ остаётся прежним.' : error.message, true);
    } finally { requests.apply(generation, () => { busy = false; buttons(); }); }
  });
  load();
})();

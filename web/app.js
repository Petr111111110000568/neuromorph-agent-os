"use strict";
(() => {
  const state = {plugins: [], advisors: [], runs: [], workflows: [], sources: [], status: null, view: "overview"};
  const sourceCatalog = new Map();
  const sourceSelections = {council: new Set(), workflow: new Set()};
  const sourceSelectionLimit = 20;
  const viewNames = {overview: "Обзор", evidence: "База знаний", advisors: "Советники", experiments: "Моделирование", workflows: "Исследовательский цикл", environment: "Окружение", journal: "Журнал и отчёты"};
  const fieldNames = {status: "Состояние", mode: "Режим", name: "Название", version: "Версия", description: "Описание", details: "Подробности", limitations: "Ограничения", installed: "Установлено", available: "Доступно", configured: "Настроено", enabled: "Включено", verified: "Проверено", required: "Необходимо", scope: "Область", runtime: "Среда исполнения", python: "Python", llm: "Языковые модели", mcp: "MCP", qpu: "Квантовое оборудование", browser: "Браузер", network: "Сеть", isolation: "Изоляция", plugins: "Плагины", voice: "Голосовой ввод", deployment: "Развёртывание", data: "Данные", security: "Защита", authentication: "Аутентификация", account: "Аккаунт", notes: "Примечания", local: "Локальное окружение", capabilities: "Возможности", dependencies: "Зависимости", checks: "Проверки", requirements: "Требования", priority: "Приоритет", phase: "Этап", target: "Цель", reason: "Причина", reason_unavailable: "Причина недоступности", next_action: "Следующий шаг", implementation: "Реализация", evidence: "Основание", last_verified: "Последняя проверка", created_at: "Создано", updated_at: "Обновлено", basis: "Основание", level: "Уровень", kind: "Тип", configured_here: "Настроено здесь", external_services: "Внешние службы", platform: "Платформа", detected: "Обнаружено", note: "Примечание", provider: "Провайдер", command: "Команда", port: "Порт", storage: "Хранилище", host: "Адрес", artifacts: "Артефакты", blockers: "Что требуется", acceptance: "Критерии готовности"};
  const statusNames = {completed: "Завершено", succeeded: "Успешно", success: "Успешно", failed: "Ошибка", error: "Ошибка", running: "Выполняется", queued: "В очереди", available: "Доступно", installed: "Установлено", built_in: "Встроенный модуль", builtin: "Встроенный модуль", ready: "Готово", active: "Активно", configured: "Настроено", implemented: "Реализовано", reviewed_not_installed: "Изучено, не установлено", reviewed: "Изучено", not_installed: "Не установлено", not_configured: "Не настроено", unavailable: "Недоступно", disabled: "Отключено", planned: "В плане", planned_not_implemented: "В плане", draft: "Черновик", unreviewed: "Не проверено", user_added: "Добавлено пользователем", needs_credentials: "Нужен доступ", requires_credentials: "Нужен доступ", needs_setup: "Нужна настройка", blocked: "Заблокировано", experimental: "Экспериментально", simulation: "Симуляция", rule_based: "По правилам", research_only: "Исследовательский режим", unknown: "Неизвестно"};
  Object.assign(fieldNames, {checked_at: "Проверено", packages: "Пакеты Python", programs: "Программы", namespace_isolation_available: "Изоляция namespaces доступна", namespace_probe: "Проверка изоляции", credentials_present: "Параметры подключения", previous_scratch_project_present: "Предыдущая рабочая копия найдена", repositories_seen: "Репозитории доступны", project_repository: "Репозиторий проекта", repository_visibility: "Видимость репозитория", publication: "Публикация", verification: "Проверка", directory_matches: "Найдено подключений", profile: "Профиль", venv_created: "Виртуальное окружение создано", llm_configured: "Языковая модель настроена", qpu_configured: "Квантовый процессор настроен", untrusted_execution_enabled: "Выполнение недоверенного кода включено", personal_skill: "Навык проекта", external_integrations: "Внешние подключения", available_packages: "Доступные пакеты", unavailable_packages: "Отсутствующие пакеты", available_programs: "Доступные программы", unavailable_programs: "Отсутствующие программы", api_key_present: "Ключ API задан", source: "Источник сведений"});
  Object.assign(statusNames, {connected_read_verified: "Подключено, чтение проверено", installed_verified: "Установлено и проверено", availability_not_confirmed: "Доступность не подтверждена", not_performed: "Не выполнялась", requires_provider_and_host: "Нужны провайдер и сервер", requires_external_runtime: "Нужна внешняя среда", prepared_architecture: "Архитектура подготовлена", classical_demo_ready: "Классическая симуляция готова", not_demonstrated: "Не продемонстрировано", local_stdlib: "Локально, стандартная библиотека Python", public: "Публичный", private: "Закрытый"});
  const $ = (id) => document.getElementById(id);
  const el = (tag, className, value) => {const node = document.createElement(tag); if (className) node.className = className; if (value !== undefined && value !== null) node.textContent = String(value); return node;};
  const array = (value) => Array.isArray(value) ? value : [];
  const items = (value) => Array.isArray(value) ? value : array(value && value.items);
  const printable = (value) => value === null || value === undefined ? "—" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  const clear = (node) => { node.replaceChildren(); return node; };
  const fmt = (value) => typeof value === "number" ? new Intl.NumberFormat("ru-RU", {maximumSignificantDigits: 7}).format(value) : printable(value);
  const label = (key) => fieldNames[key] || key.replaceAll("_", " ");
  const date = (value) => {if (!value) return "—"; const d = new Date(value); return Number.isNaN(d.valueOf()) ? String(value) : d.toLocaleString("ru-RU", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"});};
  let toastTimer;
  function toast(message, error = false) {const box = $("toast"); box.textContent = message; box.className = error ? "toast error" : "toast"; box.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => {box.hidden = true;}, error ? 14000 : 6000);}
  async function api(path, data) {
    const options = {headers: {Accept: "application/json"}, cache: "no-store"};
    if (data !== undefined) {options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(data);}
    const response = await fetch(path, options);
    let payload;
    try { payload = await response.json(); } catch (_) {throw new Error(`Сервер вернул некорректный ответ (${response.status}).`);}
    if (!response.ok) throw new Error(payload.error && (payload.error.message || printable(payload.error)) || `Ошибка сервера: ${response.status}`);
    return payload;
  }
  function badge(value) {const key = String(value || "unknown"); let cls = "badge neutral"; if (["completed", "succeeded", "success", "ready", "available", "installed", "active", "configured", "implemented", "builtin", "built_in"].includes(key)) cls = "badge"; else if (["failed", "error", "blocked"].includes(key)) cls = "badge error"; else if (["not_configured", "unreviewed", "experimental", "needs_credentials", "requires_credentials"].includes(key)) cls = "badge warning"; return el("span", cls, statusNames[key] || key);}
  function loading(target, message = "Выполняется запрос…") {const box = el("div", "panel loading-block"); box.append(el("span", "spinner"), el("span", "", message)); clear(target).append(box);}
  function errorPanel(target, error) {clear(target).append(el("div", "panel error-panel", error.message || printable(error)));}
  function empty(target, message, action) {const box = el("div", "empty-inline"); box.append(el("p", "", message)); if (action) box.append(action); clear(target).append(box);}
  function textButton(text, action) {const button = el("button", "text-button", text); button.type = "button"; button.addEventListener("click", action); return button;}
  function safeLink(url, title, className = "source-link") {try {const parsed = new URL(url); if (!["https:", "http:"].includes(parsed.protocol)) return el("span", "muted", "Ссылка недоступна"); const link = el("a", className, title); link.href = parsed.href; link.target = "_blank"; link.rel = "noopener noreferrer"; return link;} catch (_) {return el("span", "muted", "Ссылка не задана");}}
  function rawDetails(title, value) {const detail = el("details", "raw-details"); detail.append(el("summary", "", title), el("pre", "", printable(value))); return detail;}
  function limitsBox(limits) {const list = array(limits); if (!list.length) return null; const box = el("div", "boundary-note"); box.append(el("span", "note-mark", "i"), el("p", "", list.map(printable).join(" · "))); return box;}
  function navigate(view, updateHash = true) {
    if (!viewNames[view]) view = "overview";
    state.view = view;
    document.querySelectorAll(".view").forEach((node) => {node.hidden = node.id !== `view-${view}`;});
    document.querySelectorAll(".nav-item").forEach((node) => {const active = node.dataset.view === view; node.classList.toggle("active", active); if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");});
    $("breadcrumb-view").textContent = viewNames[view];
    document.title = `${viewNames[view]} — Meta-Harness`;
    if (updateHash && location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    if (view === "environment") loadEnvironment();
    if (view === "journal") loadJournal();
    if (view === "workflows") loadWorkflows();
  }
  function renderStats() {
    const stats = state.status && state.status.counts || {};
    const values = [["Источники в реестре", stats.sources, "база знаний"], ["Расчётные модули", stats.plugins, "в этой сборке"], ["Выполненные расчёты", stats.runs, "сохранено"], ["Роли советников", state.advisors.length || undefined, "по правилам"]];
    const node = clear($("overview-stats"));
    for (const [title, value, suffix] of values) {const card = el("div", "stat-card"); card.append(el("span", "", title), el("strong", "", value === undefined ? "—" : fmt(value)), el("small", "", suffix)); node.append(card);}
  }
  async function loadStatus() {
    try {state.status = await api("/api/status"); $("connection-dot").className = "status-dot"; $("connection-text").textContent = "Локальный сервер подключён"; $("sidebar-version").textContent = `v${state.status.version || "—"}`; renderStats();} catch (error) {$("connection-dot").className = "status-dot error"; $("connection-text").textContent = "Сервер недоступен"; toast(error.message, true);}
  }
  function renderPlugins() {
    const target = clear($("overview-plugins"));
    state.plugins.slice(0, 5).forEach((plugin, i) => {const row = el("div", "module-item"); const content = el("div"); content.append(el("h3", "", plugin.name || plugin.id), el("p", "", plugin.description || "Описание не задано"), badge(plugin.status)); const open = el("button", "module-open", "↗"); open.type = "button"; open.setAttribute("aria-label", `Открыть модуль ${plugin.name || plugin.id}`); open.addEventListener("click", () => {$("experiment-plugin").value = plugin.id; renderParameters("experiment"); navigate("experiments");}); row.append(el("span", "module-index", String(i + 1).padStart(2, "0")), content, open); target.append(row);});
    if (!state.plugins.length) empty(target, "Нет доступных расчётных модулей.");
    for (const prefix of ["experiment", "workflow"]) {const select = clear($(`${prefix}-plugin`)); for (const plugin of state.plugins) {const option = el("option", "", plugin.name || plugin.id); option.value = plugin.id; select.append(option);} renderParameters(prefix);}
  }
  const schemaFor = (plugin) => plugin && (plugin.parameters || plugin.input_schema || plugin.inputSchema) || {};
  function renderParameters(prefix) {
    const plugin = state.plugins.find((item) => item.id === $(`${prefix}-plugin`).value);
    const target = clear($(`${prefix}-parameters`));
    $(`${prefix}-description`).textContent = plugin ? plugin.description || "" : "Нет доступных модулей.";
    $(`${prefix}-limitations`).textContent = plugin ? array(plugin.limitations).map(printable).join(" · ") : "";
    if (!plugin) return;
    const schema = schemaFor(plugin);
    for (const [key, prop] of Object.entries(schema.properties || {})) {
      const wrap = el("div", "parameter-field"); const id = `${prefix}-param-${key}`;
      const title = el("label", "", prop.title || label(key)); title.htmlFor = id;
      let input;
      if (Array.isArray(prop.enum)) {input = el("select"); for (const value of prop.enum) {const option = el("option", "", printable(value)); option.value = String(value); input.append(option);}}
      else if (prop.type === "boolean") {input = el("input"); input.type = "checkbox"; input.checked = prop.default === true; title.classList.add("checkbox-label"); title.replaceChildren(input, document.createTextNode(prop.title || label(key)));}
      else if (["array", "object"].includes(prop.type)) {input = el("textarea"); input.rows = 3; input.value = JSON.stringify(prop.default !== undefined ? prop.default : prop.type === "array" ? [] : {});}
      else {input = el("input"); input.type = ["number", "integer"].includes(prop.type) ? "number" : "text"; if (input.type === "number") {input.step = prop.type === "integer" ? "1" : "any"; if (prop.minimum !== undefined) input.min = String(prop.minimum); if (prop.maximum !== undefined) input.max = String(prop.maximum);} if (prop.maxLength !== undefined) input.maxLength = prop.maxLength; if (prop.minLength !== undefined) input.minLength = prop.minLength;}
      input.id = id; input.name = key; input.dataset.parameter = key; input.dataset.type = prop.type || "string";
      if (prop.type !== "boolean" && prop.default !== undefined && !["array", "object"].includes(prop.type)) input.value = String(prop.default);
      if (array(schema.required).includes(key) && prop.type !== "boolean") input.required = true;
      wrap.append(title); if (prop.type !== "boolean") wrap.append(input);
      if (prop.description) wrap.append(el("p", "form-help", prop.description));
      target.append(wrap);
    }
    if (!Object.keys(schema.properties || {}).length) target.append(el("p", "form-help", "Модуль не требует дополнительных параметров."));
  }
  function parameters(prefix) {
    const result = {};
    $(`${prefix}-parameters`).querySelectorAll("[data-parameter]").forEach((input) => {const key = input.dataset.parameter; const type = input.dataset.type; if (type === "boolean") result[key] = input.checked; else if (input.value !== "") {if (["integer", "number"].includes(type)) {const n = Number(input.value); if (!Number.isFinite(n) || type === "integer" && !Number.isInteger(n)) throw new Error(`Параметр «${key}» должен быть ${type === "integer" ? "целым числом" : "числом"}.`); result[key] = n;} else if (["object", "array"].includes(type)) {try {result[key] = JSON.parse(input.value);} catch (_) {throw new Error(`Проверьте JSON в параметре «${key}».`);}} else result[key] = input.value;}});
    return result;
  }
  function renderSources() {
    const target = clear($("source-list"));
    $("source-count").textContent = `В реестре: ${state.sources.length}`;
    for (const source of state.sources) {const card = el("article", "panel source-card"); const top = el("div", "source-topline"); top.append(el("span", "source-category", source.category || "Источник"), badge(source.status)); card.append(top, el("h2", "", source.title || source.id)); if (source.summary || source.description) card.append(el("p", "", source.summary || source.description)); const meta = el("dl", "source-meta"); for (const [name, value] of [["Зрелость", source.maturity], ["Подключение", source.integration], ["Ограничения", source.limitations]]) {if (value) meta.append(el("dt", "", name), el("dd", "", Array.isArray(value) ? value.map(printable).join(" · ") : printable(value)));} card.append(meta, safeLink(source.url, "Открыть первоисточник ↗")); target.append(card);}
    if (!state.sources.length) empty(target, "Источники по этому запросу не найдены. Попробуйте другое слово или добавьте публикацию.");
  }
  function renderSourcePickers() {
    for (const prefix of ["council", "workflow"]) {
      const target = $(`${prefix}-sources`);
      const previous = target.querySelector("details");
      const wasOpen = previous && previous.open;
      const filterValue = target.querySelector("input[type=search]")?.value || "";
      const selected = sourceSelections[prefix];
      clear(target);
      const details = el("details", "source-picker"); details.open = Boolean(wasOpen);
      const summary = el("summary"); const count = el("span", "source-picker-count"); count.setAttribute("aria-live", "polite");
      summary.append(document.createTextNode("Связать источники с исследованием"), count);
      details.append(summary, el("p", "form-help", "Выберите до 20 карточек. Их идентификаторы будут сохранены вместе с запуском. Выбор карточки не означает проверку полного содержания источника."));
      const searchLabel = el("label", "sr-only", "Найти источник для выбора"); searchLabel.htmlFor = `${prefix}-source-filter`;
      const search = el("input"); search.type = "search"; search.id = `${prefix}-source-filter`; search.placeholder = "Поиск по названию или области"; search.value = filterValue;
      const list = el("fieldset", "source-choice-list"); const legend = el("legend", "sr-only", "Источники, связанные с исследованием"); list.append(legend);
      const checkboxes = [];
      const update = () => {count.textContent = `Выбрано: ${selected.size} / ${sourceSelectionLimit}`; const query = search.value.trim().toLocaleLowerCase("ru-RU"); let visible = 0; checkboxes.forEach(({input, row, text}) => {input.disabled = selected.size >= sourceSelectionLimit && !input.checked; row.hidden = query !== "" && !text.includes(query); if (!row.hidden) visible++;}); noResults.hidden = visible > 0;};
      for (const source of sourceCatalog.values()) {
        const id = String(source.id); const row = el("label", "source-choice");
        const input = el("input"); input.type = "checkbox"; input.value = id; input.checked = selected.has(id);
        const content = el("span", "source-choice-text"); content.append(el("strong", "", source.title || id), el("small", "", source.category || "Источник"));
        row.append(input, content); list.append(row);
        checkboxes.push({input, row, text: `${source.title || id} ${source.category || ""}`.toLocaleLowerCase("ru-RU")});
        input.addEventListener("change", () => {if (input.checked) {if (selected.size >= sourceSelectionLimit) {input.checked = false; toast(`Можно выбрать не более ${sourceSelectionLimit} источников.`, true);} else selected.add(id);} else selected.delete(id); update();});
      }
      const noResults = el("p", "form-help", sourceCatalog.size ? "По запросу ничего не найдено." : "Источники пока недоступны. Исследование можно запустить без привязки источников.");
      details.append(searchLabel, search, list, noResults);
      const clearButton = textButton("Снять выбор", () => {selected.clear(); checkboxes.forEach(({input}) => {input.checked = false;}); update();});
      details.append(clearButton);
      target.append(details, el("p", "form-help", "Привязка источников необязательна. Без выбора разбор сохранится без ссылок на карточки."));
      search.addEventListener("input", update); search.addEventListener("keydown", (event) => {if (event.key === "Enter") event.preventDefault();});
      update();
    }
  }
  function selectedSources(prefix) {const selected = [...sourceSelections[prefix]]; if (selected.length > sourceSelectionLimit) throw new Error(`Выберите не более ${sourceSelectionLimit} источников.`); return selected;}
  async function loadSources() {try {state.sources = items(await api(`/api/sources?q=${encodeURIComponent($("source-search").value.trim())}`)); state.sources.forEach((source) => {if (source.id !== undefined && source.id !== null) sourceCatalog.set(String(source.id), source);}); renderSources();} catch (error) {errorPanel($("source-list"), error);} finally {renderSourcePickers();}}
  function renderAdvisors() {
    const target = clear($("advisor-list"));
    state.advisors.forEach((advisor, i) => {const card = el("article", "panel advisor-card"); card.append(el("span", "advisor-icon", String(i + 1).padStart(2, "0")), el("h2", "", advisor.name || advisor.id), el("p", "", advisor.focus || advisor.description || "")); target.append(card);});
    renderStats();
  }
  function renderCouncil(council, target) {
    const panel = el("article", "panel"); const heading = el("div", "result-title"); heading.append(el("h2", "", "Результат разбора"), badge(council.mode || "rule_based")); panel.append(heading);
    if (council.question) panel.append(el("p", "result-summary", council.question));
    if (council.verdict) panel.append(el("p", "result-summary", printable(council.verdict)));
    if (array(council.source_ids).length) {const linked = el("div", "linked-sources"); linked.append(el("h3", "", "Связанные карточки источников")); const list = el("ul", "next-steps"); council.source_ids.forEach((id) => {const item = el("li"); const source = sourceCatalog.get(String(id)); item.append(source && source.url ? safeLink(source.url, source.title || String(id), "") : document.createTextNode(source && source.title || String(id))); list.append(item);}); linked.append(list, el("p", "form-help", "Привязка карточек не подтверждает проверку полного содержания публикаций.")); panel.append(linked);} else panel.append(el("p", "form-help", "Источники не выбраны: разбор сохранён без привязки карточек."));
    const cards = el("div", "council-cards");
    for (const advisor of array(council.advisors)) {const card = el("section", "council-assessment"); card.append(el("h3", "", advisor.name || advisor.id), el("p", "", printable(advisor.assessment))); if (array(advisor.actions).length) {const list = el("ul"); advisor.actions.forEach((action) => list.append(el("li", "", printable(action)))); card.append(list);} cards.append(card);}
    panel.append(cards);
    if (array(council.next_steps).length) {panel.append(el("h3", "", "Следующие шаги")); const list = el("ol", "next-steps"); council.next_steps.forEach((step) => list.append(el("li", "", printable(step)))); panel.append(list);}
    panel.append(el("p", "result-id", `Разбор ${council.id || "—"} · ${date(council.created_at)}`));
    panel.append(rawDetails("Исходные данные разбора", council));
    target.append(panel);
  }
  function renderRun(run, target) {
    const panel = el("article", "panel"); const heading = el("div", "result-title"); const plugin = state.plugins.find((item) => item.id === run.plugin_id); heading.append(el("h2", "", plugin ? plugin.name : run.plugin_id || "Результат расчёта"), badge(run.status)); panel.append(heading);
    if (run.error) panel.append(el("p", "error-panel", typeof run.error === "object" ? run.error.message || printable(run.error) : run.error));
    const result = run.result || {};
    if (result.summary) panel.append(el("p", "result-summary", printable(result.summary)));
    if (array(result.metrics).length) {const metrics = el("div", "metrics-grid"); for (const metric of result.metrics) {const card = el("div", "metric"); card.append(el("span", "", metric.label || metric.name || "Метрика"), el("strong", "", fmt(metric.value))); if (metric.unit) card.append(el("small", "", metric.unit)); metrics.append(card);} panel.append(metrics);}
    const chart = chartFor(array(result.series)); if (chart) panel.append(chart);
    if (array(result.table).length) panel.append(dataTable(result.table));
    const limits = limitsBox(result.limitations); if (limits) panel.append(limits);
    panel.append(el("p", "result-id", `Запуск ${run.id || "—"} · ${date(run.created_at)}`), rawDetails("Параметры и происхождение расчёта", {parameters: run.parameters, provenance: run.provenance}), rawDetails("Полный результат JSON", run));
    target.append(panel);
  }
  function dataTable(rows) {
    const container = el("div", "result-table"); const wrap = el("div", "table-wrap"); const table = el("table"); const head = el("thead"); const body = el("tbody");
    const columns = [...new Set(rows.flatMap((row) => row && typeof row === "object" && !Array.isArray(row) ? Object.keys(row) : ["value"]))];
    const tr = el("tr"); columns.forEach((key) => {const th = el("th", "", label(key)); th.scope = "col"; tr.append(th);}); head.append(tr);
    rows.slice(0, 150).forEach((row) => {const tr = el("tr"); columns.forEach((key) => tr.append(el("td", "", fmt(row && typeof row === "object" ? row[key] : row)))); body.append(tr);});
    table.append(head, body); wrap.append(table); container.append(wrap); if (rows.length > 150) container.append(el("p", "form-help", `Показаны первые 150 из ${rows.length} строк. Все данные доступны в JSON и экспорте.`)); return container;
  }
  function chartFor(rawSeries) {
    const series = rawSeries.map((s) => ({name: s.name || "Ряд", points: array(s.points).filter((p) => p && typeof p.x === "number" && typeof p.y === "number" && Number.isFinite(p.x) && Number.isFinite(p.y))})).filter((s) => s.points.length);
    if (!series.length) return null;
    const all = series.flatMap((s) => s.points); let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    all.forEach((p) => {minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);});
    if (minX === maxX) {minX -= .5; maxX += .5;} if (minY === maxY) {minY -= .5; maxY += .5;} else {const pad = (maxY - minY) * .07; minY -= pad; maxY += pad;}
    const ns = "http://www.w3.org/2000/svg"; const svgEl = (tag, attrs, text) => {const node = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, String(v))); if (text !== undefined) node.textContent = String(text); return node;};
    const colors = ["#007c78", "#ab762b", "#7a64a3", "#4a82a5", "#b65d69", "#529851"];
    const box = el("div", "chart"); box.append(el("h3", "", "Результаты моделирования")); const svg = svgEl("svg", {viewBox: "0 0 640 290", role: "img", "aria-label": `График расчёта. Ряды: ${series.map((s) => s.name).join(", ")}. Точные значения приведены в таблице или полном JSON.`});
    const X = (x) => 65 + (x - minX) / (maxX - minX) * 550; const Y = (y) => 246 - (y - minY) / (maxY - minY) * 216;
    const axisFmt = (n) => Math.abs(n) >= 100000 || Math.abs(n) > 0 && Math.abs(n) < .0001 ? n.toExponential(1) : Number(n.toPrecision(4)).toLocaleString("ru-RU");
    for (let i = 0; i <= 4; i++) {const y = minY + (maxY - minY) * i / 4; svg.append(svgEl("line", {x1: 65, y1: Y(y), x2: 615, y2: Y(y)}), svgEl("text", {x: 54, y: Y(y) + 3, "text-anchor": "end"}, axisFmt(y))); const x = minX + (maxX - minX) * i / 4; svg.append(svgEl("text", {x: X(x), y: 265, "text-anchor": "middle"}, axisFmt(x)));}
    svg.append(svgEl("line", {x1: 65, y1: 246, x2: 615, y2: 246, class: "axis"}), svgEl("text", {x: 625, y: 265}, "x"), svgEl("text", {x: 53, y: 17, "text-anchor": "end"}, "y"));
    series.forEach((s, index) => {const sampled = s.points.length > 2000 ? s.points.filter((_, i) => i === s.points.length - 1 || i % Math.ceil(s.points.length / 2000) === 0) : s.points; svg.append(svgEl("polyline", {points: sampled.map((p) => `${X(p.x)},${Y(p.y)}`).join(" "), stroke: colors[index % colors.length]})); if (s.points.length === 1) svg.append(svgEl("circle", {cx: X(s.points[0].x), cy: Y(s.points[0].y), r: 3, fill: colors[index % colors.length]}));});
    box.append(svg); const legend = el("div", "chart-legend"); series.forEach((s, index) => {const item = el("span", "legend-item"); const mark = svgEl("svg", {width: 10, height: 10, viewBox: "0 0 10 10", "aria-hidden": "true"}); mark.append(svgEl("circle", {cx: 5, cy: 5, r: 4, fill: colors[index % colors.length]})); item.append(mark, document.createTextNode(s.name)); legend.append(item);}); box.append(legend, el("p", "chart-note", "Оси и единицы определяются выбранной моделью. Исходные координаты доступны в полном результате JSON.")); return box;
  }
  function renderRuns(target, runs, limit = 100) {
    clear(target);
    if (!runs.length) {empty(target, "Расчётов пока нет. Первый запуск появится здесь автоматически.", textButton("Перейти к моделированию →", () => navigate("experiments"))); return;}
    const wrap = el("div", "table-wrap"); const table = el("table"); const head = el("thead"); const header = el("tr"); ["Модуль", "Создан", "Состояние", "Результат"].forEach((title) => {const cell = el("th", "", title); cell.scope = "col"; header.append(cell);}); head.append(header); const body = el("tbody");
    runs.slice(0, limit).forEach((run) => {const row = el("tr"); const plugin = state.plugins.find((p) => p.id === run.plugin_id); const module = el("td"); module.append(el("strong", "", plugin ? plugin.name : run.plugin_id)); const time = el("td"); time.append(el("time", "", date(run.created_at))); const status = el("td"); status.append(badge(run.status)); const action = el("td"); action.append(textButton("Открыть ↗", () => {navigate("journal"); clear($("journal-detail")); renderRun(run, $("journal-detail")); $("journal-detail").scrollIntoView({behavior: "smooth", block: "start"});})); row.append(module, time, status, action); body.append(row);}); table.append(head, body); wrap.append(table); target.append(wrap);
  }
  async function loadRuns() {state.runs = items(await api("/api/runs")); renderRuns($("overview-runs"), state.runs, 5); renderRuns($("journal-runs"), state.runs);}
  function valueNode(value) {
    if (typeof value === "boolean") return el("span", value ? "badge" : "badge neutral", value ? "Да" : "Нет");
    if (typeof value === "string" && statusNames[value]) return badge(value);
    if (Array.isArray(value)) {const list = el("ul"); value.forEach((item) => list.append(el("li", "", typeof item === "object" ? printable(item) : item))); return list;}
    return el("span", "", printable(value));
  }
  function environmentCard(title, value) {
    const card = el("article", "panel environment-card"); card.append(el("h2", "", title));
    if (value && typeof value === "object" && !Array.isArray(value)) {const details = el("dl", "detail-list"); Object.entries(value).forEach(([key, item]) => {const dd = el("dd"); dd.append(valueNode(item)); details.append(el("dt", "", label(key)), dd);}); card.append(details);}
    else {const content = el("div", "flat-text"); content.append(valueNode(value)); card.append(content);}
    return card;
  }
  function renderEnvironment(env) {
    $("environment-raw").textContent = printable(env);
    const target = clear($("environment-content"));
    if (env.snapshot_kind === "historical_build_report") {
      target.append(environmentCard("Текущая среда", env.live_runtime));
      const historical = el("details", "panel environment-card");
      historical.append(el("summary", "", "Архивная проверка сборки — не состояние этого ПК"));
      historical.append(el("p", "", env.notice));
      const report = el("pre"); report.textContent = printable(env.historical_snapshot);
      historical.append(report); target.append(historical);
      return;
    }
    if (env.packages && env.programs && env.runtime) {
      target.append(environmentCard("Локальная среда", {python: env.python, checked_at: env.checked_at, ...env.runtime}));
      target.append(environmentCard("Изоляция вычислений", {namespace_isolation_available: env.namespace_isolation_available, description: env.namespace_isolation_available ? "Сведения о доступности изоляции приведены в проверке окружения." : "Отдельный процесс ограничивает выполнение, но не является защищённой песочницей. Запуск недоверенных модулей требует отдельной среды."}));
      const credentials = {};
      if (env.credentials_present) Object.entries(env.credentials_present).forEach(([key, value]) => {credentials[key === "OPENAI_API_KEY" ? "Ключ OpenAI API задан" : key === "META_LLM_API_KEY" ? "Ключ провайдера задан" : key === "META_LLM_BASE_URL" ? "Адрес провайдера задан" : key === "META_LLM_MODEL" ? "Модель выбрана" : key] = value;});
      target.append(environmentCard("Подключение языковых моделей", credentials));
      for (const [key, title] of [["github", "GitHub"], ["context7", "Context7 · документация"], ["rosalind", "Rosalind"], ["personal_skill", "Навык Meta-Harness"]]) if (env[key]) target.append(environmentCard(title, env[key]));
      target.append(environmentCard("Программные инструменты", {available_packages: Object.keys(env.packages).filter((key) => env.packages[key]), unavailable_packages: Object.keys(env.packages).filter((key) => !env.packages[key]), available_programs: Object.keys(env.programs).filter((key) => env.programs[key]), unavailable_programs: Object.keys(env.programs).filter((key) => !env.programs[key])}));
      return;
    }
    if (Array.isArray(env)) env.forEach((entry) => target.append(environmentCard(entry.name || entry.id || "Возможность", entry)));
    else Object.entries(env).forEach(([key, value]) => {if (["capabilities", "components", "services"].includes(key) && Array.isArray(value)) value.forEach((entry) => target.append(environmentCard(entry.name || entry.id || label(key), entry))); else target.append(environmentCard(label(key), value));});
  }
  let environmentLoading = false;
  async function loadEnvironment() {
    if (environmentLoading) return; environmentLoading = true;
    const results = await Promise.allSettled([api("/api/environment"), api("/api/roadmap")]);
    if (results[0].status === "fulfilled") renderEnvironment(results[0].value); else errorPanel($("environment-content"), results[0].reason);
    if (results[1].status === "fulfilled") {const target = clear($("roadmap-content")); const roadmap = items(results[1].value); roadmap.forEach((item, index) => {const card = el("article", "panel roadmap-card"); card.append(badge(item.status || "planned"), el("h3", "", item.title || item.name || `Этап ${index + 1}`)); for (const [key, value] of Object.entries(item)) {if (["id", "title", "name", "status"].includes(key)) continue; if (Array.isArray(value)) {card.append(el("p", "", label(key))); const list = el("ul"); value.forEach((text) => list.append(el("li", "", printable(text)))); card.append(list);} else card.append(el("p", "", `${label(key)}: ${printable(value)}`));} target.append(card);}); if (!roadmap.length) empty(target, "План развития пока не заполнен.");} else errorPanel($("roadmap-content"), results[1].reason);
    environmentLoading = false;
  }
  function renderAudit(data) {
    const valid = $("audit-valid"); valid.className = data.valid === true ? "badge" : data.valid === false ? "badge error" : "badge neutral"; valid.textContent = data.valid === true ? "Целостность подтверждена" : data.valid === false ? "Нарушение целостности" : "Статус неизвестен";
    const target = clear($("audit-list")); const events = items(data); if (!events.length) {empty(target, "В журнале пока нет событий."); return;}
    const wrap = el("div", "table-wrap"); const table = el("table"); const head = el("thead"); const tr = el("tr"); ["Время", "Событие", "Детали"].forEach((title) => {const th = el("th", "", title); th.scope = "col"; tr.append(th);}); head.append(tr); const body = el("tbody");
    events.slice().reverse().slice(0, 150).forEach((event) => {const row = el("tr"); row.append(el("td", "", date(event.timestamp)), el("td", "", event.event)); const cell = el("td"); const detail = el("details", "audit-details"); detail.append(el("summary", "", "Запись и хеш"), el("pre", "", printable({id: event.id, details: event.details, hash: event.hash, prev_hash: event.prev_hash}))); cell.append(detail); row.append(cell); body.append(row);}); table.append(head, body); wrap.append(table); target.append(wrap); if (events.length > 150) target.append(el("p", "form-help", "Показаны последние 150 событий. Полный журнал доступен в экспорте."));
  }
  async function loadJournal() {const results = await Promise.allSettled([loadRuns(), api("/api/audit")]); if (results[0].status === "rejected") errorPanel($("journal-runs"), results[0].reason); if (results[1].status === "fulfilled") renderAudit(results[1].value); else {errorPanel($("audit-list"), results[1].reason); $("audit-valid").textContent = "Не удалось проверить"; $("audit-valid").className = "badge warning";}}
  function renderWorkflow(workflow, target) {const panel = el("article", "panel"); const heading = el("div", "result-title"); heading.append(el("h2", "", "Результат исследовательского цикла"), badge(workflow.status)); panel.append(heading); if (workflow.summary) panel.append(el("p", "result-summary", printable(workflow.summary))); panel.append(el("p", "result-id", `Цикл ${workflow.id || "—"} · ${date(workflow.created_at)}`)); target.append(panel); if (workflow.council) renderCouncil(workflow.council, target); if (workflow.run) renderRun(workflow.run, target); target.append(rawDetails("Полные данные цикла", workflow));}
  async function loadWorkflows() {try {state.workflows = items(await api("/api/workflows")); const target = clear($("workflow-history")); if (!state.workflows.length) {empty(target, "Циклов пока нет. Результат первого запуска будет сохранён здесь."); return;} state.workflows.slice(0, 30).forEach((workflow) => {const row = el("div", "empty-inline"); const info = el("div"); info.append(el("h3", "", workflow.question || workflow.council && workflow.council.question || `Цикл ${workflow.id}`), el("p", "", `${date(workflow.created_at)} · ${statusNames[workflow.status] || workflow.status || "—"}`)); row.append(info, textButton("Открыть ↗", () => {clear($("workflow-result")); renderWorkflow(workflow, $("workflow-result")); $("workflow-result").scrollIntoView({behavior: "smooth"});})); target.append(row);});} catch (error) {errorPanel($("workflow-history"), error);}}
  async function withBusy(form, operation) {const button = form.querySelector("button[type=submit]"); const text = button.textContent; button.disabled = true; button.textContent = "Выполняется…"; form.setAttribute("aria-busy", "true"); try {await operation();} catch (error) {toast(error.message, true); throw error;} finally {button.disabled = false; button.textContent = text; form.removeAttribute("aria-busy");}}
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
  document.querySelectorAll("[data-goto]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.goto)));
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1), false));
  $("experiment-plugin").addEventListener("change", () => renderParameters("experiment"));
  $("workflow-plugin").addEventListener("change", () => renderParameters("workflow"));
  $("source-search-form").addEventListener("submit", (event) => {event.preventDefault(); loadSources();});
  $("source-search").addEventListener("search", loadSources);
  $("toggle-source-form").addEventListener("click", () => {const panel = $("source-form-panel"); panel.hidden = !panel.hidden; $("toggle-source-form").setAttribute("aria-expanded", String(!panel.hidden)); if (!panel.hidden) $("source-form").elements.title.focus();});
  $("source-form").addEventListener("submit", async (event) => {event.preventDefault(); try {await withBusy(event.currentTarget, async () => {const form = $("source-form"); const body = Object.fromEntries(new FormData(form)); const url = new URL(body.url); if (!["https:", "http:"].includes(url.protocol)) throw new Error("Укажите ссылку http или https."); await api("/api/sources", body); form.reset(); $("source-form-panel").hidden = true; $("toggle-source-form").setAttribute("aria-expanded", "false"); $("source-search").value = ""; await Promise.allSettled([loadSources(), loadStatus()]); toast("Источник добавлен в реестр.");});} catch (_) { /* Error is shown by withBusy. */ }});
  $("council-form").addEventListener("submit", async (event) => {event.preventDefault(); const target = $("council-result"); try {await withBusy(event.currentTarget, async () => {loading(target, "Советники разбирают исследовательский вопрос…"); const council = await api("/api/council", {question: $("council-question").value.trim(), source_ids: selectedSources("council")}); clear(target); renderCouncil(council, target); toast("Разбор сохранён.");});} catch (error) {errorPanel(target, error);}});
  $("experiment-form").addEventListener("submit", async (event) => {event.preventDefault(); const target = $("experiment-result"); try {await withBusy(event.currentTarget, async () => {const body = {plugin_id: $("experiment-plugin").value, parameters: parameters("experiment")}; if (!body.plugin_id) throw new Error("Нет доступных модулей для запуска."); loading(target, "Выполняется расчёт в отдельном процессе…"); const run = await api("/api/run", body); clear(target); renderRun(run, target); await Promise.allSettled([loadRuns(), loadStatus()]); toast(run.status === "failed" || run.status === "error" ? "Расчёт завершился с ошибкой; сведения сохранены." : "Расчёт завершён и сохранён.", run.status === "failed" || run.status === "error");});} catch (error) {errorPanel(target, error);}});
  $("workflow-form").addEventListener("submit", async (event) => {event.preventDefault(); const target = $("workflow-result"); try {await withBusy(event.currentTarget, async () => {const body = {question: $("workflow-question").value.trim(), plugin_id: $("workflow-plugin").value, parameters: parameters("workflow"), source_ids: selectedSources("workflow")}; if (!body.plugin_id) throw new Error("Нет доступных модулей для запуска."); loading(target, "Выполняется разбор вопроса и расчёт модели…"); const workflow = await api("/api/workflow", body); clear(target); renderWorkflow(workflow, target); await Promise.allSettled([loadWorkflows(), loadRuns(), loadStatus()]); toast("Исследовательский цикл сохранён.");});} catch (error) {errorPanel(target, error);}});
  $("refresh-workflows").addEventListener("click", loadWorkflows);
  $("refresh-environment").addEventListener("click", loadEnvironment);
  $("refresh-journal").addEventListener("click", loadJournal);
  async function start() {
    navigate(location.hash.slice(1) || "overview", false);
    const results = await Promise.allSettled([loadStatus(), api("/api/plugins"), api("/api/advisors"), loadSources()]);
    if (results[1].status === "fulfilled") {state.plugins = items(results[1].value); renderPlugins();} else {errorPanel($("overview-plugins"), results[1].reason); toast(results[1].reason.message, true);}
    if (results[2].status === "fulfilled") {state.advisors = items(results[2].value); renderAdvisors();} else errorPanel($("advisor-list"), results[2].reason);
    try {await loadRuns();} catch (error) {errorPanel($("overview-runs"), error);}
  }
  start();
})();

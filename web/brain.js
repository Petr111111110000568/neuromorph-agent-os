(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const arr = value => Array.isArray(value) ? value : [];
  const obj = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const printable = value => value == null ? "—" : typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const fmt = value => finite(value) ? value.toLocaleString("ru-RU", { maximumSignificantDigits: 5 }) : printable(value);
  const time = value => { const date = new Date(value); return value && !Number.isNaN(date.getTime()) ? date.toLocaleString("ru-RU") : "Время не указано"; };
  const terminal = new Set(["completed", "partial", "failed", "cancelled"]);
  const names = { created: "Создана", awaiting_workers: "Ожидание workers", queued: "В очереди", running: "Выполняется", completed: "Завершена", partial: "Частичный результат", failed: "Ошибка", cancelled: "Отменена", cancelling: "Отмена выполняется", pending: "Ожидание", leased: "Задача у worker", accepted: "Конверт проверен", rejected: "Конверт отклонён" };
  const pluginNames = { kan_benchmark: "KAN · функции на рёбрах", cortical_sequence: "Контекстная память последовательностей", structural_plasticity: "Структурная пластичность" };
  const roles = {
    thalamus: ["Маршрутизация входа", "Таламус", "Принять запрос и передать его в исследовательский контур."],
    association_cortex: ["Связать источники", "Ассоциативная кора", "Отобрать карточки материалов, связанные с вопросом."],
    hippocampus: ["Извлечь память", "Гиппокамп", "Найти сохранённые запуски и их происхождение."],
    prefrontal: ["Составить план", "Префронтальная кора", "Записать вычислительные задачи, параметры и область применимости."],
    basal_ganglia: ["Распределить задачи", "Базальные ганглии", "Поставить выбранные ветви в очередь workers."],
    cortex_kan: ["Вычислить KAN", "Специализированная вычислительная область", "Сравнить обучаемые функции на рёбрах с контрольными моделями."],
    cortex_sequence: ["Проверить контекстную память", "Кортикальная аналогия", "Предсказывать последовательности с учётом контекста."],
    cortex_plasticity: ["Исследовать перестройку связей", "Структурная пластичность", "Сравнить фиксированные и перестраиваемые связи."],
    cerebellum: ["Проверить исполнение", "Мозжечок", "Сверить вычислительный результат и его происхождение."],
    anterior_cingulate: ["Выявить ограничения", "Передняя поясная кора", "Зафиксировать ошибки, несогласованность и границы вывода."],
    global_workspace: ["Собрать общий результат", "Общее рабочее пространство", "Объединить сводки, сохранив различия между задачами."],
  };
  const branchRole = { kan_benchmark: "cortex_kan", cortical_sequence: "cortex_sequence", structural_plasticity: "cortex_plasticity" };
  const state = { status: null, sessions: [], current: null, plugins: [], labRun: null, labBusy: false, sessionBusy: false, timer: null, pollCount: 0, polling: false, selection: 0, ready: false };
  function el(tag, className = "", text) { const item = document.createElement(tag); if (className) item.className = className; if (text != null) item.textContent = String(text); return item; }
  function svg(tag, attributes = {}, text) { const item = document.createElementNS("http://www.w3.org/2000/svg", tag); Object.entries(attributes).forEach(([key, value]) => item.setAttribute(key, String(value))); if (text != null) item.textContent = String(text); return item; }
  function badge(value) { return el("span", `badge ${["failed", "cancelled", "rejected"].includes(value) ? "error" : ["partial", "awaiting_workers", "queued"].includes(value) ? "warning" : ["completed", "accepted"].includes(value) ? "" : "neutral"}`, names[value] || value || "Не указан"); }
  function connection(message, error = false, busy = false) { $("brain-status").textContent = message; $("brain-dot").className = `status-dot${error ? " error" : busy ? " loading" : ""}`; }
  function feedback(id, message, error = false) { const box = el("div", error ? "error-panel" : "global-status-note", message); if (error) box.setAttribute("role", "alert"); $(id).replaceChildren(box); }
  function globalError(message) { $("global-error").textContent = message || ""; $("global-error").hidden = !message; }
  async function api(path, body) {
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), body && path === "/api/run" ? 45000 : 20000);
    try {
      const response = await fetch(path, { method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal });
      if (!(response.headers.get("content-type") || "").includes("application/json")) throw new Error(`Ожидался JSON, получен ответ ${response.status}. Проверьте запуск версии Meta-Harness с исследовательским контуром.`);
      const data = await response.json(); if (!response.ok) throw new Error(data.error?.message || printable(data.error) || `HTTP ${response.status}`); return data;
    } catch (error) { if (error.name === "AbortError") throw new Error("Истекло время ожидания. Действие могло завершиться на сервере; обновите список перед повторным запуском."); throw error; } finally { clearTimeout(timeout); }
  }
  function details(label, value) { const box = el("details", "brain-details"); box.append(el("summary", "", label), el("pre", "", printable(value))); return box; }
  function safeLink(url, label) { try { const parsed = new URL(url); if (["http:", "https:"].includes(parsed.protocol)) { const link = el("a", "", label); link.href = parsed.href; link.target = "_blank"; link.rel = "noopener noreferrer"; return link; } } catch (_) { /* Non-web addresses remain inert text. */ } return el("span", "", label); }
  function download(value, name) { const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json;charset=utf-8" })); const link = el("a"); link.href = url; link.download = `${name.replace(/[^a-zA-Z0-9_-]/g, "_")}.json`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  function setTab(name, focus = false) { ["circuit", "lab"].forEach(id => { const active = id === name; $(`tab-${id}`).setAttribute("aria-selected", String(active)); $(`tab-${id}`).tabIndex = active ? 0 : -1; $(`panel-${id}`).hidden = !active; }); if (focus) $(`tab-${name}`).focus(); history.replaceState(null, "", `#${name}`); }
  function renderSessions() {
    const rows = state.sessions.slice(0, 30).map(session => { const button = el("button", "session-choice"); button.type = "button"; button.setAttribute("aria-current", String(session.id === state.current?.id)); button.append(el("strong", "", session.question || session.id), badge(session.status), el("small", "", `${session.id} · seed ${session.seed ?? "—"}`)); button.addEventListener("click", () => selectSession(session.id)); return button; });
    $("session-list").replaceChildren(...(rows.length ? rows : [el("p", "muted", "Сессий пока нет. Создайте первый вычислительный план.")]));
  }
  function regionalList() { const value = state.status?.regions; return Array.isArray(value) ? value : Object.entries(obj(value)).map(([id, info]) => ({ id, ...obj(info) })); }
  function visibleBranches() {
    const session = state.current; if (!session) return []; const completed = arr(session.branches); const jobs = arr(session.job_details);
    return arr(session.plan).map(plan => { const received = completed.find(branch => branch.plugin_id === plan.plugin_id); if (received) return received; const jobId = session.jobs?.[plan.plugin_id]; const job = jobs.find(item => item.id === jobId); return { plugin_id: plan.plugin_id, job_id: jobId, status: job?.status || (session.status === "cancelled" ? "cancelled" : "pending"), not_submitted_before_cancellation: !jobId && session.status === "cancelled", scope: plan.scope, parameters: plan.parameters, error: job?.error, worker_id: job?.worker_id, limitations: plan.limitations }; });
  }
  function renderRegions() {
    const regionItems = regionalList(); const events = arr(state.current?.events); const branches = visibleBranches();
    $("brain-regions").replaceChildren(...regionItems.map((region, index) => {
      const id = region.id || region.region; const fallback = roles[id] || [region.name || id, region.analogy || "Условная аналогия", region.description || "Программная роль"];
      const matches = events.filter(event => event.region === id); const latest = matches[matches.length - 1]; const branch = branches.find(item => branchRole[item.plugin_id] === id); const active = branch && ["running", "leased"].includes(branch.status);
      const card = el("article", `brain-region${matches.length ? " has-events" : ""}`); card.dataset.active = String(Boolean(active)); card.append(el("span", "region-order", `${String(index + 1).padStart(2, "0")} / ${id}`), el("h3", "", fallback[0]), el("p", "region-analogy", `Биоаналогия: ${fallback[1]}`), el("p", "region-role", region.function || region.responsibility || fallback[2]));
      const footer = el("div", "region-footer"); footer.append(el("strong", "", `${matches.length} событий`), el("span", "", branch ? names[branch.status] || branch.status : latest ? latest.type || "Действие записано" : "Нет событий сессии")); card.append(footer); return card;
    }));
    if (!regionItems.length) $("brain-regions").append(el("p", "brain-empty", "Сервер пока не вернул описание ролей."));
    const edges = arr(state.status?.connections); $("role-connections").replaceChildren(edges.length ? dataTable(edges, 60) : el("p", "muted", "Связи не указаны в статусе сервера."));
  }
  function metrics(items) { const region = el("div", "metrics-grid"); arr(items).slice(0, 18).forEach(metric => { const card = el("div", "metric"); card.append(el("span", "", metric.label || metric.name || "Метрика"), el("strong", "", fmt(metric.value))); if (metric.unit) card.append(el("small", "", metric.unit)); region.append(card); }); return region; }
  function dataTable(values, maxRows = 120) {
    const rows = arr(values); const columns = [...new Set(rows.slice(0, maxRows).flatMap(row => Object.keys(obj(row))))].slice(0, 16); const wrap = el("div", "table-wrap lab-table"); wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "Таблица результатов с горизонтальной прокруткой");
    const table = el("table"); const head = el("thead"); const header = el("tr"); columns.forEach(key => { const cell = el("th", "", key); cell.scope = "col"; header.append(cell); }); head.append(header); const body = el("tbody");
    rows.slice(0, maxRows).forEach(row => { const line = el("tr"); columns.forEach(key => line.append(el("td", "", fmt(row[key])))); body.append(line); }); table.append(head, body); wrap.append(table);
    if (rows.length > maxRows) wrap.append(el("p", "form-help", `Показаны ${maxRows} из ${rows.length} строк. Полный результат доступен в JSON.`)); return wrap;
  }
  function limits(values) { const list = el("ul", "result-limitations"); [...new Set(arr(values).map(printable))].forEach(value => list.append(el("li", "", value))); return list; }
  function renderBranches() {
    const branches = visibleBranches(); $("branch-count").textContent = `${branches.length} ветвей · метрики каждой задачи отдельно`;
    $("branch-results").replaceChildren(...branches.map(branch => {
      const panel = el("article", "panel brain-branch"); const heading = el("div", "result-title"); heading.append(el("h3", "", pluginNames[branch.plugin_id] || branch.plugin_id), badge(branch.status)); panel.append(heading, el("p", "branch-meta", `Job ${branch.job_id || "—"}`));
      const result = obj(branch.result); if (result.summary) panel.append(el("p", "result-summary", result.summary)); else panel.append(el("p", "result-summary", branch.not_submitted_before_cancellation ? "Ветвь отменена до постановки задачи в очередь; вычисление не запускалось." : branch.status === "cancelled" ? "Задача отменена. Результат отменённой задачи не принимается контуром." : "Результат вычисления ещё не получен."));
      if (arr(result.metrics).length) panel.append(metrics(result.metrics)); if (branch.error) panel.append(el("p", "error-panel", printable(branch.error)));
      if (branch.verification) panel.append(details("Проверка результата", branch.verification)); if (arr(branch.limitations).length) panel.append(limits(branch.limitations));
      if (Object.keys(result).length) { const button = el("button", "text-button", "Открыть результат в лаборатории →"); button.type = "button"; button.addEventListener("click", () => { const run = { id: branch.run_id || branch.job_id, plugin_id: branch.plugin_id, status: branch.status, result, provenance: branch.provenance }; state.labRun = run; renderLabResult(run); setTab("lab", true); }); panel.append(button); }
      panel.append(details("Происхождение и данные ветви", branch)); return panel;
    })); if (!branches.length) $("branch-results").append(el("p", "brain-empty", "Здесь будут статусы jobs, метрики, проверки происхождения и ограничения каждого результата."));
  }
  function renderEvidence() {
    const box = $("session-evidence"); box.replaceChildren(); const sources = arr(state.current?.sources); const memory = arr(state.current?.memory);
    box.append(el("h3", "", "Отобранные карточки источников")); const sourcesList = el("ul"); sources.forEach(source => { const item = el("li"); item.append(safeLink(source.url, source.title || source.id), el("span", "evidence-kind", source.selection_reason || "Основание отбора не указано")); sourcesList.append(item); }); box.append(sources.length ? sourcesList : el("p", "form-help", "В этой сессии источники ещё не связаны."));
    box.append(el("p", "form-help", "Связь с карточкой источника не подтверждает чтение полной публикации."), el("h3", "", "Извлечённая память")); const memoryList = el("ul"); memory.forEach(record => { const item = el("li"); item.append(el("span", "", record.summary || record.id), el("span", "evidence-kind", `${record.kind || "Запись"} · ${record.plugin_id || record.id || "—"}`), details("Происхождение записи", record.provenance || record)); memoryList.append(item); }); box.append(memory.length ? memoryList : el("p", "form-help", "Подходящих сохранённых запусков в памяти не найдено."));
  }
  function renderEvents() {
    const events = arr(state.current?.events); const children = events.slice(-100).reverse().map(event => { const row = el("details", "event-row"); row.append(el("summary", "", `#${event.sequence ?? event.id} · ${roles[event.region]?.[0] || event.region || "Контур"} · ${event.type || "Действие"}`), el("time", "", time(event.at)), el("pre", "", printable({ input: event.input, output: event.output, provenance: event.provenance }))); return row; });
    $("session-events").replaceChildren(...(children.length ? children : [el("p", "muted", "Событий пока нет.")])); if (events.length > 100) $("session-events").append(el("p", "form-help", "Показаны последние 100 событий; все записи доступны в JSON."));
  }
  function setSession(session) {
    state.current = session; const listed = state.sessions.findIndex(item => item.id === session.id); const summary = { id: session.id, question: session.question, seed: session.seed, status: session.status }; if (listed >= 0) state.sessions[listed] = summary; else state.sessions.unshift(summary); $("current-question").textContent = session.question || session.id; $("current-meta").textContent = `${session.id} · seed ${session.seed} · ${session.data_class || "public"}`;
    const currentBadge = badge(session.status); $("current-status").className = currentBadge.className; $("current-status").textContent = currentBadge.textContent;
    $("workspace-summary").textContent = session.workspace?.summary || "План сохранён. Ожидаются вычислительные результаты.";
    const branchItems = visibleBranches(); const done = branchItems.filter(branch => terminal.has(branch.status) || ["accepted", "rejected"].includes(branch.status)).length; $("session-progress").max = Math.max(branchItems.length, 1); $("session-progress").value = done; $("session-progress-text").textContent = `${done} из ${branchItems.length} ветвей завершили выполнение`;
    $("tick-session").disabled = state.sessionBusy || state.polling; $("cancel-session").disabled = state.sessionBusy || state.polling || terminal.has(session.status); $("export-session").disabled = false;
    const issues = arr(session.issues); $("session-issues").replaceChildren(); if (issues.length) { const wrap = el("div", "brain-issues"); const list = el("ul"); issues.forEach(issue => list.append(el("li", "", typeof issue === "string" ? issue : issue.message || printable(issue)))); wrap.append(list); $("session-issues").append(wrap); }
    $("session-critique").replaceChildren(...arr(session.critique).map(critique => { const box = el("details", "brain-details"); box.append(el("summary", "", `Критический разбор · ${pluginNames[critique.plugin_id] || critique.plugin_id || "вычислительная ветвь"}`)); if (critique.assessment) box.append(el("p", "result-summary", printable(critique.assessment))); if (arr(critique.comparisons).length) box.append(dataTable(critique.comparisons)); box.append(el("pre", "", printable(critique))); return box; }));
    $("session-json").textContent = printable(session); renderRegions(); renderBranches(); renderEvidence(); renderEvents(); renderSessions();
    if (terminal.has(session.status)) { stopPoll(); $("poll-note").textContent = "Сессия завершила выполнение. Автоматический опрос остановлен."; }
  }
  function stopPoll() { if (state.timer !== null) clearTimeout(state.timer); state.timer = null; }
  function schedulePoll() {
    stopPoll(); if (!state.current || terminal.has(state.current.status) || !$("auto-poll").checked || document.hidden || state.polling || state.sessionBusy) return;
    if (state.pollCount >= 120) { $("poll-note").textContent = "Достигнут предел 120 обновлений. Очередь продолжает работать; нажмите «Обновить и проверить результаты» для продолжения наблюдения."; return; }
    $("poll-note").textContent = `Автообновление каждые 3 секунды после ответа · ${state.pollCount} из 120. При скрытии вкладки опрос приостанавливается.`; state.timer = setTimeout(() => tick(false), 3000);
  }
  async function reloadSessions() { const result = await api("/api/brain/sessions"); state.sessions = arr(result.items); renderSessions(); }
  async function selectSession(id) {
    stopPoll(); const selection = ++state.selection; state.pollCount = 0;
    try { const session = await api(`/api/brain/session?id=${encodeURIComponent(id)}`); if (selection !== state.selection) return; setSession(session); globalError(""); schedulePoll(); } catch (error) { if (selection === state.selection) globalError(error.message); }
  }
  async function tick(manual) {
    if (!state.current || state.polling || state.sessionBusy) return; stopPoll(); if (manual) state.pollCount = 0;
    const id = state.current.id; const selection = state.selection; state.polling = true; $("tick-session").disabled = true; $("cancel-session").disabled = true;
    try { const session = await api("/api/brain/tick", { id }); state.pollCount += 1; if (selection !== state.selection || id !== state.current?.id) return; setSession(session); connection(terminal.has(session.status) ? "Результаты сохранены" : "Контур подключён"); globalError(""); }
    catch (error) { if (selection === state.selection) { globalError(error.message); connection("Ошибка обновления", true); $("auto-poll").checked = false; $("poll-note").textContent = "Опрос остановлен после ошибки. Проверьте сервер и обновите сессию вручную."; } }
    finally { state.polling = false; if (state.current) { $("tick-session").disabled = state.sessionBusy; $("cancel-session").disabled = state.sessionBusy || terminal.has(state.current.status); } schedulePoll(); }
  }
  function schema(plugin) { return plugin?.parameters || plugin?.input_schema || plugin?.inputSchema || {}; }
  function renderParameters() {
    const plugin = state.plugins.find(item => item.id === $("lab-plugin").value); const spec = schema(plugin); $("lab-description").textContent = plugin?.description || "";
    $("lab-parameters").replaceChildren(...Object.entries(spec.properties || {}).map(([key, property]) => {
      const wrap = el("div"); const label = el("label", "", property.title || key); const id = `lab-parameter-${key}`; label.htmlFor = id; let input;
      if (arr(property.enum).length) { input = el("select"); property.enum.forEach(value => { const option = el("option", "", printable(value)); option.value = String(value); input.append(option); }); }
      else { input = el("input"); input.type = ["number", "integer"].includes(property.type) ? "number" : property.type === "boolean" ? "checkbox" : "text"; if (input.type === "number") { input.step = property.type === "integer" ? "1" : "any"; if (property.minimum != null) input.min = String(property.minimum); if (property.maximum != null) input.max = String(property.maximum); } }
      input.id = id; input.dataset.parameter = key; input.dataset.type = property.type || "string"; if (property.type === "boolean") { input.checked = property.default === true; label.className = "checkbox-label"; label.replaceChildren(input, document.createTextNode(property.title || key)); } else { if (property.default != null) input.value = String(property.default); input.required = arr(spec.required).includes(key) || property.default !== undefined; }
      wrap.append(label); if (property.type !== "boolean") wrap.append(input); if (property.description) wrap.append(el("p", "form-help", property.description)); return wrap;
    }));
  }
  function getParameters() { const values = {}; $("lab-parameters").querySelectorAll("[data-parameter]").forEach(input => { if (input.dataset.type === "boolean") values[input.dataset.parameter] = input.checked; else if (input.value !== "") { if (["number", "integer"].includes(input.dataset.type)) { const value = Number(input.value); if (!Number.isFinite(value) || input.dataset.type === "integer" && !Number.isInteger(value)) throw new Error(`Проверьте число ${input.dataset.parameter}.`); values[input.dataset.parameter] = value; } else values[input.dataset.parameter] = input.value; } }); return values; }
  function chart(series, title, color = "#16866d") {
    const points = arr(series).filter(point => finite(point.x) && finite(point.y)).slice(0, 2000); const box = el("section", "chart"); box.append(el("h3", "", title)); if (!points.length) { box.append(el("p", "form-help", "Нет числовых точек для графика.")); return box; }
    const xMin = Math.min(...points.map(point => point.x)); const xMax = Math.max(...points.map(point => point.x)); const yMin = Math.min(...points.map(point => point.y)); const yMax = Math.max(...points.map(point => point.y)); const xRange = xMax - xMin || 1; const yRange = yMax - yMin || 1; const X = value => 60 + (value - xMin) / xRange * 420; const Y = value => 207 - (value - yMin) / yRange * 160;
    const drawing = svg("svg", { viewBox: "0 0 515 250", role: "img", "aria-label": `${title}. Диапазон X ${fmt(xMin)}–${fmt(xMax)}, Y ${fmt(yMin)}–${fmt(yMax)}; ${points.length} точек.` }); drawing.append(svg("title", {}, title));
    for (let index = 0; index <= 4; index += 1) { const x = xMin + xRange * index / 4; const y = yMin + yRange * index / 4; drawing.append(svg("line", { x1: 60, y1: Y(y), x2: 480, y2: Y(y) }), svg("text", { x: 51, y: Y(y) + 3, "text-anchor": "end" }, fmt(y)), svg("text", { x: X(x), y: 228, "text-anchor": "middle" }, fmt(x))); }
    drawing.append(svg("polyline", { points: points.map(point => `${X(point.x)},${Y(point.y)}`).join(" "), stroke: color, fill: "none", "stroke-width": 2 })); if (points.length === 1) drawing.append(svg("circle", { cx: X(points[0].x), cy: Y(points[0].y), r: 4, fill: color })); box.append(drawing); return box;
  }
  function edgeFunctions(result) {
    const functions = arr(result.model?.edge_functions); if (!functions.length) return null; const section = el("section", "lab-special lab-curves"); section.append(el("h3", "", "Обученная функция выбранного ребра"), el("p", "", `Архитектура: ${arr(result.model.architecture).join(" → ")}; seed ${result.model.visualized_replicate_seed ?? "—"}. Это функция ребра на своей сетке, а не карта биологических связей.`));
    const label = el("label", "", "Выбрать ребро"); const select = el("select"); const unique = `edge-choice-${String(state.labRun?.id || "run").replace(/[^a-zA-Z0-9_-]/g, "_")}`; select.id = unique; label.htmlFor = unique;
    functions.forEach((edge, index) => { const option = el("option", "", `${edge.source} → ${edge.target} · ${edge.basis}`); option.value = String(index); select.append(option); }); const plot = el("div"); const update = () => { const edge = functions[Number(select.value)]; if (edge) plot.replaceChildren(chart(edge.points, `${edge.source} → ${edge.target} · ${edge.basis}`)); }; select.addEventListener("change", update); section.append(label, select, plot); update(); return section;
  }
  function corticalActivity(result) {
    const trial = arr(result.trials)[0]; const activity = arr(trial?.activity).slice(0, 32); if (!activity.length) return null; const section = el("section", "lab-special"); section.append(el("h3", "", "Контекстные клетки: записанная активность"), el("p", "", `Показаны ${activity.length} записанных шагов первого повтора, seed ${trial.seed ?? "—"}. Зелёный квадрат — активная клетка; фиолетовая рамка — предсказанная до входа. Строки содержат только участвовавшие клетки.`));
    const cells = [...new Set(activity.flatMap(step => [...arr(step.active_cells), ...arr(step.predicted_before_input)]))].filter(Number.isInteger).sort((a, b) => a - b).slice(0, 128); const nodes = arr(trial.state?.nodes); const cellSize = 19; const height = cells.length * cellSize + 65; const drawing = svg("svg", { viewBox: `0 0 ${activity.length * 28 + 94} ${height}`, role: "img", "aria-label": `Активность ${cells.length} контекстных клеток за ${activity.length} шагов.` }); drawing.append(svg("title", {}, "Активные и предсказанные контекстные клетки"));
    activity.forEach((step, column) => { drawing.append(svg("text", { x: 80 + column * 28, y: 17, "text-anchor": "middle" }, step.symbol), svg("text", { x: 80 + column * 28, y: 32, "text-anchor": "middle" }, column + 1)); });
    cells.forEach((cell, row) => { const item = nodes.find(value => value.id === cell); drawing.append(svg("text", { x: 58, y: 56 + row * cellSize, "text-anchor": "end" }, `${cell}${item?.symbol ? `·${item.symbol}` : ""}`)); activity.forEach((step, column) => { const active = arr(step.active_cells).includes(cell); const predicted = arr(step.predicted_before_input).includes(cell); const rect = svg("rect", { x: 73 + column * 28, y: 44 + row * cellSize, width: 14, height: 14, rx: 2, fill: active ? "#208873" : "#f0f4ec", stroke: predicted ? "#8b5eb0" : "#dce7d9", "stroke-width": predicted ? 2 : 1 }); rect.append(svg("title", {}, `Шаг ${column + 1}, ${step.symbol}; клетка ${cell}; активна: ${active ? "да" : "нет"}; предсказана: ${predicted ? "да" : "нет"}; burst-колонок: ${arr(step.burst_columns).length}`)); drawing.append(rect); }); });
    const wrap = el("div", "chart"); wrap.append(drawing); section.append(wrap, details("Активность и структура по шагам", { state: trial.state, activity })); return section;
  }
  function renderLabResult(run) {
    const result = obj(run.result); const panel = el("article", "panel"); const title = el("div", "result-title"); title.append(el("h2", "", pluginNames[run.plugin_id] || run.plugin_id), badge(run.status)); panel.append(title, el("p", "branch-meta", `Запуск ${run.id || "—"} · ${run.plugin_id}`));
    if (run.error) panel.append(el("p", "error-panel", printable(run.error))); if (result.summary) panel.append(el("p", "result-summary", result.summary)); if (arr(result.metrics).length) panel.append(metrics(result.metrics));
    const series = arr(result.series); if (series.length) { const grid = el("div", "brain-chart-grid"); series.slice(0, 12).forEach((item, index) => grid.append(chart(item.points, item.name || `Ряд ${index + 1}`, index % 2 ? "#6d73b1" : "#16866d"))); panel.append(grid, el("p", "form-help", "Каждый график использует собственный диапазон осей; сравнивайте также численные показатели.")); }
    const edges = edgeFunctions(result); if (edges) panel.append(edges); const activity = corticalActivity(result); if (activity) panel.append(activity);
    if (arr(result.table).length) panel.append(el("h3", "", "Показатели отдельных повторов"), dataTable(result.table)); const plugin = state.plugins.find(item => item.id === run.plugin_id); const restrictions = [...arr(plugin?.limitations), ...arr(result.limitations)]; if (restrictions.length) panel.append(el("h3", "", "Ограничения интерпретации"), limits(restrictions));
    panel.append(details("Модель, параметры, происхождение и полный результат", run)); $("lab-result").replaceChildren(panel); $("download-lab").hidden = false;
  }
  async function refresh() {
    $("refresh-brain").disabled = true; connection("Подключение…", false, true); const results = await Promise.allSettled([api("/api/brain"), api("/api/brain/sessions"), api("/api/plugins")]); const errors = [];
    if (results[0].status === "fulfilled") { state.status = results[0].value; state.ready = true; renderRegions(); } else { errors.push(results[0].reason.message); state.ready = false; }
    if (results[1].status === "fulfilled") { state.sessions = arr(results[1].value.items); renderSessions(); } else errors.push(results[1].reason.message);
    if (results[2].status === "fulfilled" && !state.labBusy) {
      const previous = $("lab-plugin").value; const response = results[2].value; state.plugins = (Array.isArray(response) ? response : arr(response.items)).filter(plugin => ["kan_benchmark", "cortical_sequence"].includes(plugin.id));
      $("lab-plugin").replaceChildren(...state.plugins.map(plugin => { const option = el("option", "", plugin.name || pluginNames[plugin.id]); option.value = plugin.id; return option; })); if (state.plugins.some(plugin => plugin.id === previous)) $("lab-plugin").value = previous;
      $("lab-fields").disabled = !state.plugins.length; renderParameters(); if (!state.plugins.length) feedback("lab-feedback", "Модули kan_benchmark и cortical_sequence не найдены в запущенной сборке.", true);
    } else if (results[2].status === "rejected") { errors.push(results[2].reason.message); $("lab-fields").disabled = true; }
    $("session-fields").disabled = !state.ready || state.sessionBusy; $("refresh-brain").disabled = false; globalError(errors.join(" ")); connection(errors.length ? "Не все данные доступны" : "Контур подключён", Boolean(errors.length)); if (!state.current && state.sessions.length) await selectSession(state.sessions[0].id);
  }
  $("session-form").addEventListener("submit", async event => {
    event.preventDefault(); if (state.sessionBusy || !state.ready) return; const plugins = [...document.querySelectorAll('input[name="brain-plugin"]:checked')].map(input => input.value); if (!plugins.length) { feedback("session-feedback", "Выберите хотя бы одну вычислительную ветвь.", true); return; }
    const seed = Number($("session-seed").value); if (!Number.isInteger(seed)) { feedback("session-feedback", "Seed должен быть целым числом.", true); return; } stopPoll(); state.sessionBusy = true; $("session-fields").disabled = true; feedback("session-feedback", "Создание плана и задач очереди…");
    try { const session = await api("/api/brain/start", { question: $("session-question").value.trim(), plugins, seed, data_class: $("session-data-class").value }); state.selection += 1; state.pollCount = 0; setSession(session); feedback("session-feedback", "Сессия создана. Workers выполнят вычислительные ветви; контур сохранит результаты проверки."); await reloadSessions(); }
    catch (error) { feedback("session-feedback", error.message, true); } finally { state.sessionBusy = false; $("session-fields").disabled = !state.ready; if (state.current) { $("tick-session").disabled = false; $("cancel-session").disabled = terminal.has(state.current.status); } schedulePoll(); }
  });
  $("cancel-session").addEventListener("click", async () => {
    if (!state.current || state.sessionBusy || state.polling) return; const id = state.current.id; const selection = state.selection; stopPoll(); state.sessionBusy = true; $("cancel-session").disabled = true; $("tick-session").disabled = true;
    try { const session = await api("/api/brain/cancel", { id }); if (selection === state.selection) setSession(session); await reloadSessions(); }
    catch (error) { globalError(error.message); } finally { state.sessionBusy = false; if (state.current) { $("tick-session").disabled = false; $("cancel-session").disabled = terminal.has(state.current.status); } schedulePoll(); }
  });
  $("lab-form").addEventListener("submit", async event => {
    event.preventDefault(); if (state.labBusy || !$("lab-plugin").value) return;
    try { const body = { plugin_id: $("lab-plugin").value, parameters: getParameters() }; state.labBusy = true; $("lab-fields").disabled = true; const loading = el("div", "loading-inline"); loading.append(el("span", "spinner"), el("span", "", "Выполняется расчёт. Предыдущий результат, если есть, остаётся ниже.")); $("lab-feedback").replaceChildren(loading); const run = await api("/api/run", body); state.labRun = run; renderLabResult(run); feedback("lab-feedback", run.status === "completed" ? "Расчёт завершён и сохранён." : "Запуск сохранён, но не завершён успешно. Подробности приведены ниже.", run.status !== "completed"); }
    catch (error) { feedback("lab-feedback", `${error.message}${state.labRun ? " Ниже остаётся предыдущий результат." : ""}`, true); } finally { state.labBusy = false; $("lab-fields").disabled = !state.plugins.length; }
  });
  $("lab-plugin").addEventListener("change", renderParameters); $("refresh-brain").addEventListener("click", refresh); $("refresh-sessions").addEventListener("click", () => reloadSessions().catch(error => globalError(error.message)));
  $("tick-session").addEventListener("click", () => tick(true)); $("auto-poll").addEventListener("change", () => { state.pollCount = 0; if (!$("auto-poll").checked) { stopPoll(); $("poll-note").textContent = "Автоматическое обновление выключено. Задачи очереди продолжают выполняться."; } else schedulePoll(); });
  $("export-session").addEventListener("click", () => { if (state.current) download(state.current, `brain-session-${state.current.id}`); }); $("download-lab").addEventListener("click", () => { if (state.labRun) download(state.labRun, `${state.labRun.plugin_id}-${state.labRun.id || "result"}`); });
  ["circuit", "lab"].forEach(name => { $(`tab-${name}`).addEventListener("click", () => setTab(name)); $(`tab-${name}`).addEventListener("keydown", event => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); setTab(event.key === "Home" ? "circuit" : event.key === "End" ? "lab" : name === "circuit" ? "lab" : "circuit", true); } }); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopPoll(); else schedulePoll(); }); window.addEventListener("pagehide", stopPoll); if (location.hash === "#lab") setTab("lab"); refresh();
})();

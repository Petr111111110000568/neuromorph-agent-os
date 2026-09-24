(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const arr = value => Array.isArray(value) ? value : [];
  const state = { plugin: null, run: null, busy: false };
  const CONDITIONS = ["fixed", "random", "guided"];
  const NAMES = { fixed: "Fixed", random: "Random", guided: "Guided" };
  const COLORS = { fixed: "#467c9b", random: "#aa7734", guided: "#007c78" };
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const fmt = value => finite(value) ? value.toLocaleString("ru-RU", { maximumSignificantDigits: 5 }) : "—";
  const printable = value => value == null ? "Не указано" : typeof value === "string" ? value : JSON.stringify(value, null, 2);
  function node(tag, className, text) { const element = document.createElement(tag); if (className) element.className = className; if (text != null) element.textContent = String(text); return element; }
  function svgNode(tag, attrs = {}, text) { const element = document.createElementNS("http://www.w3.org/2000/svg", tag); Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, String(value))); if (text != null) element.textContent = String(text); return element; }
  function status(message, error = false) { $("morph-status").textContent = message; $("morph-dot").className = error ? "status-dot error" : state.busy ? "status-dot loading" : "status-dot"; }
  function showError(message) { const box = node("div", "error-panel", message); box.setAttribute("role", "alert"); $("run-feedback").replaceChildren(box); }
  async function api(path, body) {
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 45000);
    try {
      const response = await fetch(path, { method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal });
      if (!(response.headers.get("content-type") || "").includes("application/json")) throw new Error(`Неожиданный ответ сервера (${response.status}). Проверьте запуск Meta-Harness.`);
      const result = await response.json(); if (!response.ok) throw new Error(result.error?.message || printable(result.error) || `Ошибка HTTP ${response.status}`); return result;
    } catch (error) { if (error.name === "AbortError") throw new Error("Истекло время ожидания. Перед повторным запуском проверьте журнал: вычисление могло завершиться на сервере."); throw error; } finally { clearTimeout(timer); }
  }
  function schemaFor(plugin) { return plugin.parameters || plugin.input_schema || plugin.inputSchema || {}; }
  function renderParameters() {
    const schema = schemaFor(state.plugin); const fields = [];
    Object.entries(schema.properties || {}).forEach(([key, prop]) => {
      const field = node("div", "parameter-field"); const label = node("label", "", prop.title || key); const id = `morph-param-${key}`; label.htmlFor = id;
      let input;
      if (Array.isArray(prop.enum)) { input = node("select"); prop.enum.forEach(value => { const option = node("option", "", printable(value)); option.value = String(value); input.append(option); }); }
      else if (prop.type === "boolean") { input = node("input"); input.type = "checkbox"; input.checked = prop.default === true; label.className = "checkbox-label"; label.replaceChildren(input, document.createTextNode(prop.title || key)); }
      else if (["array", "object"].includes(prop.type)) { input = node("textarea"); input.rows = 3; input.value = JSON.stringify(prop.default ?? (prop.type === "array" ? [] : {})); }
      else { input = node("input"); input.type = ["number", "integer"].includes(prop.type) ? "number" : "text"; if (input.type === "number") { input.step = prop.type === "integer" ? "1" : "any"; if (prop.minimum !== undefined) input.min = String(prop.minimum); if (prop.maximum !== undefined) input.max = String(prop.maximum); } if (prop.minLength !== undefined) input.minLength = prop.minLength; if (prop.maxLength !== undefined) input.maxLength = prop.maxLength; }
      input.id = id; input.name = key; input.dataset.parameter = key; input.dataset.type = prop.type || "string";
      if (prop.type !== "boolean" && prop.default !== undefined && !["object", "array"].includes(prop.type)) input.value = String(prop.default);
      if (prop.type !== "boolean" && (arr(schema.required).includes(key) || prop.default !== undefined)) input.required = true;
      field.append(label); if (prop.type !== "boolean") field.append(input);
      const bounds = finite(prop.minimum) && finite(prop.maximum) ? `Диапазон: ${fmt(prop.minimum)}–${fmt(prop.maximum)}.` : "";
      const help = prop.description || (key === "seed" ? "Одинаковые параметры и seed позволяют повторить расчёт." : key === "replicates" ? "Все последовательные seed включаются в сводку." : key === "edge_budget" ? "Дополнительно сохраняются 6 связей скрытый слой → выход." : bounds);
      if (help) field.append(node("p", "form-help", help)); fields.push(field);
    });
    $("morph-parameters").replaceChildren(...fields); $("plugin-description").textContent = state.plugin.description || "Параметры вычислительного эксперимента.";
  }
  function parameters() {
    const values = {};
    $("morph-parameters").querySelectorAll("[data-parameter]").forEach(input => {
      const key = input.dataset.parameter; const type = input.dataset.type;
      if (type === "boolean") values[key] = input.checked;
      else if (input.value !== "") {
        if (["number", "integer"].includes(type)) { const number = Number(input.value); if (!Number.isFinite(number) || type === "integer" && !Number.isInteger(number)) throw new Error(`Недопустимое число в параметре ${key}.`); values[key] = number; }
        else if (["array", "object"].includes(type)) { try { values[key] = JSON.parse(input.value); } catch { throw new Error(`Проверьте JSON в параметре ${key}.`); } }
        else values[key] = input.value;
      }
    }); return values;
  }
  async function loadSchema() {
    if (state.busy) return; $("reload-schema").disabled = true; $("morph-fields").disabled = true; $("morph-run").disabled = true; $("morph-status").textContent = "Загрузка схемы…"; $("morph-dot").className = "status-dot loading";
    try {
      const response = await api("/api/plugins"); const plugins = Array.isArray(response) ? response : arr(response.items); const plugin = plugins.find(item => item.id === "structural_plasticity");
      if (!plugin) throw new Error("Модуль structural_plasticity отсутствует в этой сборке. Запустите версию Meta-Harness с модулем структурной пластичности.");
      if (!Object.keys(schemaFor(plugin).properties || {}).length) throw new Error("У модуля отсутствует схема параметров; запуск не подготовлен.");
      state.plugin = plugin; renderParameters(); $("morph-fields").disabled = false; $("morph-run").disabled = false; $("setup-error").hidden = true; status("Модуль готов к запуску");
    } catch (error) { $("setup-error").textContent = error.message; $("setup-error").hidden = false; $("morph-parameters").replaceChildren(node("p", "muted", "Схема модуля недоступна.")); status("Модуль недоступен", true); } finally { $("reload-schema").disabled = false; }
  }
  function average(values) { const numbers = values.filter(finite); return numbers.length ? numbers.reduce((sum, value) => sum + value, 0) / numbers.length : undefined; }
  function spread(values) { const numbers = values.filter(finite); if (numbers.length < 2) return undefined; const mean = average(numbers); return Math.sqrt(numbers.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (numbers.length - 1)); }
  function renderComparison(result) {
    const rows = arr(result.table); const cards = CONDITIONS.map(condition => {
      const group = rows.filter(row => row.condition === condition); const card = node("article", "panel morph-condition-card"); const heading = node("div"); heading.append(node("span", `condition-dot ${condition}`), node("h3", "", NAMES[condition]));
      const mean = average(group.map(row => row.new_mse_after_shift)); const sd = spread(group.map(row => row.new_mse_after_shift));
      card.append(heading, node("p", "metric-primary", fmt(mean)), node("p", "metric-caption", "MSE новой задачи после смены · среднее"));
      const stats = node("dl"); [["Стандартное отклонение MSE", sd === undefined ? "Недостаточно повторов" : fmt(sd)], ["Прежняя задача после фазы 1", fmt(average(group.map(row => row.old_mse_after_phase1)))], ["Прежняя задача после смены", fmt(average(group.map(row => row.old_mse_after_shift)))], ["Забывание · Δ MSE", fmt(average(group.map(row => row.forgetting_delta)))], ["Условная стоимость · среднее", fmt(average(group.map(row => row.total_counted_visits)))], ["Активные связи · среднее", fmt(average(group.map(row => row.active_edges)))]].forEach(([key, value]) => stats.append(node("dt", "", key), node("dd", "", value)));
      card.append(stats, node("p", "metric-note", `Повторов: ${group.length}. Разброс описывает эти seed; доверительный интервал не оценивался.`)); return card;
    }); $("condition-comparison").replaceChildren(...cards); $("comparison-count").textContent = `${arr(result.trials).length} seed · ${rows.length} результатов`;
  }
  function renderTable(result) {
    const columns = [["seed", "Seed"], ["condition", "Условие"], ["new_mse_before_shift", "Новая задача до смены"], ["new_mse_after_shift", "Новая задача после смены"], ["old_mse_after_phase1", "Прежняя задача после фазы 1"], ["old_mse_after_shift", "Прежняя задача после смены"], ["forgetting_delta", "Забывание · Δ MSE"], ["active_edges", "Активные связи"], ["rewiring_events", "Перестройки"], ["total_counted_visits", "Учтённые посещения рёбер"]];
    const wrap = node("div", "table-wrap"); wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "Показатели каждого повторения; таблица прокручивается по горизонтали"); const table = node("table"); const head = node("thead"); const heading = node("tr");
    columns.forEach(([, title]) => { const th = node("th", "", title); th.scope = "col"; heading.append(th); }); head.append(heading); const body = node("tbody"); arr(result.table).forEach(row => { const tr = node("tr"); columns.forEach(([key]) => tr.append(node("td", "", key === "condition" ? NAMES[row[key]] || String(row[key]) : fmt(row[key])))); body.append(tr); }); table.append(head, body); wrap.append(table); const box = node("div", "result-table"); box.append(wrap); $("trial-table").replaceChildren(box);
  }
  function drawNetwork(edges, title) {
    const positions = new Map(); for (let index = 0; index < 6; index += 1) { positions.set(`x${index}`, { x: 62, y: 65 + index * 42, kind: "input-node" }); positions.set(`h${index}`, { x: 215, y: 65 + index * 42, kind: "graph-node" }); } positions.set("y", { x: 365, y: 170, kind: "output-node" });
    const valid = arr(edges).filter(edge => edge && positions.has(edge.source) && positions.has(edge.target) && finite(edge.weight));
    if (!valid.length) return node("p", "morph-placeholder", "Маска связей отсутствует в результате.");
    const drawing = svgNode("svg", { viewBox: "0 0 430 325", class: "morph-network", role: "img", "aria-label": `${title}: 6 входов, 6 скрытых узлов, 1 выход; ${valid.length} активных связей.` }); drawing.append(svgNode("title", {}, title));
    drawing.append(svgNode("text", { x: 62, y: 27, "text-anchor": "middle", class: "layer-label" }, "6 входов"), svgNode("text", { x: 215, y: 27, "text-anchor": "middle", class: "layer-label" }, "6 скрытых узлов"), svgNode("text", { x: 365, y: 27, "text-anchor": "middle", class: "layer-label" }, "1 выход"));
    const maximum = Math.max(...valid.map(edge => Math.abs(edge.weight)), 0.000001);
    valid.forEach(edge => { const from = positions.get(edge.source); const to = positions.get(edge.target); const line = svgNode("line", { x1: from.x, y1: from.y, x2: to.x, y2: to.y, stroke: edge.weight < 0 ? "#9b67a5" : "#14846d", "stroke-width": 0.6 + Math.abs(edge.weight) / maximum * 2.5, "stroke-opacity": 0.65 }); line.append(svgNode("title", {}, `${edge.source} → ${edge.target}; вес ${fmt(edge.weight)}`)); drawing.append(line); });
    positions.forEach((position, name) => { drawing.append(svgNode("circle", { cx: position.x, cy: position.y, r: 14, class: `graph-node ${position.kind}` }), svgNode("text", { x: position.x, y: position.y + 3.5, "text-anchor": "middle" }, name)); }); drawing.append(svgNode("text", { x: 215, y: 308, "text-anchor": "middle", class: "layer-label" }, `${valid.length} активных связей`)); return drawing;
  }
  function drawTrajectory(trial, result) {
    const metric = $("trajectory-metric").value; const conditionRows = arr(trial.conditions).filter(item => CONDITIONS.includes(item.condition)); const series = conditionRows.map(item => ({ condition: item.condition, points: arr(item.trajectory).filter(point => finite(point.step) && finite(point[metric])) })).filter(item => item.points.length);
    if (!series.length) { $("trajectory-chart").replaceChildren(node("p", "morph-placeholder", "Траектории отсутствуют в результате.")); return; }
    const all = series.flatMap(item => item.points); const xMin = Math.min(...all.map(point => point.step)); const xMax = Math.max(...all.map(point => point.step)); const yMax = Math.max(...all.map(point => point[metric]), 0.000001) * 1.08; const X = x => 68 + (x - xMin) / Math.max(1, xMax - xMin) * 690; const Y = y => 265 - y / yMax * 220;
    const title = `${metric === "old_test_mse" ? "Прежняя" : "Новая"} задача · seed ${trial.seed}`; const svg = svgNode("svg", { viewBox: "0 0 795 310", class: "morph-trajectory", role: "img", "aria-label": `${title}. Три кривые тестовой MSE; точные итоговые значения приведены в таблице.` }); svg.append(svgNode("title", {}, title));
    for (let tick = 0; tick <= 4; tick += 1) { const value = yMax * tick / 4; const step = xMin + (xMax - xMin) * tick / 4; svg.append(svgNode("line", { x1: 68, y1: Y(value), x2: 758, y2: Y(value), class: "chart-grid" }), svgNode("text", { x: 58, y: Y(value) + 3, "text-anchor": "end" }, fmt(value)), svgNode("text", { x: X(step), y: 286, "text-anchor": "middle" }, fmt(step))); }
    const shift = result.validation?.shift_at_step; if (finite(shift) && shift >= xMin && shift <= xMax) svg.append(svgNode("line", { x1: X(shift), y1: 35, x2: X(shift), y2: 265, class: "phase-line" }), svgNode("text", { x: X(shift), y: 21, "text-anchor": "middle" }, "Смена задачи"));
    svg.append(svgNode("text", { x: 22, y: 26 }, "MSE"), svgNode("text", { x: 759, y: 304, "text-anchor": "end" }, "Шаг обучения"));
    series.forEach(item => { const line = svgNode("polyline", { points: item.points.map(point => `${X(point.step)},${Y(point[metric])}`).join(" "), class: "curve", stroke: COLORS[item.condition] }); line.append(svgNode("title", {}, NAMES[item.condition])); svg.append(line); });
    const legend = node("div", "morph-chart-legend"); series.forEach(item => { const line = node("span"); line.append(node("i", `condition-dot ${item.condition}`), document.createTextNode(NAMES[item.condition])); legend.append(line); }); $("trajectory-chart").replaceChildren(node("p", "trajectory-summary", title), svg, legend);
  }
  function renderSelectedTrial() {
    if (!state.run) return; const result = state.run.result; const trial = arr(result.trials).find(item => String(item.seed) === $("trial-seed").value); if (!trial) return; const condition = arr(trial.conditions).find(item => item.condition === $("network-condition").value); drawTrajectory(trial, result);
    if (!condition) { $("network-initial").replaceChildren(node("p", "morph-placeholder", "Условие отсутствует в результате.")); $("network-final").replaceChildren(node("p", "morph-placeholder", "Условие отсутствует в результате.")); $("network-events").replaceChildren(); return; }
    $("network-initial").replaceChildren(drawNetwork(condition.initial_edges, `${NAMES[condition.condition]} · начальная маска · seed ${trial.seed}`)); $("network-final").replaceChildren(drawNetwork(condition.final_edges, `${NAMES[condition.condition]} · итоговая маска · seed ${trial.seed}`)); $("network-legend").hidden = false;
    $("network-description").textContent = `Seed ${trial.seed}, ${NAMES[condition.condition]}. Показаны ${arr(condition.initial_edges).length} исходных и ${arr(condition.final_edges).length} итоговых связей. Число узлов фиксировано: 6 входов, 6 скрытых узлов, 1 выход. Шесть связей к выходу сохраняются; их веса обучаются.`;
    const metrics = condition.metrics || {}; const text = node("p", "events-summary", `Событий перестройки: ${fmt(metrics.rewiring_events)}. Изменённых связей между начальной и итоговой маской: ${fmt(metrics.final_topology_changed_edges)}. Условная стоимость этого повтора: ${fmt(metrics.total_counted_visits)} учтённых посещений рёбер.`);
    const detail = node("details", "network-details"); detail.append(node("summary", "", "События перестройки и состав условной стоимости"), node("pre", "", printable({ events: condition.events, costs: condition.costs, initial_state_hash: condition.initial_state_hash, final_state_hash: condition.final_state_hash }))); $("network-events").replaceChildren(text, detail);
  }
  function renderResult(run) {
    const result = run.result; state.run = run; $("run-empty").hidden = true; $("run-summary").hidden = false; $("run-summary-text").textContent = result.summary || "Сравнение трёх условий завершено.";
    const when = run.created_at ? new Date(run.created_at) : null; $("run-meta").textContent = `Запуск ${run.id || "—"}${when && !Number.isNaN(when.getTime()) ? ` · ${when.toLocaleString("ru-RU")}` : ""} · ${state.plugin.name} ${state.plugin.version || ""}`;
    renderComparison(result); renderTable(result); const select = $("trial-seed"); select.replaceChildren(); arr(result.trials).forEach(trial => { const option = node("option", "", `Seed ${trial.seed}`); option.value = String(trial.seed); select.append(option); }); ["trial-seed", "network-condition", "trajectory-metric"].forEach(id => { $(id).disabled = false; }); renderSelectedTrial();
    const limitations = [...arr(state.plugin.limitations), ...arr(result.limitations)]; const unique = [...new Set(limitations.map(printable))]; if (unique.length) $("result-limitations").replaceChildren(...unique.map(value => node("li", "", value)));
    $("result-provenance").textContent = printable({ id: run.id, status: run.status, plugin_id: run.plugin_id, parameters: run.parameters || result.parameters, provenance: run.provenance, model: result.model, validation: result.validation, paired_comparisons: result.paired_comparisons, sources: result.sources, interpretation: result.interpretation });
  }
  $("morph-form").addEventListener("submit", async event => {
    event.preventDefault(); if (state.busy || !state.plugin) return;
    try {
      const values = parameters(); state.busy = true; $("morph-fields").disabled = true; $("morph-run").textContent = "Выполняется сравнение…"; $("reload-schema").disabled = true; status("Вычисление…");
      const box = node("div", "busy-message"); box.append(node("span", "spinner"), node("span", "", state.run ? "Выполняется новый расчёт. Ниже пока показан предыдущий сохранённый запуск." : "Выполняется расчёт трёх условий в вычислительном модуле…")); $("run-feedback").replaceChildren(box);
      const run = await api("/api/run", { plugin_id: "structural_plasticity", parameters: values });
      if (run.status !== "completed") throw new Error(run.error?.message || printable(run.error) || `Расчёт не завершён: ${run.status || "неизвестное состояние"}.`);
      if (!run.result || !arr(run.result.table).length || !arr(run.result.trials).length) throw new Error("Расчёт вернул неполный результат: отсутствует таблица или отдельные повторы. Проверьте запись в журнале.");
      renderResult(run); $("run-feedback").replaceChildren(node("div", "network-success", "Расчёт завершён. Ниже показаны результаты нового запуска, включая все заданные seed.")); status("Расчёт завершён");
    } catch (error) { showError(`${error.message}${state.run ? " На экране остаётся предыдущий сохранённый результат." : ""}`); status("Ошибка расчёта", true); } finally { state.busy = false; $("morph-fields").disabled = !state.plugin; $("morph-run").disabled = !state.plugin; $("morph-run").textContent = "Выполнить сравнение →"; $("reload-schema").disabled = false; if (!$("morph-dot").classList.contains("error")) $("morph-dot").className = "status-dot"; }
  });
  ["trial-seed", "network-condition", "trajectory-metric"].forEach(id => $(id).addEventListener("change", renderSelectedTrial));
  $("reload-schema").addEventListener("click", loadSchema);
  $("download-result").addEventListener("click", () => { if (!state.run) return; const blob = new Blob([JSON.stringify(state.run, null, 2)], { type: "application/json;charset=utf-8" }); const url = URL.createObjectURL(blob); const link = node("a"); link.href = url; link.download = `structural-plasticity-${String(state.run.id || "result").replace(/[^a-zA-Z0-9_-]/g, "_")}.json`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
  loadSchema();
})();

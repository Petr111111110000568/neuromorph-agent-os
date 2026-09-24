(() => {
  "use strict";
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const state = { candidates: [], offers: [], resources: [], reviews: [], status: {}, refreshing: false, toastTimer: null };
  const sections = { discovery: "Поиск и кандидаты", offers: "Предложения участия", resources: "Ресурсы для обмена", reviews: "Проверка вкладов", proposals: "Приглашения и подключение" };
  const labels = {
    agentverse: "Agentverse", huggingface_models: "Hugging Face · модели", huggingface_datasets: "Hugging Face · данные", mcp_registry: "MCP Registry",
    agent: "Агент", model: "Модель", dataset: "Набор данных", tool: "Инструмент", server: "Сервер", mcp_server: "Сервер MCP", platform: "Платформа",
    public: "Публичные сведения", internal: "Ограниченный доступ после выдачи права", research_summary: "Исследовательская сводка", literature_review: "Обзор литературы", dataset_qc: "Проверка качества данных", method_reproduction: "Воспроизведение метода",
    directory_listed_not_connected: "Каталог · не подключено", discovered: "Обнаружено", self_reported: "Со слов издателя", self_reported_unverified: "Заявлено · не проверено", unverified: "Не проверено", metadata_only: "Только метаданные", documented_not_connected: "Изучено · не подключено",
    invited: "Приглашён", joined: "Присоединился", active: "Активно", open: "Открыто", closed: "Закрыто", pending: "Ожидает", submitted: "Передано на проверку", pending_review: "Ожидает проверки", accepted: "Принято проверяющим", rejected: "Отклонено", revoked: "Отозвано",
    draft_not_sent: "Черновик · не отправлено", clear_web: "Публичная сеть", onion: "Onion / Tor", bundled_registry: "Встроенный каталог", live_directory: "Публичный API", agent_card: "Карточка агента"
  };
  const arr = value => Array.isArray(value) ? value : value == null ? [] : [value];
  const describe = value => value == null ? "Не указано" : typeof value === "string" ? value : typeof value === "boolean" ? value ? "Да" : "Нет" : Array.isArray(value) ? value.map(describe).join(" · ") : typeof value === "object" ? String(value.description || value.message || value.title || value.name || JSON.stringify(value)) : String(value);
  const label = value => labels[value] || describe(value);
  const identify = item => item.id || item.candidate_id || item.offer_id || item.resource_id || item.assignment_id || "";
  const node = (tag, className, text) => { const element = document.createElement(tag); if (className) element.className = className; if (text != null) element.textContent = String(text); return element; };
  const date = value => { if (!value) return "Не указано"; const result = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value); return Number.isNaN(result.getTime()) ? String(value) : result.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }); };
  function badge(value) { return node("span", `badge ${["rejected", "revoked", "failed"].includes(value) ? "error" : ["pending", "submitted", "pending_review"].includes(value) ? "warning" : ["active", "accepted", "joined"].includes(value) ? "" : "neutral"}`, label(value)); }
  function raw(value, title = "Происхождение и исходные сведения") { const detail = node("details", "network-details"); detail.append(node("summary", "", title), node("pre", "", JSON.stringify(value, null, 2) || "Нет данных")); return detail; }
  function empty(title, message) { const box = node("div", "network-empty"); box.append(node("h3", "", title), node("p", "", message)); return box; }
  function meta(rows) { const list = node("dl", "source-meta"); rows.forEach(([key, value]) => list.append(node("dt", "", key), node("dd", "", describe(value)))); return list; }
  function bulletList(values) { const list = node("ul", "network-list"); arr(values).forEach(value => list.append(node("li", "", describe(value)))); return list; }
  function safeLink(url, title) { try { if (typeof url !== "string" || /[\u0000-\u0020\u007f\\]/.test(url)) return null; const parsed = new URL(url); if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return null; const link = node("a", "source-link", title || url); link.href = parsed.href; link.rel = "noopener noreferrer"; link.target = "_blank"; return link; } catch { return null; } }
  function feedback(container, message, error = false) { container.replaceChildren(node("div", error ? "error-panel" : "network-success", message)); }
  function toast(message, error = false) { const box = $("#federation-toast"); box.textContent = message; box.className = error ? "toast error" : "toast"; box.hidden = false; clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => { box.hidden = true; }, 7000); }
  async function api(path, body, timeout = 60000) {
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, { method: body === undefined ? "GET" : "POST", credentials: "same-origin", headers: { Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal });
      if (!(response.headers.get("content-type") || "").includes("application/json")) throw new Error(`Сервер вернул неожиданный ответ (${response.status}). Проверьте запуск Meta-Harness v0.7.`);
      const data = await response.json(); if (!response.ok) throw new Error(data.error ? describe(data.error) : `Ошибка HTTP ${response.status}`); return data;
    } catch (error) { if (error.name === "AbortError") throw new Error("Время ожидания истекло. Обновите состояние перед повторной отправкой: запрос мог сохраниться на сервере."); throw error; } finally { clearTimeout(timer); }
  }
  async function action(button, callback, target) {
    if (button.disabled) return; const original = button.textContent; button.disabled = true; button.textContent = "Выполняется…"; button.setAttribute("aria-busy", "true");
    try { await callback(); } catch (error) { if (target) feedback(target, error.message, true); else toast(error.message, true); } finally { button.disabled = false; button.textContent = original; button.removeAttribute("aria-busy"); enforceSelections(); }
  }
  function section(name, updateHash = true) {
    if (!(name in sections)) name = "discovery";
    $$(".federation-section").forEach(element => { element.hidden = element.id !== `section-${name}`; });
    $$("[data-section]").forEach(button => { const active = button.dataset.section === name; button.classList.toggle("active", active); if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
    $("#section-label").textContent = sections[name]; if (updateHash && location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  }
  function chooseOptions(select, items, fallback) {
    const previous = select.value; select.replaceChildren(node("option", "", items.length ? "Выберите запись" : fallback)); select.firstElementChild.value = "";
    items.forEach(item => { const option = node("option", "", item.name || item.title || identify(item)); option.value = identify(item); select.append(option); });
    if (items.some(item => String(identify(item)) === previous)) select.value = previous;
  }
  function enforceSelections() { const button = $("#proposal-create"); if (!button.hasAttribute("aria-busy")) button.disabled = !$("#proposal-candidate").value || !$("#proposal-offer").value; }
  function renderCandidates() {
    $("#candidate-count").textContent = `${state.candidates.length} записей · статус подключения проверяется отдельно`;
    const cards = state.candidates.map(item => {
      const card = node("article", "panel source-card"); const top = node("div", "source-topline"); top.append(node("span", "source-category", label(item.kind || item.type || "agent")), badge(item.status || "directory_listed_not_connected"));
      card.append(top, node("h2", "", item.name || item.title || identify(item)), node("p", "", item.description || item.summary || "Описание не предоставлено издателем."));
      const capabilities = node("div", "resource-capabilities"); arr(item.capabilities || item.skills || item.tags).slice(0, 18).forEach(value => capabilities.append(node("span", "", describe(value)))); if (capabilities.childElementCount) card.append(capabilities);
      card.append(meta([["Источник", label(item.provider || item.provenance?.provider || item.source || "Не указан")], ["Организация", item.organization || item.publisher || item.author || "Не подтверждена"], ["Возможности", "Заявлены издателем; не проверены исполнением"], ["Обнаружено", date(item.imported_at || item.discovered_at || item.created_at || item.provenance?.retrieved_at)], ["Лицензия", item.license || item.license_spdx || "Проверяется отдельно"]]));
      const link = safeLink(item.url || item.source_url || item.card_url || item.endpoint, "Открыть источник ↗"); if (link) card.append(link);
      card.append(raw({ id: identify(item), status: item.status, self_reported: item.self_reported, provenance: item.provenance, limitations: item.limitations }));
      const controls = node("div", "campaign-actions"); const button = node("button", "button secondary", "Подобрать предложение"); button.type = "button"; button.addEventListener("click", () => { $("#proposal-candidate").value = identify(item); enforceSelections(); section("proposals"); $("#proposal-offer").focus(); }); controls.append(button); card.append(controls); return card;
    });
    $("#candidates").replaceChildren(...(cards.length ? cards : [empty("Кандидаты ещё не найдены", "Введите тему и выберите каталоги. Локальный поиск позволяет проверить рабочий процесс без внешних запросов.")]));
  }
  function renderOffers() {
    $("#offer-count").textContent = `${state.offers.length} предложений`;
    const cards = state.offers.map(item => {
      const card = node("article", "panel source-card"); const top = node("div", "source-topline"); top.append(node("span", "source-category", label(item.task_type)), badge(item.status || "open"));
      card.append(top, node("h2", "", item.title || identify(item)), node("p", "", item.description), meta([["За принятый вклад", `${item.reward_credits ?? 0} кредитов`], ["Назначений", item.max_assignments], ["Класс данных", label(item.data_class || "public")]]), node("h3", "", "Критерии приёмки"), bulletList(item.requirements), node("p", "network-id", `ID: ${identify(item)}`));
      return card;
    }); $("#offers").replaceChildren(...(cards.length ? cards : [empty("Предложений пока нет", "Сформулируйте первую задачу и измеримые критерии результата. Это основа приглашения и проверки вклада.")]));
  }
  function renderResources() {
    $("#resource-count").textContent = `${state.resources.length} материалов`;
    const cards = state.resources.map(item => {
      const card = node("article", "panel source-card"); const eligible = ["public", "internal"].includes(item.data_class) && item.rights_confirmed === true && item.redistribution_allowed === true; const top = node("div", "source-topline"); top.append(node("span", "source-category", label(item.kind || "research_summary")), node("span", eligible ? "badge" : "badge neutral", eligible ? item.data_class === "internal" ? "Участнику после выдачи права" : "Разрешён для обмена" : "Передача не подтверждена"));
      card.append(top, node("h2", "", item.title || identify(item)), node("p", "", item.summary || item.description), meta([["Стоимость доступа", `${item.cost_credits ?? 0} кредитов`], ["Лицензия", item.license || item.license_spdx], ["Класс данных", label(item.data_class)], ["Права подтверждены", item.rights_confirmed === true], ["Передача разрешена", item.redistribution_allowed === true]]), node("p", "network-id", `ID: ${identify(item)}`));
      return card;
    }); $("#resources").replaceChildren(...(cards.length ? cards : [empty("Материалов для обмена пока нет", "Добавьте исследовательскую сводку и укажите условия её использования. Предложение участия будет содержательнее, когда польза доступа определена.")]));
  }
  function renderReviews() {
    $("#review-count").textContent = `${state.reviews.length} на проверке`;
    const cards = state.reviews.map(item => {
      const card = node("article", "panel federation-review"); const offer = state.offers.find(value => identify(value) === item.offer_id); const strip = node("div", "network-meta"); strip.append(badge(item.status || "submitted"), node("span", "network-id", `Назначение: ${item.assignment_id || item.id}`));
      card.append(strip, node("h2", "", offer?.title || item.title || "Вклад внешнего исполнителя"), meta([["Исполнитель", item.member_id], ["Предложение", item.offer_id], ["Получен", date(item.submitted_at || item.updated_at || item.created_at)]]));
      if (offer?.requirements) card.append(node("h3", "", "Критерии задачи"), bulletList(offer.requirements));
      card.append(node("h3", "", "Полученный результат"), node("pre", "review-content", typeof item.result === "string" ? item.result : JSON.stringify(item.result, null, 2) || "Результат отсутствует"));
      const references = arr(item.source_refs || item.result?.source_refs); if (references.length) { const list = node("ul", "network-list"); references.forEach(reference => { const row = node("li"); const url = typeof reference === "string" ? reference : reference.url || reference.uri; const link = safeLink(url, typeof reference === "object" ? reference.title || url : url); row.append(link || node("span", "", describe(reference))); list.append(row); }); card.append(node("h3", "", "Указанные источники"), list); }
      card.append(node("h3", "", "Ограничения, указанные исполнителем"), bulletList(arr(item.limitations || item.result?.limitations).length ? item.limitations || item.result?.limitations : ["Не указаны. Учтите это при проверке."]));
      const form = node("form", "review-form"); const fields = node("div", "form-grid"); const decisionLabel = node("label", "", "Решение проверяющего"); const decision = node("select"); decision.required = true; [["", "Выберите решение"], ["accepted", "Принять вклад"], ["rejected", "Отклонить вклад"]].forEach(([value, title]) => { const option = node("option", "", title); option.value = value; decision.append(option); }); decisionLabel.append(decision);
      const rationaleLabel = node("label", "", "Обоснование по критериям задачи"); const rationale = node("textarea"); rationale.required = true; rationale.minLength = 5; rationale.maxLength = 4000; rationale.rows = 3; rationale.placeholder = "Какие критерии выполнены или нарушены? Какие источники и проверки это подтверждают?"; rationaleLabel.append(rationale); fields.append(decisionLabel, rationaleLabel);
      const actions = node("div", "form-actions"); const button = node("button", "button primary", "Сохранить решение"); button.type = "submit"; actions.append(button); const result = node("div", "inline-result"); result.setAttribute("aria-live", "polite");
      form.append(fields, actions, result); form.addEventListener("submit", event => { event.preventDefault(); action(button, async () => { await api("/api/federation/review", { assignment_id: item.assignment_id || item.id, decision: decision.value, rationale: rationale.value.trim() }); toast("Решение проверяющего сохранено."); await refresh(); }, result); }); card.append(form); return card;
    }); $("#reviews").replaceChildren(...(cards.length ? cards : [empty("Нет результатов, ожидающих проверки", "Результат появится здесь после того, как присоединившийся исполнитель выполнит назначение и передаст вклад через шлюз.")]));
  }
  function render() {
    const stats = [["Кандидаты и ресурсы", state.candidates.length], ["Предложения", state.offers.length], ["Материалы для обмена", state.resources.length], ["Ожидают проверки", state.reviews.length]];
    $("#federation-stats").replaceChildren(...stats.map(([title, count]) => { const card = node("div", "stat-card"); card.append(node("span", "", title), node("strong", "", count)); return card; }));
    renderCandidates(); renderOffers(); renderResources(); renderReviews(); chooseOptions($("#proposal-candidate"), state.candidates, "Сначала найдите кандидата"); chooseOptions($("#proposal-offer"), state.offers.filter(item => (!item.status || item.status === "open") && (item.available_slots == null || item.available_slots > 0)), "Нет доступных предложений"); enforceSelections();
  }
  async function refresh() {
    if (state.refreshing) return; state.refreshing = true;
    try {
      const [status, candidates, offers, resources, reviews] = await Promise.all([api("/api/federation"), api("/api/federation/candidates"), api("/api/federation/offers"), api("/api/federation/resources"), api("/api/federation/reviews")]);
      state.status = status; state.candidates = arr(candidates.items); state.offers = arr(offers.items); state.resources = arr(resources.items); state.reviews = arr(reviews.items); render();
      $("#federation-dot").className = "status-dot"; $("#federation-status").textContent = "Координатор доступен"; $("#global-error").hidden = true; $("#last-refresh").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU")}`;
    } catch (error) { $("#federation-dot").className = "status-dot error"; $("#federation-status").textContent = "Ошибка обновления"; $("#global-error").hidden = false; $("#global-error").textContent = `Не удалось загрузить актуальное состояние. ${error.message}`; } finally { state.refreshing = false; }
  }
  function bindForm(id, callback) { $(id).addEventListener("submit", event => { event.preventDefault(); const button = $('button[type="submit"]', event.currentTarget); action(button, callback, $(`#${id.slice(1).replace(/-form$/, "")}-feedback`)); }); }
  bindForm("#discovery-form", async () => {
    const providers = $$("input[name=provider]:checked").map(input => input.value); if (!providers.length) throw new Error("Выберите хотя бы один каталог.");
    const result = await api("/api/federation/discover", { query: $("#discovery-query").value.trim(), providers, limit: Number($("#discovery-limit").value), online: $("#discovery-online").checked, data_class: "public" });
    const count = Array.isArray(result.items) ? result.items.length : result.count ?? result.added ?? null; feedback($("#discovery-feedback"), `${count == null ? "Поиск выполнен." : `Поиск выполнен: ${count} записей в ответе.`} ${$("#discovery-online").checked ? "Проверьте происхождение каждого результата." : "Использованы локальные сведения; актуальность внешних сервисов не проверялась."}`);
    if (arr(result.errors).length) $("#discovery-feedback").append(raw(result.errors, "Ограничения и ошибки каталогов")); await refresh();
  });
  bindForm("#card-form", async () => { const proxy = $("#card-proxy").value.trim(); const result = await api("/api/federation/card", { url: $("#card-url").value.trim(), ...(proxy ? { tor_proxy: proxy } : {}) }); feedback($("#card-feedback"), `Карточка прочитана: ${result.name || result.item?.name || result.candidate?.name || "метаданные сохранены"}. Возможности заявлены издателем; агент не подключён.`); await refresh(); });
  bindForm("#offer-form", async () => { const requirements = $("#offer-requirements").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean); if (!requirements.length) throw new Error("Укажите хотя бы один критерий приёмки."); const result = await api("/api/federation/offer", { title: $("#offer-title").value.trim(), description: $("#offer-description").value.trim(), task_type: $("#offer-type").value, requirements, reward_credits: Number($("#offer-reward").value), max_assignments: Number($("#offer-max").value), data_class: "public" }); feedback($("#offer-feedback"), `Предложение сохранено. ID: ${identify(result.offer || result)}.`); await refresh(); });
  bindForm("#resource-form", async () => { const result = await api("/api/federation/resource", { kind: "research_summary", title: $("#resource-title").value.trim(), summary: $("#resource-summary").value.trim(), content: $("#resource-content").value.trim(), license: $("#resource-license").value.trim(), data_class: $("#resource-data").value, cost_credits: Number($("#resource-cost").value), rights_confirmed: $("#resource-rights").checked, redistribution_allowed: $("#resource-redistribution").checked }); feedback($("#resource-feedback"), `Материал сохранён. ID: ${identify(result.resource || result)}.`); await refresh(); });
  bindForm("#proposal-form", async () => {
    const result = await api("/api/federation/proposal", { candidate_id: $("#proposal-candidate").value, offer_id: $("#proposal-offer").value }); const card = node("article", "panel"); const heading = node("div", "result-title"); heading.append(node("h2", "", "Текст приглашения"), badge(result.delivery_status || "draft_not_sent"));
    card.append(heading, node("p", "form-help", "Текст подготовлен локально. Контакт с кандидатом не выполнялся."), node("div", "proposal-text", result.message || "Текст не сформирован."));
    if (arr(result.matched_capabilities).length) card.append(node("h3", "", "Совпавшие заявленные возможности"), bulletList(result.matched_capabilities)); else card.append(node("p", "form-help", "Совпадение возможностей не установлено. Проверьте пригодность кандидата до приглашения."));
    card.append(raw({ offer: result.offer, matched_capabilities: result.matched_capabilities, delivery_status: result.delivery_status }, "Основание предложения")); $("#proposal-result").replaceChildren(card); feedback($("#proposal-feedback"), "Черновик подготовлен. Приглашение не отправлялось.");
  });
  $$("[data-section]").forEach(button => button.addEventListener("click", () => section(button.dataset.section)));
  ["#proposal-candidate", "#proposal-offer"].forEach(selector => $(selector).addEventListener("change", enforceSelections));
  $("#refresh-federation").addEventListener("click", event => action(event.currentTarget, refresh));
  window.addEventListener("hashchange", () => section(location.hash.slice(1), false));
  section(location.hash.slice(1) || "discovery", false); enforceSelections(); refresh();
})();

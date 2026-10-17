"use strict";

(() => {
  const labels = { proposed: "Proposal", review: "In review", complete: "Example complete" };
  const categories = new Set(["interface", "documentation", "quality"]);
  const get = (id) => document.getElementById(id);
  const search = get("search");
  const status = get("status");
  const dialog = get("task-dialog");
  let tasks = [];
  let lastOpener = null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function validDataset(data) {
    if (!data || data.schema_version !== 1 || data.synthetic !== true ||
        data.dataset_label !== "Entirely fictional Atlas examples" ||
        !Array.isArray(data.tasks) || data.tasks.length !== 6) return false;
    const ids = new Set();
    return data.tasks.every((task) => {
      if (!task || task.synthetic !== true || !/^DEMO-00[1-6]$/.test(task.id) ||
          ids.has(task.id) || !Object.hasOwn(labels, task.status) || !categories.has(task.category)) return false;
      ids.add(task.id);
      return ["title", "summary", "acceptance", "evidence"].every((field) =>
        typeof task[field] === "string" && task[field].length > 0 && task[field].length <= 400);
    });
  }

  function showTask(task, opener) {
    if (dialog.open) return;
    lastOpener = opener;
    get("task-id").textContent = task.id;
    get("task-title").textContent = task.title;
    get("task-summary").textContent = task.summary;
    get("task-acceptance").textContent = task.acceptance;
    get("task-evidence").textContent = task.evidence;
    dialog.showModal();
    get("close-dialog").focus();
  }

  function render() {
    const query = search.value.trim().toLowerCase();
    const shown = tasks.filter((task) => (status.value === "all" || status.value === task.status) &&
      `${task.id} ${task.title} ${task.summary} ${task.category}`.toLowerCase().includes(query));
    const cards = shown.map((task) => {
      const card = element("article", "card");
      const top = element("div", "card-top");
      top.append(element("span", "card-id", task.id), element("span", `badge badge-${task.status}`, labels[task.status]));
      const heading = element("h3", "", task.title);
      const button = element("button", "card-button", "Inspect example →");
      button.type = "button";
      button.setAttribute("aria-label", `Inspect example: ${task.title}`);
      button.addEventListener("click", () => showTask(task, button));
      card.append(top, heading, element("p", "", task.summary), button);
      return card;
    });
    get("cards").replaceChildren(...cards);
    get("result-count").textContent = `${shown.length} of ${tasks.length} synthetic examples`;
    get("empty-state").hidden = shown.length !== 0;
  }

  get("filters").addEventListener("submit", (event) => event.preventDefault());
  search.addEventListener("input", render);
  status.addEventListener("change", render);
  get("reset").addEventListener("click", () => {
    search.value = "";
    status.value = "all";
    render();
    search.focus();
  });
  get("close-dialog").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    if (lastOpener && lastOpener.isConnected) lastOpener.focus();
  });
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });

  fetch("demo-data.json", { credentials: "omit", cache: "no-store" })
    .then((response) => {
      if (!response.ok) throw new Error("Demo data unavailable");
      return response.json();
    })
    .then((data) => {
      if (!validDataset(data)) throw new Error("Invalid synthetic dataset");
      tasks = data.tasks;
      get("total-summary").textContent = `${tasks.length} fictional examples · 3 sample statuses`;
      render();
    })
    .catch(() => {
      get("total-summary").textContent = "Demo data unavailable";
      get("result-count").textContent = "Examples could not be displayed";
      get("load-error").hidden = false;
      search.disabled = true;
      status.disabled = true;
      get("reset").disabled = true;
    });
})();

import { renderInventory } from "./inventory.js";

const elements = {
  launch: document.querySelector("#launch-button"),
  launchTemplate: document.querySelector("#launch-template"),
  refresh: document.querySelector("#refresh-button"),
  search: document.querySelector("#search"),
  filter: document.querySelector("#status-filter"),
  groupFilter: document.querySelector("#group-filter"),
  templateFilter: document.querySelector("#template-filter"),
  notice: document.querySelector("#notice"),
  body: document.querySelector("#jobs-body"),
  empty: document.querySelector("#empty-state"),
  detailEmpty: document.querySelector("#detail-empty"),
  detailContent: document.querySelector("#detail-content"),
  detailActions: document.querySelector("#detail-actions"),
  cancel: document.querySelector("#cancel-button"),
  output: document.querySelector("#detail-output"),
  outputState: document.querySelector("#output-state"),
};

let jobs = [];
let selectedId = null;
let displayedId = null;
let catalog = null;
let launchPending = false;
let session = null;

function updateSession(value) {
  session = value;
  document.querySelector("#signin-panel").hidden = Boolean(session);
  document.querySelector("#logout-button").hidden = !session;
  document.querySelector("#session-user").textContent = session
    ? `${session.user.name} · ${session.user.roles.join(", ")}`
    : "Signed out";
  if (!session) {
    jobs = [];
    catalog = null;
    selectedId = null;
  }
  showView();
  render();
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "X-CSRF-Token": session?.csrf || "", ...options.headers },
    cache: "no-store",
  });
  if (response.status === 401) {
    updateSession(null);
    throw new Error("Session expired. Sign in again.");
  }
  return response;
}

function showView(focus = false) {
  const name = location.hash.slice(1);
  const view = ["jobs", "templates", "inventory"].includes(name)
    ? name
    : "jobs";
  const label = view[0].toUpperCase() + view.slice(1);
  for (const section of document.querySelectorAll("main > section"))
    section.hidden = !session || section.id !== `${view}-view`;
  for (const link of document.querySelectorAll("[data-view]")) {
    const current = link.dataset.view === view;
    link.classList.toggle("current", current);
    if (current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.querySelector("#current-section").textContent = label;
  document.title = `${label} · Target`;
  if (focus && session) document.querySelector(`#${view}-view h1`).focus();
}

function addOption(select, value, label) {
  if (![...select.options].some((option) => option.value === value))
    select.add(new Option(label, value));
}

function renderCatalog() {
  elements.launchTemplate.replaceChildren(
    ...catalog.templates.map(
      (template) => new Option(template.name, template.id),
    ),
  );
  const container = document.querySelector("#template-catalog");
  container.replaceChildren();
  for (const group of new Set(
    catalog.templates.map((template) => template.group),
  )) {
    const section = document.createElement("section");
    section.className = "template-group";
    const heading = document.createElement("h2");
    heading.textContent = group;
    const cards = document.createElement("div");
    cards.className = "template-grid";
    for (const template of catalog.templates.filter(
      (template) => template.group === group,
    )) {
      const card = document.createElement("article");
      card.className = "panel template-card";
      const name = document.createElement("h3");
      name.textContent = template.name;
      const description = document.createElement("p");
      description.className = "intro";
      description.textContent = template.description;
      const target = document.createElement("a");
      target.className = "template-target";
      target.href = "#inventory";
      target.textContent = `${template.inventory.name} / ${template.target_group} · ${template.target_hosts.length} host`;
      const run = document.createElement("button");
      run.type = "button";
      run.className = "primary-button";
      run.dataset.templateRun = template.id;
      run.textContent = "Run template";
      run.setAttribute("aria-label", `Run ${template.name}`);
      run.addEventListener("click", () => launch(template.id));
      card.append(name, description, target, run);
      cards.append(card);
    }
    section.append(heading, cards);
    container.append(section);
  }
  renderInventory(catalog.inventories[0]);
}

function date(value) {
  return value
    ? new Date(value).toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : "—";
}

function duration(job) {
  if (!job.started_at) return "—";
  if (!job.finished_at) return "Running";
  const seconds = Math.max(
    0,
    Math.round((new Date(job.finished_at) - new Date(job.started_at)) / 1000),
  );
  return seconds < 60
    ? `${seconds}s`
    : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function statusPill(status) {
  const pill = document.createElement("span");
  pill.className = `status-pill status-${status}`;
  pill.textContent = status.replace("_", " ");
  return pill;
}

function showNotice(message, error = false) {
  elements.notice.textContent = message;
  elements.notice.classList.toggle("error", error);
  elements.notice.hidden = !message;
}

function renderDetails() {
  const job = jobs.find((item) => item.id === selectedId);
  elements.detailEmpty.hidden = Boolean(job);
  elements.detailContent.hidden = !job;
  if (!job) return;

  const eventLabels = {
    queued: "Run queued",
    started: "Execution started",
    cancellation_requested: "Cancellation requested",
    cancelled: "Run cancelled",
    succeeded: "Run succeeded",
    failed: "Run failed",
    timed_out: "Execution timed out",
    interrupted: "Interrupted run detected at API startup",
  };
  const events = job.events || [];
  document.querySelector("#timeline-empty").hidden = events.length > 0;
  document.querySelector("#run-timeline").replaceChildren(
    ...events.map((event) => {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = eventLabels[event.event] || event.event;
      const time = document.createElement("time");
      time.dateTime = event.occurred_at;
      time.textContent = new Date(event.occurred_at).toLocaleString();
      item.append(label, time);
      return item;
    }),
  );
  document.querySelector("#detail-name").textContent = job.template.name;
  const hashes = job.template.source_hashes;
  document.querySelector("#input-legacy").hidden = Boolean(hashes);
  document.querySelector("#input-provenance").hidden = !hashes;
  document.querySelector("#detail-revision").textContent =
    job.template.revision || "";
  for (const [name, id] of [
    ["playbook.yml", "detail-playbook-hash"],
    ["inventory.yml", "detail-inventory-hash"],
    ["ansible.cfg", "detail-config-hash"],
  ])
    document.getElementById(id).textContent = hashes?.[name] || "";
  document.querySelector("#detail-group").textContent = job.template.group;
  document.querySelector("#detail-inventory").textContent =
    job.template.inventory.name;
  document.querySelector("#detail-target").textContent =
    job.template.target_group;
  document.querySelector("#detail-hosts").textContent =
    job.template.target_hosts.join(", ");
  document.querySelector("#detail-status").replaceWith(statusPill(job.status));
  document.querySelector(".detail-title .status-pill").id = "detail-status";
  document.querySelector("#detail-id").textContent = job.id;
  document.querySelector("#detail-created").textContent = date(job.created_at);
  document.querySelector("#detail-finished").textContent = date(
    job.finished_at,
  );
  document.querySelector("#detail-exit").textContent =
    job.return_code === null ? "—" : String(job.return_code);
  const active = ["queued", "running", "cancelling"].includes(job.status);
  elements.outputState.textContent = active
    ? "Live · updates every 2s"
    : "stdout + stderr";
  const followOutput =
    displayedId !== job.id ||
    elements.output.scrollTop + elements.output.clientHeight >=
      elements.output.scrollHeight - 8;
  const scrollTop = elements.output.scrollTop;
  const output =
    job.output ||
    (active ? "Waiting for Ansible output…" : "No output recorded.");
  if (elements.output.textContent !== output) {
    elements.output.textContent = output;
  }
  elements.output.scrollTop = followOutput
    ? elements.output.scrollHeight
    : scrollTop;
  displayedId = job.id;
  elements.detailActions.hidden =
    !session?.can_operate || !["queued", "running"].includes(job.status);
  elements.cancel.disabled = false;
}

function render() {
  const active = jobs.some((job) =>
    ["queued", "running", "cancelling"].includes(job.status),
  );
  elements.launch.disabled =
    active || launchPending || !catalog || !session?.can_operate;
  elements.launchTemplate.disabled = elements.launch.disabled;
  for (const button of document.querySelectorAll("[data-template-run]"))
    button.disabled = elements.launch.disabled;
  elements.launch.textContent = active
    ? "Run in progress"
    : launchPending
      ? "Launching…"
      : "＋ Run template";
  document.querySelector("#metric-total").textContent = jobs.length;
  document.querySelector("#metric-succeeded").textContent = jobs.filter(
    (job) => job.status === "succeeded",
  ).length;
  document.querySelector("#metric-failed").textContent = jobs.filter(
    (job) => job.status === "failed" || job.status === "timed_out",
  ).length;
  document.querySelector("#metric-active").textContent = jobs.filter((job) =>
    ["queued", "running", "cancelling"].includes(job.status),
  ).length;

  const query = elements.search.value.trim().toLowerCase();
  const status = elements.filter.value;
  const group = elements.groupFilter.value;
  const template = elements.templateFilter.value;
  const visible = jobs.filter(
    (job) =>
      (status === "all" || job.status === status) &&
      (group === "all" || job.template.group === group) &&
      (template === "all" || job.template.id === template) &&
      (!query ||
        job.id.toLowerCase().includes(query) ||
        job.status.includes(query) ||
        `${job.template.name} ${job.template.group} ${job.template.inventory.name} ${job.template.target_group} ${job.template.target_hosts.join(" ")}`
          .toLowerCase()
          .includes(query)),
  );
  elements.body.replaceChildren(
    ...visible.map((job) => {
      const row = document.createElement("tr");
      if (job.id === selectedId) row.className = "selected";
      const name = document.createElement("td");
      const button = document.createElement("button");
      button.className = "run-link";
      button.type = "button";
      button.textContent = job.template.name;
      button.setAttribute(
        "aria-label",
        `Inspect ${job.template.name} run ${job.id}`,
      );
      button.addEventListener("click", () => {
        selectedId = job.id;
        render();
      });
      const shortId = document.createElement("span");
      shortId.className = "run-id";
      shortId.textContent = `${job.template.group} · ${job.id.slice(0, 8)}`;
      name.append(button, shortId);
      const state = document.createElement("td");
      state.append(statusPill(job.status));
      const started = document.createElement("td");
      started.textContent = date(job.started_at || job.created_at);
      const elapsed = document.createElement("td");
      elapsed.textContent = duration(job);
      row.append(name, state, started, elapsed);
      return row;
    }),
  );
  elements.empty.hidden = visible.length > 0;
  elements.empty.querySelector("h3").textContent = jobs.length
    ? "No matching runs"
    : "No runs yet";
  elements.empty.querySelector("p").textContent = jobs.length
    ? "Try another search or filter."
    : "Choose an approved template to see its status and Ansible output here.";
  renderDetails();
}

async function loadJobs() {
  try {
    const response = await api("/api/jobs");
    if (!response.ok) throw new Error(`Server returned ${response.status}`);
    jobs = await response.json();
    for (const template of [
      ...(catalog?.templates || []),
      ...jobs.map((job) => job.template),
    ]) {
      addOption(elements.groupFilter, template.group, template.group);
      addOption(elements.templateFilter, template.id, template.name);
    }
    if (selectedId && !jobs.some((job) => job.id === selectedId))
      selectedId = null;
    if (!selectedId && jobs.length) selectedId = jobs[0].id;
    showNotice("");
    render();
  } catch (error) {
    showNotice(`Could not load jobs. ${error.message}`, true);
  }
}

async function launch(templateId) {
  if (launchPending) return;
  launchPending = true;
  elements.launchTemplate.value = templateId;
  location.hash = "jobs";
  render();
  try {
    const response = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template_id: templateId }),
    });
    const result = await response.json();
    if (!response.ok)
      throw new Error(result.error || `Server returned ${response.status}`);
    selectedId = result.id;
    await loadJobs();
  } catch (error) {
    showNotice(`Could not launch job. ${error.message}`, true);
  } finally {
    launchPending = false;
    render();
  }
}
elements.launch.addEventListener("click", () =>
  launch(elements.launchTemplate.value),
);
elements.cancel.addEventListener("click", async () => {
  const jobId = selectedId;
  if (!jobId) return;
  elements.cancel.disabled = true;
  try {
    const response = await api(`/api/jobs/${jobId}/cancel`, {
      method: "POST",
    });
    const result = await response.json();
    if (!response.ok)
      throw new Error(result.error || `Server returned ${response.status}`);
    await loadJobs();
    showNotice("Cancellation requested.");
  } catch (error) {
    showNotice(`Could not cancel job. ${error.message}`, true);
    elements.cancel.disabled = false;
  }
});
elements.refresh.addEventListener("click", () =>
  catalog ? loadJobs() : initialize(),
);
elements.search.addEventListener("input", render);
elements.filter.addEventListener("change", render);
elements.groupFilter.addEventListener("change", render);
elements.templateFilter.addEventListener("change", render);

async function initialize() {
  try {
    const identity = await fetch("/api/auth/session", { cache: "no-store" });
    if (identity.status === 401) {
      updateSession(null);
      return;
    }
    if (!identity.ok) throw new Error(`Server returned ${identity.status}`);
    updateSession(await identity.json());
    const response = await api("/api/catalog");
    if (!response.ok) throw new Error(`Server returned ${response.status}`);
    catalog = await response.json();
    renderCatalog();
    await loadJobs();
  } catch (error) {
    showNotice(
      `Could not load workflows and inventory. ${error.message}`,
      true,
    );
  }
}

document.querySelector("#logout-button").addEventListener("click", async () => {
  try {
    const response = await api("/api/auth/logout", { method: "POST" });
    if (!response.ok) throw new Error("Could not sign out");
    location.reload();
  } catch (error) {
    showNotice(error.message, true);
  }
});

showView();
window.addEventListener("hashchange", () => showView(true));
initialize();
setInterval(() => {
  if (
    jobs.some((job) => ["queued", "running", "cancelling"].includes(job.status))
  )
    loadJobs();
}, 2000);

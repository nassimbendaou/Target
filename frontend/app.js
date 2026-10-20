const elements = {
  launch: document.querySelector("#launch-button"),
  refresh: document.querySelector("#refresh-button"),
  search: document.querySelector("#search"),
  filter: document.querySelector("#status-filter"),
  notice: document.querySelector("#notice"),
  body: document.querySelector("#jobs-body"),
  empty: document.querySelector("#empty-state"),
  detailEmpty: document.querySelector("#detail-empty"),
  detailContent: document.querySelector("#detail-content"),
  detailActions: document.querySelector("#detail-actions"),
  cancel: document.querySelector("#cancel-button"),
};

let jobs = [];
let selectedId = null;

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

  document.querySelector("#detail-status").replaceWith(statusPill(job.status));
  document.querySelector(".detail-title .status-pill").id = "detail-status";
  document.querySelector("#detail-id").textContent = job.id;
  document.querySelector("#detail-created").textContent = date(job.created_at);
  document.querySelector("#detail-finished").textContent = date(
    job.finished_at,
  );
  document.querySelector("#detail-exit").textContent =
    job.return_code === null ? "—" : String(job.return_code);
  document.querySelector("#detail-output").textContent =
    job.output || "Output will appear when the run finishes.";
  elements.detailActions.hidden = !["queued", "running"].includes(job.status);
  elements.cancel.disabled = false;
}

function render() {
  const active = jobs.some((job) =>
    ["queued", "running", "cancelling"].includes(job.status),
  );
  elements.launch.disabled = active;
  elements.launch.textContent = active
    ? "Run in progress"
    : "＋ Run smoke check";
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
  const visible = jobs.filter(
    (job) =>
      (status === "all" || job.status === status) &&
      (!query ||
        job.id.toLowerCase().includes(query) ||
        job.status.includes(query) ||
        "local smoke check".includes(query)),
  );
  elements.body.replaceChildren(
    ...visible.map((job) => {
      const row = document.createElement("tr");
      if (job.id === selectedId) row.className = "selected";
      const name = document.createElement("td");
      const button = document.createElement("button");
      button.className = "run-link";
      button.type = "button";
      button.textContent = "Local smoke check";
      button.setAttribute("aria-label", `Inspect run ${job.id}`);
      button.addEventListener("click", () => {
        selectedId = job.id;
        render();
      });
      const shortId = document.createElement("span");
      shortId.className = "run-id";
      shortId.textContent = job.id.slice(0, 8);
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
    ? "Try another search or status filter."
    : "Run the localhost smoke check to see its status and Ansible output here.";
  renderDetails();
}

async function loadJobs() {
  try {
    const response = await fetch("/api/jobs", { cache: "no-store" });
    if (!response.ok) throw new Error(`Server returned ${response.status}`);
    jobs = await response.json();
    if (selectedId && !jobs.some((job) => job.id === selectedId))
      selectedId = null;
    if (!selectedId && jobs.length) selectedId = jobs[0].id;
    showNotice("");
    render();
  } catch (error) {
    showNotice(`Could not load jobs. ${error.message}`, true);
  }
}

elements.launch.addEventListener("click", async () => {
  elements.launch.disabled = true;
  try {
    const response = await fetch("/api/jobs", { method: "POST" });
    const result = await response.json();
    if (!response.ok)
      throw new Error(result.error || `Server returned ${response.status}`);
    selectedId = result.id;
    await loadJobs();
  } catch (error) {
    showNotice(`Could not launch job. ${error.message}`, true);
    elements.launch.disabled = false;
  }
});
elements.cancel.addEventListener("click", async () => {
  const jobId = selectedId;
  if (!jobId) return;
  elements.cancel.disabled = true;
  try {
    const response = await fetch(`/api/jobs/${jobId}/cancel`, {
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
elements.refresh.addEventListener("click", loadJobs);
elements.search.addEventListener("input", render);
elements.filter.addEventListener("change", render);

loadJobs();
setInterval(() => {
  if (
    jobs.some((job) => ["queued", "running", "cancelling"].includes(job.status))
  )
    loadJobs();
}, 2000);

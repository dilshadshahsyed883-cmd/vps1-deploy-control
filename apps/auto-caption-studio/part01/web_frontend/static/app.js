const BASE = "/auto-caption";
const $ = (id) => document.getElementById(id);

let currentProjectId = "";
let currentProjectName = "";
let currentJobId = "";
let settings = {};
let queueFilter = "all";
let pollTimer = null;
let queueCache = [];

async function api(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed (${response.status})`);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

function toast(message, kind = "ok") {
  const box = $("toast");
  box.textContent = message;
  box.className = `toast show ${kind}`;
  clearTimeout(box._timer);
  box._timer = setTimeout(() => box.className = "toast", 3200);
}

function readSetting(input) {
  if (input.dataset.kind === "integer") return Number.parseInt(input.value || "0", 10);
  if (input.dataset.kind === "number") return Number.parseFloat(input.value || "0");
  if (input.type === "checkbox") return input.checked;
  return input.value;
}

function collectSettings() {
  document.querySelectorAll("[data-setting]").forEach((input) => {
    settings[input.dataset.setting] = readSetting(input);
  });
  settings.caption_text_source =
    document.querySelector("#captionSourceGroup button.active")?.dataset.value || "script";
  settings.captions_enabled = true;
  settings.caption_heavy_enabled = true;
  settings.text_background_enabled = true;
  return settings;
}

function applySettings(values) {
  settings = {...settings, ...values};
  document.querySelectorAll("[data-setting]").forEach((input) => {
    if (!(input.dataset.setting in settings)) return;
    if (input.type === "checkbox") input.checked = Boolean(settings[input.dataset.setting]);
    else input.value = settings[input.dataset.setting] ?? "";
  });

  const source = settings.caption_text_source || "script";
  document.querySelectorAll("#captionSourceGroup button").forEach((button) => {
    button.classList.toggle("active", button.dataset.value === source);
  });

  $("opacityValue").textContent = `${settings.text_background_opacity_percent ?? 65}%`;
  updateCaptionMock();
  setSavedState(true);
}

function setSavedState(saved) {
  $("saveState").textContent = saved ? "Saved" : "Unsaved changes";
  $("saveState").className = `tiny-state ${saved ? "saved" : "dirty"}`;
}

function markDirty() {
  setSavedState(false);
  collectSettings();
  updateCaptionMock();
}

function updateCaptionMock() {
  collectSettings();
  const mock = $("captionMock");
  mock.style.fontFamily = settings.font_family || "DejaVu Sans";
  mock.style.fontSize = `${Math.min(Number(settings.font_size || 48), 64)}px`;
  mock.style.color = settings.text_color || "#ffffff";
  const bg = settings.text_background_color || "#000000";
  const opacity = Number(settings.text_background_opacity_percent ?? 65) / 100;
  mock.style.background = hexToRgba(bg, opacity);

  const pos = settings.caption_position || "bottom";
  mock.classList.remove("top", "center", "bottom");
  mock.classList.add(pos);
}

function hexToRgba(hex, alpha) {
  const value = hex.replace("#", "");
  const full = value.length === 3 ? value.split("").map((x) => x + x).join("") : value;
  const n = Number.parseInt(full, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function projectReady(ready) {
  ["saveSettingsBtn", "previewBtn", "renderBtn"].forEach((id) => $(id).disabled = !ready);
}

async function loadProjects(selectId = "") {
  const projects = await api("/api/projects");
  const select = $("projectSelect");
  select.innerHTML = `<option value="">Select a project</option>`;
  projects.forEach((project) => {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${project.name} · ${project.status}`;
    select.appendChild(option);
  });
  if (selectId) select.value = selectId;
}

async function selectProject(projectId) {
  if (!projectId) {
    currentProjectId = "";
    currentProjectName = "";
    $("currentProjectTitle").textContent = "No project selected";
    projectReady(false);
    return;
  }

  const project = await api(`/api/projects/${projectId}`);
  currentProjectId = project.id;
  currentProjectName = project.name;
  $("projectName").value = project.name;
  $("currentProjectTitle").textContent = project.name;
  applySettings(await api(`/api/projects/${projectId}/settings`));
  projectReady(true);
  toast("Project loaded");
}

async function createProject() {
  const audio = $("audioFile").files[0];
  const script = $("scriptFile").files[0];
  const name = $("projectName").value.trim() || "Auto Caption Project";

  if (!audio) {
    toast("Select an audio file first.", "bad");
    return;
  }

  $("createProjectBtn").disabled = true;
  $("createProjectBtn").textContent = "Creating project…";

  try {
    const project = await api("/api/projects", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name, owner_id: "ignored"}),
    });

    for (const file of [audio, script].filter(Boolean)) {
      const form = new FormData();
      form.append("file", file);
      await api(`/api/projects/${project.id}/upload`, {
        method: "POST",
        body: form,
      });
    }

    currentProjectId = project.id;
    currentProjectName = project.name;
    collectSettings();
    await saveSettings();

    $("currentProjectTitle").textContent = project.name;
    $("projectMessage").textContent = "Project created successfully.";
    projectReady(true);
    await loadProjects(project.id);
    toast("Project created and files uploaded.");
  } catch (error) {
    $("projectMessage").textContent = error.message;
    toast("Project creation failed.", "bad");
  } finally {
    $("createProjectBtn").disabled = false;
    $("createProjectBtn").textContent = "Create project and upload";
  }
}

async function saveSettings() {
  if (!currentProjectId) return;
  collectSettings();
  const saved = await api(`/api/projects/${currentProjectId}/settings`, {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(settings),
  });
  applySettings(saved);
  toast("Settings saved.");
}

async function submitRender(mode) {
  if (!currentProjectId) {
    toast("Create or select a project first.", "bad");
    return;
  }

  await saveSettings();
  const button = mode === "preview" ? $("previewBtn") : $("renderBtn");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = mode === "preview" ? "Submitting preview…" : "Submitting render…";

  try {
    const job = await api(`/api/projects/${currentProjectId}/${mode}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(settings),
    });
    currentJobId = job.id;
    selectJob(job);
    startPolling();
    toast(mode === "preview" ? "Preview added to queue." : "Full render added to queue.");
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function normalizeStatus(status) {
  if (["held", "queued", "waiting"].includes(status)) return "waiting";
  if (["running", "processing"].includes(status)) return "active";
  if (status === "completed") return "completed";
  if (["failed", "cancelled", "interrupted"].includes(status)) return "failed";
  return status || "waiting";
}

function statusLabel(status) {
  const map = {
    held: "Held",
    queued: "Waiting",
    waiting: "Waiting",
    running: "Rendering",
    processing: "Rendering",
    completed: "Completed",
    failed: "Failed",
    cancelled: "Cancelled",
    interrupted: "Interrupted",
  };
  return map[status] || status || "Unknown";
}

function selectJob(job) {
  currentJobId = job.id;
  $("detailStatus").textContent = statusLabel(job.status);
  $("detailProgress").textContent = `${job.progress || 0}%`;
  $("detailCreated").textContent = formatDate(job.created_at);
  $("detailJob").textContent = job.id.slice(0, 10);
  $("currentStatusBadge").textContent = statusLabel(job.status);
  $("currentStatusBadge").className = `badge ${normalizeStatus(job.status)}`;

  $("progressTitle").textContent = statusLabel(job.status);
  $("progressStage").textContent = `${job.current_stage || "waiting"} · ${job.message || "Waiting"}`;
  $("progressPercent").textContent = `${job.progress || 0}%`;
  $("progressFill").style.width = `${job.progress || 0}%`;

  $("cancelBtn").disabled = !["held", "queued", "waiting", "running", "processing"].includes(job.status);
  $("retryBtn").disabled = !["failed", "cancelled", "interrupted"].includes(job.status);

  if (job.status === "completed") {
    const previewUrl = `${BASE}/api/jobs/${job.id}/preview`;
    $("openPreviewBtn").href = previewUrl;
    $("openPreviewBtn").classList.remove("disabled");
    $("videoPreview").src = previewUrl;
    $("videoPreview").style.display = "block";
    $("emptyPreview").style.display = "none";
    $("captionMock").style.display = "none";

    if (job.settings?._render_mode !== "preview") {
      $("downloadBtn").href = `${BASE}/api/jobs/${job.id}/output`;
      $("downloadBtn").classList.remove("disabled");
    }
  }
}

async function pollCurrentJob() {
  if (!currentJobId) return;
  try {
    const job = await api(`/api/jobs/${currentJobId}`);
    selectJob(job);
    if (["completed", "failed", "cancelled"].includes(job.status)) stopPolling();
    await loadJobLog(job.id);
    await refreshQueue();
  } catch {}
}

function startPolling() {
  stopPolling();
  pollCurrentJob();
  pollTimer = setInterval(pollCurrentJob, 1500);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function loadJobLog(jobId) {
  try {
    $("logOutput").textContent = await api(`/api/jobs/${jobId}/logs`);
  } catch {
    $("logOutput").textContent = "No log available yet.";
  }
}

function renderQueueSummary(jobs) {
  const counts = {
    active: jobs.filter((x) => normalizeStatus(x.status) === "active").length,
    waiting: jobs.filter((x) => normalizeStatus(x.status) === "waiting").length,
    completed: jobs.filter((x) => normalizeStatus(x.status) === "completed").length,
    failed: jobs.filter((x) => normalizeStatus(x.status) === "failed").length,
  };
  $("queueSummary").innerHTML = `
    <div><strong>${counts.active}</strong><span>Active</span></div>
    <div><strong>${counts.waiting}</strong><span>Waiting</span></div>
    <div><strong>${counts.completed}</strong><span>Completed</span></div>
    <div><strong>${counts.failed}</strong><span>Failed</span></div>`;
}

function renderQueue(jobs) {
  renderQueueSummary(jobs);
  const list = $("queueList");
  list.innerHTML = "";

  const filtered = jobs.filter((job) => {
    if (queueFilter === "all") return true;
    return normalizeStatus(job.status) === queueFilter;
  });

  if (!filtered.length) {
    list.innerHTML = `<div class="queue-empty">No ${queueFilter === "all" ? "" : queueFilter} jobs yet.</div>`;
    return;
  }

  filtered.forEach((job) => {
    const card = document.createElement("article");
    const normalized = normalizeStatus(job.status);
    card.className = `queue-item ${normalized}`;
    card.innerHTML = `
      <div class="queue-main">
        <div class="queue-status-dot"></div>
        <div class="queue-copy">
          <strong>${job.project_name || job.project_id || "Render job"}</strong>
          <span>${statusLabel(job.status)} · ${job.message || "Waiting"}</span>
        </div>
      </div>
      <div class="queue-progress">
        <div><span style="width:${job.progress || 0}%"></span></div>
        <strong>${job.progress || 0}%</strong>
      </div>
      <div class="queue-meta">
        <span>${formatDate(job.created_at)}</span>
        <code>${job.id.slice(0, 8)}</code>
      </div>
      <div class="queue-actions">
        <button data-action="details">Details</button>
        ${job.status === "completed" ? `<a href="${BASE}/api/jobs/${job.id}/preview" target="_blank">Preview</a>` : ""}
        ${job.status === "completed" && job.settings?._render_mode !== "preview" ? `<a href="${BASE}/api/jobs/${job.id}/output" target="_blank">Download</a>` : ""}
        ${["held","queued","waiting","running","processing"].includes(job.status) ? `<button data-action="cancel">Cancel</button>` : ""}
        ${["failed","cancelled","interrupted"].includes(job.status) ? `<button data-action="retry">Retry</button>` : ""}
      </div>`;

    card.querySelector('[data-action="details"]')?.addEventListener("click", async () => {
      selectJob(job);
      await loadJobLog(job.id);
      startPolling();
    });
    card.querySelector('[data-action="cancel"]')?.addEventListener("click", () => cancelJob(job.id));
    card.querySelector('[data-action="retry"]')?.addEventListener("click", () => retryJob(job.id));
    list.appendChild(card);
  });
}

async function refreshQueue() {
  try {
    queueCache = await api("/api/jobs");
    renderQueue(queueCache);
  } catch (error) {
    $("queueList").innerHTML = `<div class="queue-empty error">${error.message}</div>`;
  }
}

async function cancelJob(jobId = currentJobId) {
  if (!jobId) return;
  await api(`/api/jobs/${jobId}/cancel`, {method: "POST"});
  toast("Cancellation requested.");
  await refreshQueue();
}

async function retryJob(jobId = currentJobId) {
  if (!jobId) return;
  const job = await api(`/api/jobs/${jobId}/retry`, {method: "POST"});
  currentJobId = job.id || jobId;
  toast("Job added back to queue.");
  startPolling();
}

async function initialize() {
  document.querySelectorAll("[data-setting]").forEach((input) => {
    input.addEventListener("input", markDirty);
    input.addEventListener("change", markDirty);
  });

  document.querySelectorAll("#captionSourceGroup button").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("#captionSourceGroup button").forEach((x) => x.classList.remove("active"));
      button.classList.add("active");
      settings.caption_text_source = button.dataset.value;
      document.body.classList.toggle("whisper-mode", button.dataset.value === "whisper");
      markDirty();
    });
  });

  document.querySelectorAll("[data-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      queueFilter = button.dataset.filter;
      document.querySelectorAll("[data-filter]").forEach((x) => x.classList.remove("active"));
      button.classList.add("active");
      renderQueue(queueCache);
    });
  });

  $("opacityRange").addEventListener("input", () => {
    $("opacityValue").textContent = `${$("opacityRange").value}%`;
  });

  $("audioFile").addEventListener("change", () => {
    $("audioFileName").textContent = $("audioFile").files[0]?.name || "No file selected";
  });
  $("scriptFile").addEventListener("change", () => {
    $("scriptFileName").textContent = $("scriptFile").files[0]?.name || "No file selected";
  });

  $("projectSelect").addEventListener("change", (event) => selectProject(event.target.value));
  $("createProjectBtn").addEventListener("click", createProject);
  $("newProjectBtn").addEventListener("click", () => location.reload());
  $("saveSettingsBtn").addEventListener("click", saveSettings);
  $("previewBtn").addEventListener("click", () => submitRender("preview"));
  $("renderBtn").addEventListener("click", () => submitRender("render"));
  $("cancelBtn").addEventListener("click", () => cancelJob());
  $("retryBtn").addEventListener("click", () => retryJob());

  collectSettings();
  updateCaptionMock();

  try {
    const health = await api("/health");
    $("healthBadge").textContent = "Service online";
    $("healthBadge").className = "badge completed";
  } catch {
    $("healthBadge").textContent = "Service offline";
    $("healthBadge").className = "badge failed";
  }

  await loadProjects();
  await refreshQueue();
  setInterval(refreshQueue, 5000);
}

initialize();
n/* ACS_HIDE_SHARED_FG_UI_V1 */
(function(){
  function hide(){
    document.querySelectorAll("body *").forEach(function(el){
      if((el.children.length===0 || el.tagName==="H1" || el.tagName==="H2" || el.tagName==="H3") && el.textContent.trim()==="Shared FG library"){
        var card=el.closest(".card, section, article, [class*='card'], [class*='panel']");
        (card || el.parentElement).hidden=true;
        (card || el.parentElement).style.display="none";
      }
    });
  }
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",hide); else hide();
  new MutationObserver(hide).observe(document.documentElement,{childList:true,subtree:true});
})();

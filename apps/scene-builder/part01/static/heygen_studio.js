(() => {
  "use strict";
  const root = document.getElementById("heygen-studio");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const csrf = root.dataset.csrf;
  let active = "create";
  const message = (value, error=false) => {
    $("hg-message").textContent = value;
    $("hg-message").dataset.error = error ? "1" : "0";
  };
  async function api(path, {method="GET", body, json=false}={}) {
    const headers = {"X-Heygen-CSRF": csrf};
    if (json) headers["Content-Type"] = "application/json";
    const response = await fetch("/api/heygen-studio" + path, {
      method, body: json ? JSON.stringify(body) : body,
      headers, credentials:"same-origin"
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      // Server-side middleware may return text/plain, not a JSON detail.
      // Keep messages actionable without echoing arbitrary upstream HTML.
      const detail = typeof result.detail === "string" ? result.detail
        : typeof result.error === "string" ? result.error
        : response.status === 403
          ? "VPS denied this request (HTTP 403). Sign in with an active account and reload this page."
          : response.status === 401
            ? "Your session expired. Sign in again to continue."
            : "HTTP " + response.status;
      throw Error(detail);
    }
    return result;
  }
  function tab(name) {
    active = name;
    root.querySelectorAll(".hg-tabs button").forEach(button => {
      const selected = button.dataset.tab === name;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    ["create", "jobs", "models", "gpu"].forEach(item => $("hg-" + item).hidden = item !== name);
    if (name === "jobs") { refreshJobs(); refreshQueue(); }
    if (name === "models") refreshModel();
  }
  root.querySelectorAll(".hg-tabs button").forEach(button => button.addEventListener("click", () => tab(button.dataset.tab)));
  $("hg-view-jobs").addEventListener("click", () => tab("jobs"));
  let imageUrl, audioUrl;
  function preview(input, target, isImage) {
    const file = input.files && input.files[0];
    const previous = isImage ? imageUrl : audioUrl;
    if (previous) URL.revokeObjectURL(previous);
    target.hidden = !file;
    if (file) {
      const url = URL.createObjectURL(file);
      target.src = url;
      if (isImage) imageUrl = url;
      else audioUrl = url;
    } else target.removeAttribute("src");
  }
  $("hg-image").addEventListener("change", () => preview($("hg-image"), $("hg-image-preview"), true));
  $("hg-audio").addEventListener("change", () => preview($("hg-audio"), $("hg-audio-preview"), false));

  async function loadConfig() {
    try {
      const data = await api("/config");
      $("hg-endpoint").value = data.endpoint || "";
      $("hg-token").value = "";
      $("hg-token-state").textContent = data.has_token ? "HeyGen token saved on VPS" : "No HeyGen token saved";
      $("hg-salad-token").value = "";
      $("hg-salad-token-state").textContent = data.has_salad_key
        ? "SaladCloud API key saved on VPS" : "No SaladCloud API key saved";
      $("hg-connection").textContent = data.configured ? "GPU configured — not verified" : "GPU not configured";
      $("hg-connection").dataset.state = data.configured ? "ready" : "offline";
    } catch (e) { message(e.message, true); }
  }
  $("hg-gpu-form").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      const result = await api("/config", {method:"POST", json:true, body:{
        endpoint:$("hg-endpoint").value.trim(), token:$("hg-token").value,
        salad_api_key:$("hg-salad-token").value
      }});
      $("hg-token").value = "";
      $("hg-salad-token").value = "";
      message(result.configured ? "GPU configuration saved. Run Test Connection to verify readiness." : "Configuration saved, GPU not connected.");
      await loadConfig();
    } catch (e) { message(e.message, true); }
  });
  async function refreshModel() {
    const info = $("hg-model-details");
    try {
      const config = await api("/config");
      $("hg-model-image").value = config.image || "Not reported";
      const status = await api("/engine");
      info.textContent = JSON.stringify({engine:status.engine || null, models_ready:!!status.models_ready, entrypoint_found:!!status.entrypoint_found, worker_report:status}, null, 2);
    } catch (e) { info.textContent = "Worker unavailable or not configured: " + e.message; }
  }
  $("hg-refresh-model").addEventListener("click", refreshModel);
  $("hg-test").addEventListener("click", async () => {
    $("hg-test").disabled = true;
    $("hg-engine").hidden = true;
    try {
      const status = await api("/engine");
      $("hg-engine").hidden = false;
      $("hg-engine").textContent = JSON.stringify(status, null, 2);
      const ready = !!(status.entrypoint_found && status.models_ready);
      $("hg-connection").textContent = ready ? "Worker model ready" : "Worker reachable — models missing";
      $("hg-connection").dataset.state = ready ? "ready" : "offline";
      message(ready ? "HeyGen GPU engine connected and models present. Run a real render to verify output." : "GPU worker responded, but engine or models are not ready.", !ready);
    } catch (e) {
      $("hg-connection").textContent = "GPU offline";
      $("hg-connection").dataset.state = "offline";
      message(e.message, true);
    } finally { $("hg-test").disabled = false; }
  });

  $("hg-create-form").addEventListener("submit", async event => {
    event.preventDefault();
    const image = $("hg-image").files[0], audio = $("hg-audio").files[0];
    if (!image || !audio) return message("Choose a portrait and an audio file first.", true);
    if (image.size > 10 * 1048576 || audio.size > 80 * 1048576)
      return message("Image limit 10 MB; audio limit 80 MB.", true);
    const button = $("hg-generate");
    button.disabled = true;
    const data = new FormData();
    data.append("image", image);
    data.append("audio", audio);
    try {
      const job = await api("/jobs", {method:"POST", body:data});
      message("Avatar job " + job.id.slice(0, 10) + " queued on GPU.");
      tab("jobs");
    } catch (e) { message(e.message, true); }
    finally { button.disabled = false; }
  });

  const button = (text, onClick, danger=false) => {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "hg-btn " + (danger ? "danger" : "secondary");
    node.textContent = text;
    node.addEventListener("click", onClick);
    return node;
  };
  async function jobAction(job, action) {
    if (action === "delete" && !window.confirm("Permanently delete this generated video and its GPU job files?")) return;
    try {
      await api("/jobs/" + encodeURIComponent(job.id) + "/" + action, {method:"POST"});
      message("Job " + action + " completed.");
      refreshJobs();
    } catch (e) { message(e.message, true); }
  }
  async function refreshQueue() {
    try {
      const data = await api("/queue");
      const state = data.state || "stopped";
      $("hg-queue-state").textContent = "Queue: " + state;
      $("hg-start-queue").disabled = state === "running";
      $("hg-pause-queue").disabled = state === "paused";
      $("hg-resume-queue").disabled = state === "running";
    } catch (e) { message(e.message, true); }
  }
  for (const action of ["start", "pause", "resume"]) {
    $("hg-" + action + "-queue").addEventListener("click", async () => {
      try {
        await api("/queue/" + action, {method:"POST"});
        await refreshQueue();
        await refreshJobs();
      } catch (e) { message(e.message, true); }
    });
  }

  async function refreshJobs() {
    if (active !== "jobs") return;
    const list = $("hg-list");
    try {
      const data = await api("/jobs");
      list.replaceChildren();
      if (!data.items || !data.items.length) {
        const empty = document.createElement("p");
        empty.className = "hg-note";
        empty.textContent = "No avatar jobs yet. Use Create Video to start.";
        list.append(empty);
        return;
      }
      for (const job of data.items) {
        const card = document.createElement("article");
        card.className = "hg-card";
        const top = document.createElement("div");
        top.className = "hg-toolbar";
        const title = document.createElement("div");
        const strong = document.createElement("h3");
        strong.textContent = "Avatar " + job.id.slice(0, 12);
        const note = document.createElement("p");
        note.textContent = "Created " + (job.created_at || "unknown");
        title.append(strong, note);
        const status = document.createElement("span");
        status.className = "hg-pill";
        status.textContent = job.status || "unknown";
        top.append(title, status);
        card.append(top);
        if (job.error) {
          const error = document.createElement("p");
          error.className = "hg-error";
          error.textContent = job.error;
          card.append(error);
        }
        if (job.vps_media) {
          const saved = document.createElement("p");
          saved.className = "hg-note";
          saved.textContent = job.vps_media.state === "ready"
            ? "MP4 verified and saved on VPS (available when GPU is offline)"
            : job.vps_media.state === "copying"
              ? "Copying verified MP4 to VPS…"
              : job.vps_media.state === "failed"
                ? "VPS media copy failed; use Retry VPS Save"
                : "VPS media copy pending";
          card.append(saved);
        }
        const actions = document.createElement("div");
        actions.className = "hg-actions";
        if (job.status === "completed") {
          actions.append(button("Preview", () => window.open("/api/heygen-studio/jobs/" + job.id + "/video", "_blank", "noopener,noreferrer")));
          actions.append(button("Download", () => {window.location.href="/api/heygen-studio/jobs/" + job.id + "/video?download=1";}));
          if (job.vps_media && !["ready", "copying"].includes(job.vps_media.state))
            actions.append(button(job.vps_media.state === "failed" ? "Retry VPS Save" : "Save to VPS", () => jobAction(job, "save")));
        }
        if (["queued", "running", "cancelling", "waiting_network", "waiting_upgrade"].includes(job.status))
          actions.append(button("Cancel", () => jobAction(job, "cancel"), true));
        if (["failed", "cancelled"].includes(job.status))
          actions.append(button("Retry", () => jobAction(job, "retry")));
        if (["completed", "failed", "cancelled", "waiting_network", "waiting_upgrade"].includes(job.status))
          actions.append(button("Delete", () => jobAction(job, "delete"), true));
        card.append(actions);
        list.append(card);
      }
    } catch (e) { message(e.message, true); }
  }
  $("hg-refresh").addEventListener("click", refreshJobs);
  setInterval(() => {if (active === "jobs" && !document.hidden) { refreshJobs(); refreshQueue(); }}, 12000);
  loadConfig();
})();

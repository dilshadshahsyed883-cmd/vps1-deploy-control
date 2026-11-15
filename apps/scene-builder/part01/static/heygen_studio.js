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
    if (!response.ok) throw Error(typeof result.detail === "string" ? result.detail : "HTTP " + response.status);
    return result;
  }
  function tab(name) {
    active = name;
    root.querySelectorAll(".hg-tabs button").forEach(button => {
      const selected = button.dataset.tab === name;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    ["create", "jobs", "gpu"].forEach(item => $("hg-" + item).hidden = item !== name);
    if (name === "jobs") refreshJobs();
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
      $("hg-token-state").textContent = data.has_token ? "Token saved on VPS" : "No token saved";
      $("hg-connection").textContent = data.configured ? "GPU configured — not verified" : "GPU not configured";
      $("hg-connection").dataset.state = data.configured ? "ready" : "offline";
    } catch (e) { message(e.message, true); }
  }
  $("hg-gpu-form").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      const result = await api("/config", {method:"POST", json:true, body:{
        endpoint:$("hg-endpoint").value.trim(), token:$("hg-token").value
      }});
      $("hg-token").value = "";
      message(result.configured ? "GPU configuration saved. Run Test Connection to verify readiness." : "Configuration saved, GPU not connected.");
      await loadConfig();
    } catch (e) { message(e.message, true); }
  });
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
        const actions = document.createElement("div");
        actions.className = "hg-actions";
        if (job.status === "completed") {
          actions.append(button("Preview", () => window.open("/api/heygen-studio/jobs/" + job.id + "/video", "_blank", "noopener,noreferrer")));
          actions.append(button("Download", () => {window.location.href="/api/heygen-studio/jobs/" + job.id + "/video?download=1";}));
        }
        if (["queued", "running", "cancelling"].includes(job.status))
          actions.append(button("Cancel", () => jobAction(job, "cancel"), true));
        if (["failed", "cancelled"].includes(job.status))
          actions.append(button("Retry", () => jobAction(job, "retry")));
        if (["completed", "failed", "cancelled"].includes(job.status))
          actions.append(button("Delete", () => jobAction(job, "delete"), true));
        card.append(actions);
        list.append(card);
      }
    } catch (e) { message(e.message, true); }
  }
  $("hg-refresh").addEventListener("click", refreshJobs);
  setInterval(() => {if (active === "jobs" && !document.hidden) refreshJobs();}, 12000);
  loadConfig();
})();

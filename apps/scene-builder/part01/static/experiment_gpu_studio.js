(() => {
  "use strict";
  const root = document.getElementById("eg-root");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const studio = root.dataset.studio;
  const csrf = root.dataset.csrf;
  const apiRoot = "/api/experiment-gpu/" + encodeURIComponent(studio);
  let activeTab = "production";
  const say = (text, error=false) => {
    $("eg-message").textContent = text;
    $("eg-message").dataset.error = error ? "1" : "0";
  };
  async function api(path, options={}) {
    const headers = {"X-Experiment-GPU-CSRF": csrf};
    if (options.json) headers["Content-Type"] = "application/json";
    const response = await fetch(apiRoot + path, {
      method: options.method || "GET", credentials: "same-origin",
      headers, body: options.json ? JSON.stringify(options.body) : options.body,
      cache: "no-store"
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw Error(typeof result.detail === "string" ? result.detail :
        "Request failed: HTTP " + response.status);
    }
    return result;
  }
  function tab(name) {
    activeTab = name;
    for (const button of root.querySelectorAll("[data-eg-tab]")) {
      const selected = button.dataset.egTab === name;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    }
    for (const section of ["production", "jobs", "models", "gpu"])
      $("eg-" + section).hidden = section !== name;
    if (name === "jobs") refreshJobs();
  }
  for (const button of root.querySelectorAll("[data-eg-tab]"))
    button.addEventListener("click", () => tab(button.dataset.egTab));

  async function config() {
    try {
      const data = await api("/config");
      $("eg-endpoint").value = data.endpoint || "";
      $("eg-image-ref").value = data.image_ref || "";
      $("eg-image-label").textContent = data.image_ref || "Not configured";
      $("eg-auth-mode").value = data.auth_mode || (studio === "imagen" ? "x-api-key" : "bearer");
      $("eg-api-key").value = "";
      $("eg-salad-key").value = "";
      $("eg-api-key-state").textContent = data.has_api_key ? "Worker key saved on VPS" : "No worker key saved";
      $("eg-salad-key-state").textContent = data.has_salad_key ? "Gateway key saved on VPS" : "No gateway key saved";
      $("eg-connection").textContent = data.configured ? "Configured — not verified" : "GPU not configured";
      $("eg-connection").dataset.state = "offline";
    } catch (error) { say(error.message, true); }
  }
  $("eg-config").addEventListener("submit", async event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const button = $("eg-save-config");
    button.disabled = true;
    try {
      const body = Object.fromEntries(form.entries());
      const result = await api("/config", {method: "POST", json: true, body});
      say(result.configured ? "Saved. Use Test Connection to check the GPU model." :
        "Saved. Add a gateway URL and worker token to enable generation.");
      await config();
    } catch (error) { say(error.message, true); }
    finally { button.disabled = false; }
  });
  $("eg-test").addEventListener("click", async () => {
    const button = $("eg-test");
    button.disabled = true;
    try {
      const state = await api("/health");
      $("eg-health-info").hidden = false;
      $("eg-health-info").textContent = JSON.stringify(state, null, 2);
      $("eg-connection").textContent = state.ready ? "GPU model ready" : "Worker online — not ready";
      $("eg-connection").dataset.state = state.ready ? "ready" : "offline";
      say(state.ready ? "Health check passed. Real inference must still be tested." :
        "Worker alive, but model readiness has not passed.", !state.ready);
    } catch (error) {
      $("eg-connection").textContent = "GPU unavailable";
      $("eg-connection").dataset.state = "offline";
      say(error.message, true);
    } finally { button.disabled = false; }
  });
  $("eg-check-model").addEventListener("click", async () => {
    $("eg-model-info").textContent = "Checking...";
    try { $("eg-model-info").textContent = JSON.stringify(await api("/models"), null, 2); }
    catch (error) { $("eg-model-info").textContent = "Could not inspect model: " + error.message; }
  });
  if (studio === "imagen") {
    $("eg-mode").addEventListener("change", () => {
      $("eg-edit-wrap").hidden = $("eg-mode").value !== "edit";
    });
  }
  $("eg-create").addEventListener("submit", async event => {
    event.preventDefault();
    const button = $("eg-submit");
    button.disabled = true;
    try {
      const form = new FormData(event.currentTarget);
      if (studio === "imagen" && form.get("mode") !== "edit") form.delete("reference");
      const result = await api("/jobs", {method:"POST", body:form});
      say("GPU job " + result.id + " submitted. Refresh Jobs & Outputs to monitor it.");
      tab("jobs");
    } catch (error) { say(error.message, true); }
    finally { button.disabled = false; }
  });
  const pill = (text) => {
    const span = document.createElement("span");
    span.className = "eg-pill";
    span.textContent = text;
    return span;
  };
  const link = (text, url) => {
    const a = document.createElement("a");
    a.textContent = text;
    a.href = url;
    a.rel = "noopener noreferrer";
    a.target = "_blank";
    return a;
  };
  async function renderJob(job, list) {
    const card = document.createElement("div");
    card.className = "eg-job";
    const heading = document.createElement("h4");
    heading.textContent = "Job " + job.id;
    const status = pill(job.status || "queued");
    const note = document.createElement("p");
    note.className = "eg-muted";
    note.textContent = "Created: " + (job.created_at || "unknown");
    const actions = document.createElement("div");
    actions.className = "eg-job-actions";
    card.append(heading, status, note);
    list.append(card);
    try {
      const detail = await api("/jobs/" + encodeURIComponent(job.id));
      status.textContent = detail.status || status.textContent;
      if (detail.worker_error) {
        const message = document.createElement("p");
        message.className = "eg-muted";
        message.textContent = "Worker status unavailable: " + detail.worker_error;
        card.append(message);
      }
      if (detail.error) {
        const message = document.createElement("p");
        message.textContent = detail.error;
        card.append(message);
      }
      if (detail.generation_seconds || detail.speed_factor) {
        const performance = document.createElement("p");
        performance.className = "eg-muted";
        performance.textContent = "GPU generation: " + (detail.generation_seconds || "—") +
          "s · Speed factor: " + (detail.speed_factor || "—");
        card.append(performance);
      }
      if (["completed", "succeeded", "done"].includes(detail.status)) {
        const media = "/jobs/" + encodeURIComponent(job.id) + "/media";
        if (studio === "voice-clone") {
          actions.append(link("Download WAV", apiRoot + media));
        } else {
          const count = Math.min(8, detail.output_count || 0);
          for (let i = 0; i < count; i++)
            actions.append(link("Image " + (i+1), apiRoot + media + "?index=" + i));
        }
      }
      if (studio === "imagen" && ["queued", "running", "processing"].includes(detail.status)) {
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "eg-btn secondary";
        cancel.textContent = "Cancel";
        cancel.addEventListener("click", async () => {
          try {
            await api("/jobs/" + encodeURIComponent(job.id) + "/cancel", {method:"POST"});
            say("Cancellation requested.");
            refreshJobs();
          } catch (error) { say(error.message, true); }
        });
        actions.append(cancel);
      }
    } catch (error) {
      const problem = document.createElement("p");
      problem.className = "eg-muted";
      problem.textContent = "Status check: " + error.message;
      card.append(problem);
    }
    card.append(actions);
  }
  async function refreshJobs() {
    const list = $("eg-job-list");
    try {
      const result = await api("/jobs");
      list.replaceChildren();
      if (!result.items || !result.items.length) {
        const none = document.createElement("p");
        none.className = "eg-muted";
        none.textContent = "No jobs yet. Open Production to submit one.";
        list.append(none);
      } else {
        for (const item of result.items.slice(0, 15))
          await renderJob(item, list);
      }
    } catch (error) { say(error.message, true); }
  }
  $("eg-refresh-jobs").addEventListener("click", refreshJobs);
  setInterval(() => {
    if (activeTab === "jobs" && !document.hidden) refreshJobs();
  }, 15000);
  config();
})();
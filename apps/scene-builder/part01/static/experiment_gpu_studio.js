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
    for (const section of ["production", "jobs", ...(studio === "imagen" ? ["gallery"] : []), "models", "gpu"])
      $("eg-" + section).hidden = section !== name;
    if (name === "jobs") {
      refreshJobs();
      if (studio === "imagen") refreshBatches();
    }
    if (name === "gallery") {
      refreshGallery();
      if (studio === "imagen") refreshBatchGallery();
    }
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
  if (studio === "voice-clone") {
    let previewUrl = "";
    $("eg-reference").addEventListener("change", () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      const file = $("eg-reference").files[0], player = $("eg-voice-preview");
      player.hidden = !file;
      if (file) { previewUrl = URL.createObjectURL(file); player.src = previewUrl; }
      else player.removeAttribute("src");
    });
  }
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
      if (detail.saved_to_vps) {
        const saved = document.createElement("p");
        saved.className = "eg-muted";
        saved.textContent = detail.saved_to_vps + " PNG(s) saved permanently on VPS";
        card.append(saved);
      }
      if (detail.worker_error && !detail.saved_to_vps) {
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
  let jobsLoading = false;
  async function refreshJobs() {
    if (jobsLoading) return;
    jobsLoading = true;
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
        await Promise.all(result.items.slice(0, 10).map(item => renderJob(item, list)));
      }
    } catch (error) { say(error.message, true); }
    finally { jobsLoading = false; }
  }
  async function refreshGallery() {
    if (studio !== "imagen") return;
    const list = $("eg-gallery-list");
    list.replaceChildren();
    try {
      const local = await api("/jobs");
      const details = await Promise.all((local.items || []).slice(0, 8).map(job =>
        api("/jobs/" + encodeURIComponent(job.id)).catch(() => null)));
      let count = 0;
      for (const job of details) {
        if (!job || !["completed", "succeeded", "done"].includes(job.status)) continue;
        for (let i = 0; i < Math.min(8, job.output_count || 0); i++) {
          const media = apiRoot + "/jobs/" + encodeURIComponent(job.id) + "/media?index=" + i;
          const tile = document.createElement("figure");
          const photo = document.createElement("img");
          photo.src = media + "&preview=1"; photo.loading = "lazy"; photo.alt = "Generated image " + (i + 1);
          const caption = document.createElement("figcaption");
          caption.textContent = "Job " + job.id.slice(0, 12) + " / Image " + (i+1);
          tile.append(photo, caption, link("Download", media));
          list.append(tile); count++;
        }
      }
      if (!count) {
        const text = document.createElement("p");
        text.className = "eg-muted"; text.textContent = "No completed image outputs yet.";
        list.append(text);
      }
    } catch (error) { say(error.message, true); }
  }

  // Admin-only Imagen batch queue. The server executes jobs without a live browser.
  if (studio === "imagen") {
    $("eg-batch-create").addEventListener("submit", async event => {
      event.preventDefault();
      const button = $("eg-batch-submit");
      button.disabled = true;
      try {
        const body = {
          prompts: $("eg-batch-prompts").value,
          width: Number($("eg-width").value),
          height: Number($("eg-height").value)
        };
        const result = await api("/batches", {method:"POST", json:true, body});
        say("Batch " + result.batch_id.slice(0, 12) + " queued with " +
          result.count + " prompt(s). PNG names will match Prompt IDs.");
        $("eg-batch-prompts").value = "";
        tab("jobs");
      } catch (error) { say(error.message, true); }
      finally { button.disabled = false; }
    });
    $("eg-refresh-batches").addEventListener("click", refreshBatches);
  }

  function batchUrl(id) {
    return apiRoot + "/batches/" + encodeURIComponent(id);
  }

  async function openBatchDetails(batchId, target) {
    target.replaceChildren();
    try {
      const details = await api("/batches/" + encodeURIComponent(batchId));
      const actions = document.createElement("div");
      actions.className = "eg-job-actions";
      const selectedZip = document.createElement("button");
      selectedZip.type = "button";
      selectedZip.className = "eg-btn secondary";
      selectedZip.textContent = "Download Selected ZIP";
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "eg-btn secondary";
      retry.textContent = "Retry Failed";
      actions.append(selectedZip, retry);
      target.append(actions);

      const visible = details.items.slice(0, 250);
      const grid = document.createElement("div");
      grid.className = "eg-batch-items";
      const failed = [];
      for (const item of visible) {
        const row = document.createElement("div");
        row.className = "eg-batch-item";
        const check = document.createElement("input");
        check.type = "checkbox";
        check.value = item.prompt_id;
        check.disabled = item.state !== "saved";
        const title = document.createElement("span");
        title.textContent = item.filename + " — " + item.state;
        title.title = item.prompt;
        row.append(check, title);
        if (item.state === "saved")
          row.append(link("PNG", batchUrl(batchId) + "/media/" +
            encodeURIComponent(item.prompt_id)));
        if (item.state === "failed") failed.push(item.prompt_id);
        if (item.error) {
          const problem = document.createElement("small");
          problem.textContent = item.error;
          row.append(problem);
        }
        grid.append(row);
      }
      target.append(grid);
      if (details.items.length > visible.length) {
        const note = document.createElement("p");
        note.className = "eg-muted";
        note.textContent = "Showing first 250 prompts. Download All Saved ZIP includes the entire batch.";
        target.append(note);
      }
      selectedZip.addEventListener("click", () => {
        const ids = [...grid.querySelectorAll('input[type="checkbox"]:checked')].map(x => x.value);
        if (!ids.length) { say("Select at least one saved PNG first.", true); return; }
        window.location.assign(batchUrl(batchId) + "/download?ids=" +
          encodeURIComponent(ids.join(",")));
      });
      retry.disabled = failed.length === 0;
      retry.addEventListener("click", async () => {
        try {
          const allFailed = details.items.filter(x => x.state === "failed")
            .map(x => x.prompt_id);
          const result = await api("/batches/" + encodeURIComponent(batchId) + "/retry",
            {method:"POST", json:true, body:{ids:allFailed}});
          say("Retrying " + result.retrying + " failed prompt(s).");
          refreshBatches();
        } catch (error) { say(error.message, true); }
      });
    } catch (error) {
      const p = document.createElement("p");
      p.className = "eg-muted";
      p.textContent = error.message;
      target.append(p);
    }
  }

  let batchesLoading = false;
  async function refreshBatches() {
    if (studio !== "imagen" || batchesLoading) return;
    batchesLoading = true;
    const panel = $("eg-batches");
    try {
      const result = await api("/batches");
      panel.replaceChildren();
      if (!result.items.length) {
        const p = document.createElement("p");
        p.className = "eg-muted";
        p.textContent = "No batch prompts submitted yet.";
        panel.append(p);
      }
      for (const item of result.items.slice(0, 20)) {
        const card = document.createElement("div");
        card.className = "eg-job";
        const head = document.createElement("h4");
        head.textContent = "Batch " + item.id.slice(0, 12) + " · " +
          item.saved + "/" + item.total + " saved to VPS" +
          (item.failed ? " · " + item.failed + " failed" : "");
        const buttons = document.createElement("div");
        buttons.className = "eg-job-actions";
        const inspect = document.createElement("button");
        inspect.type = "button";
        inspect.className = "eg-btn secondary";
        inspect.textContent = "View Prompts / Select Images";
        inspect.addEventListener("click", () => {
          const pane = card.querySelector(".eg-batch-details");
          pane.hidden = !pane.hidden;
          if (!pane.hidden) openBatchDetails(item.id, pane);
        });
        buttons.append(inspect);
        if (item.saved > 0) buttons.append(
          link("Download All Saved ZIP", batchUrl(item.id) + "/download"));
        const details = document.createElement("div");
        details.className = "eg-batch-details";
        details.hidden = true;
        card.append(head, buttons, details);
        panel.append(card);
      }
    } catch (error) { say("Batch refresh: " + error.message, true); }
    finally { batchesLoading = false; }
  }

  async function refreshBatchGallery() {
    if (studio !== "imagen") return;
    const gallery = $("eg-batch-gallery");
    try {
      const result = await api("/batches");
      gallery.replaceChildren();
      let count = 0;
      for (const batch of result.items.slice(0, 5)) {
        if (!batch.saved || count >= 40) continue;
        const details = await api("/batches/" + encodeURIComponent(batch.id));
        for (const item of details.items) {
          if (item.state !== "saved" || count >= 40) continue;
          const media = batchUrl(batch.id) + "/media/" + encodeURIComponent(item.prompt_id);
          const tile = document.createElement("figure");
          const photo = document.createElement("img");
          photo.src = media + "?preview=1";
          photo.loading = "lazy";
          photo.alt = item.prompt_id;
          const caption = document.createElement("figcaption");
          caption.textContent = item.filename;
          tile.append(photo, caption, link("Download", media));
          gallery.append(tile);
          count++;
        }
      }
      if (!count) {
        const p = document.createElement("p");
        p.className = "eg-muted";
        p.textContent = "No saved batch images yet.";
        gallery.append(p);
      }
    } catch (error) { say("VPS gallery: " + error.message, true); }
  }

  if (studio === "imagen") $("eg-refresh-gallery").addEventListener("click", refreshGallery);
  $("eg-refresh-jobs").addEventListener("click", () => {
    refreshJobs();
    if (studio === "imagen") refreshBatches();
  });
  setInterval(() => {
    if (activeTab === "jobs" && !document.hidden) {
      refreshJobs();
      if (studio === "imagen") refreshBatches();
    }
  }, 15000);
  config();
})();
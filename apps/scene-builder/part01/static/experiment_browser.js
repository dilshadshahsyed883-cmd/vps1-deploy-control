/* Experiment Studio browser controller. No external CDP or third-party JS. */
(() => {
  "use strict";
  const root = document.getElementById("experiment-browser");
  if (!root) return;
  const byId = id => document.getElementById(id);
  const csrf = root.dataset.csrf;
  const endpoint = "/api/experiment-browser";
  const message = byId("browser-message");
  const preview = byId("browser-preview");
  const blank = byId("browser-no-preview");
  const actions = ["start", "stop", "delete", "go", "reload", "back", "type",
                   "enter", "scroll-up", "scroll-down", "refresh-preview"];
  let profiles = [];
  let selected = "";
  let busy = false;
  let pictureUrl = "";
  let screenshotBusy = false;

  function notice(value, error = false) {
    message.textContent = String(value || "");
    message.style.color = error ? "#b42318" : "#475467";
  }

  async function api(path, method = "GET", payload = null) {
    const options = {method, credentials: "same-origin", cache: "no-store"};
    if (method !== "GET") {
      options.headers = {"Content-Type": "application/json", "X-Experiment-CSRF": csrf};
      options.body = JSON.stringify(payload || {});
    }
    const response = await fetch(endpoint + path, options);
    const kind = response.headers.get("content-type") || "";
    if (!kind.includes("application/json")) {
      throw new Error("Session unavailable. Sign in again and reload the page.");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Browser request failed");
    return data;
  }

  const current = () => profiles.find(profile => profile.id === selected);
  const running = () => Boolean(current() && current().state === "running");

  function controls() {
    const have = Boolean(current());
    for (const name of actions) {
      const button = byId("browser-" + name);
      if (button) {
        button.disabled = busy || (name === "start" ? !have || running()
          : name === "delete" ? !have || running()
          : name === "stop" ? !running() : !running());
      }
    }
    const item = current();
    if (item && document.activeElement !== byId("browser-address") && !running()) {
      byId("browser-address").value = item.start_url === "about:blank" ? "" : item.start_url;
    }
  }

  function render() {
    const list = byId("browser-profiles");
    list.replaceChildren();
    for (const profile of profiles) {
      const row = document.createElement("div");
      row.className = "experiment-profile";
      const description = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = profile.name;
      const details = document.createElement("small");
      details.textContent = profile.state + " · " + profile.locale +
        (profile.proxy_server ? " · Proxy configured" : " · Direct");
      description.append(title, details);
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = profile.id === selected ? "Selected" : "Select";
      button.disabled = busy || profile.id === selected;
      button.addEventListener("click", () => {
        selected = profile.id;
        clearPreview();
        render();
        if (running()) refreshPreview();
      });
      row.append(description, button);
      list.appendChild(row);
    }
    if (!profiles.length) {
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = "No profiles yet. Create your first browser profile above.";
      list.appendChild(hint);
    }
    controls();
  }

  function clearPreview() {
    preview.hidden = true;
    preview.removeAttribute("src");
    blank.style.display = "flex";
    if (pictureUrl) URL.revokeObjectURL(pictureUrl);
    pictureUrl = "";
  }

  async function sync(choose = "") {
    const data = await api("/profiles");
    profiles = data.profiles;
    if (choose) selected = choose;
    if (!profiles.some(p => p.id === selected)) selected = profiles[0]?.id || "";
    if (!running()) clearPreview();
    render();
  }

  async function perform(operation, syncAfter = true) {
    if (busy) return;
    busy = true;
    controls();
    try {
      await operation();
      if (syncAfter) await sync();
      notice("Ready");
    } catch (error) {
      notice(error.message || String(error), true);
    } finally {
      busy = false;
      render();
    }
  }

  function requireSelection() {
    if (!selected) throw new Error("Select a browser profile first.");
    return "/profiles/" + selected;
  }

  async function refreshPreview() {
    if (!running() || screenshotBusy || document.hidden) return;
    screenshotBusy = true;
    const identity = selected;
    try {
      const response = await fetch(endpoint + "/profiles/" + identity + "/screenshot",
        {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) throw new Error("Browser preview is temporarily unavailable");
      const blob = await response.blob();
      if (selected !== identity || !running()) return;
      const newer = URL.createObjectURL(blob);
      preview.src = newer;
      preview.hidden = false;
      blank.style.display = "none";
      if (pictureUrl) URL.revokeObjectURL(pictureUrl);
      pictureUrl = newer;
    } catch (error) {
      notice(error.message, true);
    } finally {
      screenshotBusy = false;
    }
  }

  byId("browser-create-form").addEventListener("submit", event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const data = Object.fromEntries(form.entries());
    perform(async () => {
      const result = await api("/profiles", "POST", data);
      await sync(result.profile.id);
      byId("browser-profile-name").value = "";
      byId("browser-proxy-pass").value = "";
      notice("Profile saved. Press Start to launch its browser.");
    }, false);
  });

  byId("browser-start").addEventListener("click", () => perform(async () => {
    await api(requireSelection() + "/start", "POST");
    await sync();
    refreshPreview();
  }, false));
  byId("browser-stop").addEventListener("click", () => perform(async () => {
    await api(requireSelection() + "/stop", "POST");
    await sync();
  }, false));
  byId("browser-delete").addEventListener("click", () => {
    if (!current() || !window.confirm("Delete this stopped browser profile and all its local browsing data?")) return;
    perform(async () => {
      await api(requireSelection(), "DELETE");
      await sync();
    }, false);
  });

  byId("browser-go").addEventListener("click", () => perform(async () => {
    const data = await api(requireSelection() + "/navigate", "POST",
      {url: byId("browser-address").value.trim()});
    byId("browser-address").value = data.url;
    refreshPreview();
  }, false));
  byId("browser-address").addEventListener("keydown", event => {
    if (event.key === "Enter" && running()) {
      event.preventDefault();
      byId("browser-go").click();
    }
  });

  async function doAction(action, extra = {}) {
    await perform(async () => {
      await api(requireSelection() + "/action", "POST", {action, ...extra});
      refreshPreview();
    }, false);
  }
  byId("browser-reload").addEventListener("click", () => doAction("reload"));
  byId("browser-back").addEventListener("click", () => doAction("back"));
  byId("browser-enter").addEventListener("click", () => doAction("key", {key: "Enter"}));
  byId("browser-type").addEventListener("click", () => doAction("type",
    {text: byId("browser-typed-text").value}));
  byId("browser-scroll-up").addEventListener("click", () => doAction("scroll", {dy: -620}));
  byId("browser-scroll-down").addEventListener("click", () => doAction("scroll", {dy: 620}));
  byId("browser-refresh-preview").addEventListener("click", refreshPreview);
  preview.addEventListener("click", event => {
    if (!running()) return;
    const rect = preview.getBoundingClientRect();
    const x = Math.floor((event.clientX - rect.left) * preview.naturalWidth / rect.width);
    const y = Math.floor((event.clientY - rect.top) * preview.naturalHeight / rect.height);
    doAction("click", {x, y});
  });
  const refreshTimer = window.setInterval(() => {
    if (running() && !busy) refreshPreview();
  }, 5000);
  window.addEventListener("pagehide", () => {
    window.clearInterval(refreshTimer);
    if (pictureUrl) URL.revokeObjectURL(pictureUrl);
  });

  api("/health").then(status => {
    byId("browser-engine-status").textContent =
      status.chrome_installed ? "Browser engine available · Up to 2 active profiles"
        : "Browser engine installed · Chrome binary unavailable";
    return sync();
  }).catch(error => notice(error.message, true));
})();

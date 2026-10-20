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
  const actions = ["start", "stop", "delete", "clone", "privacy", "connection-check", "go", "reload", "back", "type",
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
          : name === "clone" ? !have
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
      details.textContent = profile.state + " · " + profile.locale + " · " +
        (profile.privacy_mode === "strict" ? "Strict privacy" : "Standard privacy") +
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
    const item = current();
    byId("browser-privacy-summary").textContent = item ?
      ("Privacy: " + item.privacy_mode + " · DNT: " + (item.do_not_track ? "on" : "off") +
       " · Location: " + item.geolocation_mode +
       " · Browsing storage isolated per profile. Proxy IP not checked.") :
      "Create or select a profile to view its privacy configuration.";
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

  async function perform(operation, syncAfter = true, successMessage = "Ready") {
    if (busy) return;
    busy = true;
    controls();
    try {
      await operation();
      if (syncAfter) await sync();
      if (successMessage !== null) notice(successMessage);
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
    }, false, null);
  });

  byId("browser-clone").addEventListener("click", () => perform(async () => {
    const result = await api(requireSelection() + "/clone", "POST");
    await sync(result.profile.id);
    notice("Settings cloned into a fresh profile. Cookies and proxy credentials were not copied.");
  }, false, null));
  byId("browser-privacy").addEventListener("click", () => perform(async () => {
    const result = await api(requireSelection() + "/privacy");
    const o = result.observed;
    notice("Observed browser: " + o.user_agent + "\nLanguage: " + o.language +
      " · Timezone: " + o.timezone + " · Viewport: " + o.width + "×" + o.height +
      " · Scale: " + o.device_pixel_ratio + "\nWebRTC non-proxied UDP restricted: " +
      (result.webrtc_udp_restricted ? "yes" : "no") +
      " · Service workers blocked: " + (result.service_workers_blocked ? "yes" : "no") +
      "\nDNS and WebRTC leak tests have NOT been run.");
  }, false, null));
  byId("browser-connection-check").addEventListener("click", () => perform(async () => {
    const result = await api(requireSelection() + "/connection-check", "POST");
    notice("Browser exit IP: " + result.ip + " (via " + result.source + ")" +
      (result.proxy_configured ? " · Proxy configured" : " · Direct connection") +
      "\nThis checks the HTTP exit IP only; DNS and WebRTC leaks were NOT tested.");
  }, false, null));

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
    const x = Math.floor((event.clientX - rect.left) * current().width / rect.width);
    const y = Math.floor((event.clientY - rect.top) * current().height / rect.height);
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

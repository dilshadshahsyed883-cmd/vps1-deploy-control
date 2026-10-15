"use strict";

const registryUrl = "/shell-assets/apps.json";
const appList = document.getElementById("appList");
const homeLink = document.getElementById("homeLink");
const homePanel = document.getElementById("homePanel");
const unavailablePanel = document.getElementById("unavailablePanel");
const appFrame = document.getElementById("appFrame");
const pageTitle = document.getElementById("pageTitle");
const pageStatus = document.getElementById("pageStatus");
const loadHint = document.getElementById("loadHint");
const navToggle = document.getElementById("navToggle");
const suiteSidebar = document.getElementById("suiteSidebar");
let apps = new Map();
const themeToggle = document.getElementById("themeToggle");
function setShellTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  themeToggle.textContent = dark ? "Light theme" : "Dark theme";
  themeToggle.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  themeToggle.setAttribute("aria-pressed", String(dark));
}
let savedTheme = null;
try { savedTheme = window.localStorage.getItem("vps1-shell-theme"); } catch (_) {}
const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
setShellTheme(savedTheme === "dark" || (savedTheme !== "light" && prefersDark) ? "dark" : "light");
themeToggle.addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  setShellTheme(next);
  try { window.localStorage.setItem("vps1-shell-theme", next); } catch (_) {}
});


function selectedAppId() {
  const match = /^#\/([a-z][a-z0-9-]{1,63})$/.exec(location.hash);
  return match ? match[1] : null;
}

function renderSelected() {
  const selected = selectedAppId();
  const item = selected ? apps.get(selected) : null;
  for (const link of appList.querySelectorAll("a[data-app-id]")) {
    if (link.dataset.appId === selected) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (!item) {
    homeLink.setAttribute("aria-current", "page");
    pageTitle.textContent = "Workspace";
    pageStatus.textContent = "Preview shell";
    homePanel.hidden = false;
    unavailablePanel.hidden = true;
    appFrame.hidden = true;
    appFrame.removeAttribute("src");
    loadHint.hidden = true;
  } else {
    homeLink.removeAttribute("aria-current");
    pageTitle.textContent = item.title;
    homePanel.hidden = true;
    if (!item.route_configured) {
      pageStatus.textContent = "Route pending";
      unavailablePanel.hidden = false;
      appFrame.hidden = true;
      appFrame.removeAttribute("src");
      loadHint.hidden = true;
    } else {
      pageStatus.textContent = "Runtime not verified";
      unavailablePanel.hidden = true;
      appFrame.hidden = false;
      appFrame.title = item.title;
      // Paths come only from the build-time registry, never from a raw URL.
      if (appFrame.getAttribute("src") !== item.path) appFrame.setAttribute("src", item.path);
      loadHint.hidden = false;
    }
  }
  suiteSidebar.classList.remove("open");
  navToggle.setAttribute("aria-expanded", "false");
}

function addNavigationItem(item) {
  const link = document.createElement("a");
  link.href = "#/" + item.id;
  link.className = "app-link";
  link.dataset.appId = item.id;
  link.append(document.createTextNode(item.title));
  const dot = document.createElement("span");
  dot.className = item.route_configured ? "dot configured" : "dot";
  dot.setAttribute("aria-hidden", "true");
  link.appendChild(dot);
  appList.appendChild(link);
}

async function boot() {
  try {
    const response = await fetch(registryUrl, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error("registry request failed");
    const data = await response.json();
    if (data.schema_version !== 1 || !Array.isArray(data.apps)) throw new Error("registry format invalid");
    const verified = new Map();
    for (const item of data.apps) {
      if (!/^[a-z][a-z0-9-]{1,63}$/.test(item.id) || item.path !== "/" + item.id + "/") {
        throw new Error("unsafe app route in registry");
      }
      if (verified.has(item.id)) throw new Error("duplicate app ID");
      if (typeof item.title !== "string" || typeof item.route_configured !== "boolean") {
        throw new Error("incomplete app registry");
      }
      verified.set(item.id, item);
    }
    apps = verified;
    appList.replaceChildren();
    apps.forEach(addNavigationItem);
    renderSelected();
  } catch (_) {
    pageTitle.textContent = "Workspace unavailable";
    pageStatus.textContent = "Registration error";
    homePanel.querySelector("h2").textContent = "Application registry unavailable";
    homePanel.querySelector("p").textContent = "Navigation was not loaded; no app will be opened until its routes are verified.";
    homePanel.hidden = false;
    unavailablePanel.hidden = true;
    appFrame.hidden = true;
  }
}
window.addEventListener("hashchange", renderSelected);
navToggle.addEventListener("click", () => {
  const isOpen = suiteSidebar.classList.toggle("open");
  navToggle.setAttribute("aria-expanded", String(isOpen));
});
boot();

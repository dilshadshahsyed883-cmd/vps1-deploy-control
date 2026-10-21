"use strict";

const registryUrl = "/shell-assets/apps.json";
const appList = document.getElementById("appList");
const homeLink = document.getElementById("homeLink");
const homePanel = document.getElementById("homePanel");
const pageTitle = document.getElementById("pageTitle");
const pageStatus = document.getElementById("pageStatus");
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


// The overview only links to native, top-level application documents.
// Do not inject a second application shell with iframes.
function addNavigationItem(item) {
  const link = document.createElement(item.route_configured ? "a" : "span");
  link.className = "app-link" + (item.route_configured ? "" : " pending");
  link.dataset.appId = item.id;
  if (item.route_configured) link.href = item.path;
  else link.setAttribute("aria-disabled", "true");
  link.append(document.createTextNode(item.title));
  const dot = document.createElement("span");
  dot.className = item.runtime_verified ? "dot configured" : "dot";
  dot.setAttribute("aria-hidden", "true");
  link.appendChild(dot);
  if (!item.route_configured) {
    link.title = "Route pending";
    link.append(document.createTextNode(" · Route pending"));
  } else {
    link.title = item.runtime_verified ? "Gateway checks verified" : "Route configured; runtime not verified";
  }
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
      if (typeof item.title !== "string" || !item.title.trim() || typeof item.route_configured !== "boolean" || typeof item.runtime_verified !== "boolean") {
        throw new Error("incomplete app registry");
      }
      verified.set(item.id, item);
    }
    apps = verified;
    appList.replaceChildren();
    apps.forEach(addNavigationItem);
    pageStatus.textContent = "Navigation ready";
    document.getElementById("appsHint").textContent =
      [...apps.values()].filter(item => item.route_configured).length +
      " configured app routes. Choose a tool to open its own page.";
  } catch (_) {
    pageTitle.textContent = "Workspace unavailable";
    pageStatus.textContent = "Registration error";
    homePanel.querySelector("h2").textContent = "Application registry unavailable";
    homePanel.querySelector("p").textContent = "Navigation was not loaded; no app will be opened until its routes are verified.";
    homePanel.hidden = false;
    document.getElementById("appsHint").textContent = "No routes were opened.";
  }
}
navToggle.addEventListener("click", () => {
  const isOpen = suiteSidebar.classList.toggle("open");
  navToggle.setAttribute("aria-expanded", String(isOpen));
});
boot();

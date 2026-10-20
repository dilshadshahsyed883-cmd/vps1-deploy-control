/* VPS1_NATIVE_SITE_SHELL_V1. No iframe, cross-origin fetch, or duplicated app logic. */
(() => {
  "use strict";
  const script = document.currentScript;
  const app = script && script.dataset ? script.dataset.workspace : "";
  if (!["acs", "voice", "uploader", "stream"].includes(app)) return;
  // During staggered releases an old Scene shell may still embed this document.
  // Never render a second navigation in that legacy framing context.
  if (window.self !== window.top) return;
  document.documentElement.classList.add("vps1-native-bootstrap");
  const node = (tag, name, value) => {
    const e = document.createElement(tag);
    if (name) e.className = name;
    if (value !== undefined) e.textContent = value;
    return e;
  };
  const addLink = (nav, label, href, active) => {
    const a = node("a", active ? "active" : "", label);
    a.href = href;
    if (active) a.setAttribute("aria-current", "page");
    nav.appendChild(a);
    return a;
  };
  // Canonical navigation shared by independently served native and legacy pages.
  // Never expose Admin from static markup or a client-stored role.
  const PRIMARY_NAV = Object.freeze([
    ["Dashboard", "/", "dashboard"],
    ["Upload Studio", "/local-upload-manager", "upload"],
    ["Video Studio", "/scene-builder/", "video"],
    ["Voice Studio", "/voice-studio/app#vps1-production", "voice"]
  ]);
  const activeKeyForPath = (pathname) => {
    if (/^\/(local-upload-manager|auto-stream)(\/|$)/.test(pathname)) return "upload";
    if (/^\/(scene-builder|video-studio|auto-caption-studio|short-maker|video-merger)(\/|$)/.test(pathname)) return "video";
    if (/^\/voice-studio(\/|$)/.test(pathname)) return "voice";
    return "dashboard";
  };
  const addPrimaryLinks = (navigation, current) => {
    const links = {};
    for (const [label, href, key] of PRIMARY_NAV) {
      links[key] = addLink(navigation, label, href, current === key);
    }
    return links;
  };
  function installLegacySidebar() {
    const sidebar = document.getElementById("app-sidebar");
    if (!sidebar || sidebar.dataset.vps1UnifiedSidebar) return;
    sidebar.dataset.vps1UnifiedSidebar = "1";
    sidebar.setAttribute("aria-label", "Scene Builder navigation");
    let navigation = sidebar.querySelector(":scope > nav");
    if (!navigation) {
      navigation = node("nav");
      sidebar.appendChild(navigation);
    }
    navigation.setAttribute("aria-label", "Applications");
    navigation.replaceChildren();
    addPrimaryLinks(navigation, activeKeyForPath(window.location.pathname));
    let account = sidebar.querySelector(":scope > .sidebar-account");
    if (!account) {
      account = node("div", "sidebar-account");
      const bottom = sidebar.querySelector(":scope > .server, :scope > .server-dot");
      sidebar.insertBefore(account, bottom);
    }
    account.hidden = true;
    account.replaceChildren();
    const accountName = node("div", "sidebar-account-name");
    const logout = node("form");
    logout.action = "/logout";
    logout.method = "post";
    const logoutButton = node("button", "", "Log out");
    logoutButton.type = "submit";
    logout.appendChild(logoutButton);
    account.append(accountName, logout);
    let adminLink = null;
    const setIdentity = (identity) => {
      const authenticated = !!(identity && identity.authenticated === true);
      const isAdmin = authenticated && identity.is_admin === true;
      if (isAdmin && !adminLink) {
        adminLink = addLink(navigation, "Admin", "/admin",
          /^\/admin(\/|$)/.test(window.location.pathname));
      } else if (!isAdmin && adminLink) {
        adminLink.remove();
        adminLink = null;
      }
      account.hidden = !authenticated;
      accountName.textContent = authenticated ? String(identity.username || "") : "";
    };
    const refreshIdentity = async () => {
      try {
        const response = await fetch("/api/session/identity", {
          credentials: "same-origin", cache: "no-store",
          headers: {"Accept": "application/json"}
        });
        if (!response.ok) throw new Error("Scene session unavailable");
        setIdentity(await response.json());
      } catch (_) {
        setIdentity(null);
      }
    };
    refreshIdentity();
    window.addEventListener("pageshow", refreshIdentity);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshIdentity();
    });
  }
  if (script && script.dataset && script.dataset.unifySidebar === "1") {
    if (window.self !== window.top) return;
    if (document.readyState === "loading")
      document.addEventListener("DOMContentLoaded", installLegacySidebar, {once: true});
    else installLegacySidebar();
    return;
  }
  function install() {
    if (document.getElementById("vps1-native-site-sidebar")) return;
    document.body.classList.add("vps1-native-site", "vps1-native-" + app);
    const aside = node("aside");
    aside.id = "vps1-native-site-sidebar";
    aside.setAttribute("aria-label", "Scene Builder navigation");
    const brand = node("div", "vps1-native-brand");
    brand.appendChild(node("div", "vps1-native-logo", "S"));
    const title = node("div");
    title.appendChild(node("strong", "", "Scene Builder"));
    title.appendChild(node("small", "", "Team Studio"));
    brand.appendChild(title);
    aside.appendChild(brand);
    const navigation = node("nav");
    navigation.setAttribute("aria-label", "Applications");
    // Native pages and legacy Video Studio documents use the same menu.
    const current = app === "uploader" || app === "stream" ? "upload" :
      app === "acs" ? "video" : "voice";
    const voiceLink = addPrimaryLinks(navigation, current).voice;
    if (app === "voice") {
      // A click on the already-open Voice Studio must not reload its large
      // document or restart voice engines, queues, and ongoing audio previews.
      // Keyboard/modifier navigation and uninitialized UI fall back to href.
      voiceLink.addEventListener("click", (event) => {
        if (event.defaultPrevented || (event.button !== undefined && event.button !== 0) ||
            event.ctrlKey || event.metaKey || event.shiftKey || event.altKey ||
            typeof window.vps1VoiceStudioTab !== "function") return;
        event.preventDefault();
        window.vps1VoiceStudioTab("production");
      });
    }

    // All native app screens use the live Scene session role; no local-app
    // guessing or browser-stored role flags. Admin stays hidden until verified.
    let adminLink = null;
    const account = node("div", "vps1-native-account");
    account.hidden = true;
    account.appendChild(node("small", "", "SIGNED IN"));
    const accountName = node("strong");
    account.appendChild(accountName);
    const logout = node("form");
    logout.action = "/logout";
    logout.method = "post";
    const logoutButton = node("button", "", "Log out");
    logoutButton.type = "submit";
    logout.appendChild(logoutButton);
    account.appendChild(logout);
    const setIdentity = (identity) => {
      const authorized = !!(identity && identity.authenticated === true);
      const isAdmin = authorized && identity.is_admin === true;
      if (isAdmin && !adminLink) {
        adminLink = node("a", "", "Admin");
        adminLink.href = "/admin";
        navigation.appendChild(adminLink);
      } else if (!isAdmin && adminLink) {
        adminLink.remove();
        adminLink = null;
      }
      account.hidden = !authorized;
      accountName.textContent = authorized ? String(identity.username || "") : "";
    };
    const refreshIdentity = async () => {
      try {
        const response = await fetch("/api/session/identity", {
          credentials: "same-origin", cache: "no-store",
          headers: {"Accept": "application/json"}
        });
        if (!response.ok) throw new Error("Scene session unavailable");
        setIdentity(await response.json());
      } catch (_) {
        setIdentity(null);
      }
    };

    aside.appendChild(navigation);
    aside.appendChild(account);
    aside.appendChild(node("div", "vps1-native-bottom", "VPS Connected"));
    refreshIdentity();
    // Re-check when returning to a tab after admin demotion or re-login.
    window.addEventListener("pageshow", refreshIdentity);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshIdentity();
    });
    document.body.prepend(aside);
    const toggle = node("button", "vps1-native-toggle", "☰ Menu");
    toggle.type = "button";
    toggle.setAttribute("aria-label", "Toggle main navigation");
    toggle.addEventListener("click", () => document.body.classList.toggle("vps1-native-open"));
    aside.after(toggle);
    if (app === "acs") {
      const tabs = node("nav", "vps1-native-video-tabs");
      tabs.dataset.vps1Component = "studio-tabs";
      tabs.setAttribute("aria-label", "Video Studio");
      addLink(tabs, "Scene Builder", "/scene-builder/", false);
      addLink(tabs, "Auto Caption Studio", "/auto-caption-studio/", true);
      addLink(tabs, "Short Maker", "/short-maker/", false);
      addLink(tabs, "Video Merger", "/video-merger/", false);
      toggle.after(tabs);
    }
    if (app === "uploader" || app === "stream") {
      const tabs = node("nav", "vps1-native-upload-tabs");
      tabs.dataset.vps1Component = "studio-tabs";
      tabs.setAttribute("aria-label", "Upload Studio");
      addLink(tabs, "Auto Uploader", "/local-upload-manager/app?embedded=1&tab=dashboard", app === "uploader");
      addLink(tabs, "Auto Stream", "/auto-stream/", app === "stream");
      // Native uploader and stream tabs mount before either app's own header.
      toggle.after(tabs);
    }
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", install, {once: true});
  else install();
})();

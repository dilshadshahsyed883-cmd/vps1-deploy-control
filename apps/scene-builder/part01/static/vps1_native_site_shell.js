/* VPS1_NATIVE_SITE_SHELL_V1. No iframe, cross-origin fetch, or duplicated app logic. */
(() => {
  "use strict";
  const script = document.currentScript;
  const app = script && script.dataset ? script.dataset.workspace : "";
  if (!["acs", "voice", "uploader"].includes(app)) return;
  // During staggered releases an old Scene shell may still embed this document.
  // Never render a second navigation in that legacy framing context.
  if (window.self !== window.top) return;
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
  };
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
    addLink(navigation, "Dashboard", "/", false);
    addLink(navigation, "Auto Uploader", "/local-upload-manager", app === "uploader");
    addLink(navigation, "Video Studio", "/queue", app === "acs");
    addLink(navigation, "Voice Studio", "/voice-studio", app === "voice");
    aside.appendChild(navigation);
    aside.appendChild(node("div", "vps1-native-bottom", "VPS Connected"));
    document.body.prepend(aside);
    const toggle = node("button", "vps1-native-toggle", "☰ Menu");
    toggle.type = "button";
    toggle.setAttribute("aria-label", "Toggle main navigation");
    toggle.addEventListener("click", () => document.body.classList.toggle("vps1-native-open"));
    aside.after(toggle);
    if (app === "acs") {
      const tabs = node("nav", "vps1-native-video-tabs");
      tabs.setAttribute("aria-label", "Video Studio");
      addLink(tabs, "Scene Builder", "/queue", false);
      addLink(tabs, "Auto Caption Studio", "/auto-caption/studio", true);
      addLink(tabs, "Short Maker", "/video-studio/short-maker", false);
      addLink(tabs, "Video Merger", "/video-studio/video-merger", false);
      toggle.after(tabs);
    }
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", install, {once: true});
  else install();
})();

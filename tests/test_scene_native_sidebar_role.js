"use strict";
/* Executes the actual native sidebar in a minimal DOM, no browser/network/VPS. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname,
  "../apps/scene-builder/part01/static/vps1_native_site_shell.js"), "utf8");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attrs = {};
    this.parent = null;
    this.hidden = false;
    this.dataset = {};
    this.className = "";
    this.textContent = "";
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      contains: name => classes.has(name),
      toggle: (name, force) => {
        const enabled = force === undefined ? !classes.has(name) : Boolean(force);
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      }
    };
  }
  appendChild(element) {
    element.parent = this;
    this.children.push(element);
    return element;
  }
  prepend(element) {
    element.parent = this;
    this.children.unshift(element);
  }
  setAttribute(key, value) {this.attrs[key] = value;}
  addEventListener(name, callback) {
    this.listeners ||= {};
    this.listeners[name] = callback;
  }
  after(element) {
    if (!this.parent) throw new Error("Cannot insert orphan element");
    const siblings = this.parent.children;
    siblings.splice(siblings.indexOf(this) + 1, 0, element);
    element.parent = this.parent;
  }
  remove() {
    if (!this.parent) return;
    const children = this.parent.children;
    children.splice(children.indexOf(this), 1);
    this.parent = null;
  }
}
const all = (root) => [root, ...root.children.flatMap(all)];

function boot(workspace, role) {
  const body = new Element("body");
  const listeners = {};
  const document = {
    body, hidden: false, readyState: "complete",
    documentElement: {classList: {add() {}}},
    currentScript: {dataset: {workspace}},
    createElement: (tag) => new Element(tag),
    getElementById: (id) => all(body).find(e => e.id === id) || null,
    addEventListener: (event, fn) => {listeners[event] = fn},
  };
  const winEvents = {};
  const window = {addEventListener: (event, fn) => {winEvents[event] = fn}};
  window.top = window;
  window.self = window;
  let identity = role;
  let calls = [];
  const fetch = async (url, options) => {
    calls.push({url, options});
    return {
      ok: identity != null,
      json: async () => identity,
    };
  };
  vm.runInNewContext(source, {document, window, fetch, console}, {
    filename: "vps1_native_site_shell.js", timeout: 3000,
  });
  const tick = () => new Promise(resolve => setImmediate(resolve));
  return {
    body, calls, tick, document,
    links: () => all(body).filter(e => e.tag === "a").map(e => ({href:e.href, text:e.textContent})),
    account: () => all(body).find(e => e.className === "vps1-native-account"),
    update: async (next) => {identity = next;await winEvents.pageshow();},
    hideAndRestore: async () => {document.hidden=false;await listeners.visibilitychange();},
    voiceNavigation: (options={}) => {
      const link = all(body).find(e => e.tag === "a" && e.href === "/voice-studio/app#vps1-production");
      assert.ok(link, "canonical Voice Studio link missing");
      let prevented=false;
      const event = {button:0,defaultPrevented:false,
        preventDefault:()=>{prevented=true},...options};
      link.listeners?.click?.(event);
      return prevented;
    },
    enableVoiceTab: fn => {window.vps1VoiceStudioTab = fn},
    toggleMenu: () => all(body).find(e => e.className === "vps1-native-toggle")?.listeners?.click?.(),
    clickBackdrop: () => all(body).find(e => e.className === "vps1-native-nav-backdrop")?.listeners?.click?.(),
    clickNavigation: () => all(body).find(e => e.tag === "nav" && e.attrs["aria-label"] === "Applications")?.listeners?.click?.(),
    pressEscape: () => listeners.keydown?.({key:"Escape"}),
    resizeTo: width => {window.innerWidth = width;winEvents.resize?.()},
    isMenuOpen: () => body.classList.contains("vps1-native-open"),
    nativeToggle: () => all(body).find(e => e.className === "vps1-native-toggle"),
    nativeBackdrop: () => all(body).find(e => e.className === "vps1-native-nav-backdrop"),
  };
}

test("native sidebar style and role source remain shared across apps", () => {
  const stylesheet = fs.readFileSync(path.join(__dirname,
    "../apps/scene-builder/part01/static/vps1_native_site_shell.css"), "utf8");
  assert.match(stylesheet, /--vps1-sidebar-width:214px/);
  assert.match(stylesheet, /@view-transition\s*\{\s*navigation:\s*auto/);
  assert.match(stylesheet, /view-transition-name:vps1-global-sidebar/);
  assert.match(stylesheet, /#app-sidebar.sidebar,#vps1-native-site-sidebar/);
});

for (const workspace of ["uploader", "voice", "acs"]) {
  test(`${workspace}: administrator gets Admin link and Scene login footer`, async () => {
    const h = boot(workspace, {authenticated:true,is_admin:true,username:"owner"});
    await h.tick();
    assert.equal(h.links().filter(e => e.href === "/admin").length, 1);
    assert.equal(h.account().hidden, false);
    assert(h.calls.every(x => x.url === "/api/session/identity"));
    assert.equal(h.calls[0].options.credentials, "same-origin");
    assert.equal(h.calls[0].options.cache, "no-store");
    assert(h.links().some(e => e.href === "/scene-builder/"));
    assert.equal(h.links().filter(e => e.href === "/experiment-studio/" && e.text === "Experiment Studio").length,1);
    assert(all(h.body).some(e => e.tag === "form" && e.action === "/logout"));
    // Promotion/demotion changes the existing DOM without stale admin navigation.
    await h.update({authenticated:true,is_admin:false,username:"editor"});
    assert.equal(h.links().filter(e => e.href === "/admin").length, 0);
    assert.equal(h.account().hidden, false);
    await h.hideAndRestore();
    assert.equal(h.links().filter(e => e.href === "/admin").length, 0);
    await h.update({authenticated:true,is_admin:true,username:"owner"});
    assert.equal(h.links().filter(e => e.href === "/admin").length, 1);
    await h.update({authenticated:true,is_admin:true,username:"owner"});
    assert.equal(h.links().filter(e => e.href === "/admin").length, 1);
  });
  test(`${workspace}: non-admin or anonymous never sees Admin`, async () => {
    const h = boot(workspace, {authenticated:true,is_admin:false,username:"editor"});
    await h.tick();
    assert.equal(h.links().filter(e => e.href === "/admin").length, 0);
    await h.update(null);
    assert.equal(h.links().filter(e => e.href === "/admin").length, 0);
    assert.equal(h.account().hidden, false);
    assert.equal(h.account().children.find(e=>e.tag==="form").hidden, true);
  });
}

test("Self Media Library uses exactly one Experiment Studio parent and three subtabs", async () => {
  const h=boot("library",{authenticated:true,is_admin:false,username:"editor"});
  await h.tick();
  const nav=all(h.body).find(e=>e.tag==="nav"&&e.attrs["aria-label"]==="Applications");
  assert.ok(nav);
  assert.equal(nav.children.filter(e=>e.textContent==="Experiment Studio"&&e.attrs["aria-current"]==="page").length,1);
  assert.equal(nav.children.filter(e=>e.textContent==="Self Media Library").length,0);
  const tabs=all(h.body).find(e=>e.className==="vps1-native-experiment-tabs");
  assert.ok(tabs);
  assert.deepEqual(tabs.children.map(e=>e.textContent),["Antidetect Browser","Self Media Library","Auto Script"]);
  assert.deepEqual(tabs.children.map(e=>e.href),["/experiment-studio/antidetect-browser/","/self-media-library/","/experiment-studio/auto-script/"]);
  assert.equal(tabs.children.filter(e=>e.attrs["aria-current"]==="page").length,1);
  assert.equal(h.links().filter(e=>e.href==="/admin").length,0);
});

test("voice native menu uses in-page tab activation instead of document reload", async () => {
  const h=boot("voice",{authenticated:true,is_admin:true,username:"owner"});
  await h.tick();
  const chosen=[];
  h.enableVoiceTab(tab=>{chosen.push(tab);return false});
  assert.equal(h.voiceNavigation(),true);
  assert.deepEqual(chosen,["production"]);
  assert.equal(h.voiceNavigation({ctrlKey:true}),false);
  assert.equal(h.voiceNavigation({button:1}),false);
  assert.equal(h.voiceNavigation({metaKey:true}),false);
  assert.equal(h.voiceNavigation({defaultPrevented:true}),false);
  assert.deepEqual(chosen,["production"]);
  h.enableVoiceTab(null);
  assert.equal(h.voiceNavigation(),false);
});
test("other workspaces link directly to Voice Studio without interception", async () => {
  for(const workspace of ["acs","uploader"]){
    const h=boot(workspace,{authenticated:true,is_admin:false,username:"editor"});
    await h.tick();
    assert.equal(h.voiceNavigation(),false);
  }
});

for (const workspace of ["uploader","stream","voice","acs"]) {
  test(`${workspace}: one Upload Studio sidebar entry and native app subtabs`, async () => {
    const h=boot(workspace,{authenticated:true,is_admin:false,username:"editor"});
    await h.tick();
    const nav=all(h.body).find(e=>e.tag==="nav"&&e.attrs["aria-label"]==="Applications");
    assert.ok(nav);
    assert.equal(nav.children.filter(e=>e.href==="/local-upload-manager"&&e.textContent==="Upload Studio").length,1);
    assert.equal(nav.children.filter(e=>e.textContent==="Auto Uploader"||e.textContent==="Auto Stream").length,0);
    const tabs=all(h.body).find(e=>e.className==="vps1-native-upload-tabs");
    assert.equal(!!tabs,workspace==="uploader"||workspace==="stream");
    if(tabs){
      assert.deepEqual(tabs.children.map(e=>e.textContent),["Auto Uploader","Auto Stream"]);
      assert.deepEqual(tabs.children.map(e=>e.href),["/local-upload-manager/app?embedded=1&tab=dashboard","/auto-stream/"]);
      assert.equal(tabs.children.filter(e=>e.attrs["aria-current"]==="page").length,1);
    }
  });
}

test("native sidebar drawer geometry and accessibility stay stable across every independent app",async()=>{
  const stylesheet=fs.readFileSync(path.join(__dirname,
    "../apps/scene-builder/part01/static/vps1_native_site_shell.css"),"utf8");
  for(const key of ["--vps1-sidebar-width:214px","--vps1-mobile-drawer-width:min(84vw,300px)",
    "body.vps1-native-site > .vps1-native-toggle",
    "body.vps1-native-open > .vps1-native-nav-backdrop",
    "body.vps1-native-open {overflow:hidden",
    "body.vps1-native-site > :is(.vps1-native-video-tabs,.vps1-native-upload-tabs,.vps1-native-experiment-tabs)"]){
    assert.ok(stylesheet.includes(key),"Missing stable sidebar CSS "+key);
  }
  for(const app of ["library","acs","uploader","stream","voice"]){
    const h=boot(app,{authenticated:true,is_admin:false,username:"editor"});
    await h.tick();
    const toggles=all(h.body).filter(e=>e.className==="vps1-native-toggle");
    const backdrops=all(h.body).filter(e=>e.className==="vps1-native-nav-backdrop");
    const sidebars=all(h.body).filter(e=>e.id==="vps1-native-site-sidebar");
    assert.equal(toggles.length,1,app+" must have one toggle");
    assert.equal(backdrops.length,1,app+" must have one backdrop");
    assert.equal(sidebars.length,1,app+" must have one sidebar");
    assert.equal(h.nativeToggle().attrs["aria-controls"],"vps1-native-site-sidebar");
    assert.equal(h.nativeToggle().attrs["aria-expanded"],"false");
    h.toggleMenu();
    assert.equal(h.isMenuOpen(),true,app+" should open drawer");
    assert.equal(h.nativeToggle().attrs["aria-expanded"],"true");
    h.pressEscape();
    assert.equal(h.isMenuOpen(),false,app+" Escape must close drawer");
    h.toggleMenu();
    h.clickBackdrop();
    assert.equal(h.isMenuOpen(),false,app+" backdrop must close drawer");
    h.toggleMenu();
    h.clickNavigation();
    assert.equal(h.isMenuOpen(),false,app+" app navigation must close drawer");
    h.toggleMenu();
    h.resizeTo(1024);
    assert.equal(h.isMenuOpen(),false,app+" desktop resize must close drawer");
    h.toggleMenu();
    await h.update({authenticated:true,is_admin:false,username:"editor"});
    assert.equal(h.isMenuOpen(),false,app+" history restore must close drawer");
    assert.equal(h.nativeToggle().attrs["aria-expanded"],"false");
  }
});

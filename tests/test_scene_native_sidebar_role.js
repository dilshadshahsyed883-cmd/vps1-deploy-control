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
    this.classList = {add() {}, toggle() {}};
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
    assert(h.links().some(e => e.href === "/queue"));
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
    assert.equal(h.account().hidden, true);
  });
}

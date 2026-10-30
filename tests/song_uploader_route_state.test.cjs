'use strict';
/* Regression for the live bug: saved Upload Studio state redirected /song-uploader
   to /local-upload-manager/app. No browser, OAuth, VPS or upload required. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname,
  '../apps/local-upload-manager/part01/static/lum_ux_state_v2.js'), 'utf8');

function load(url, activeTab, initial = {}) {
  const location = new URL(url);
  const local = new Map(Object.entries(initial));
  const redirects = [];
  const root = location.pathname.startsWith('/song-uploader') ? '/song-uploader'
    : location.pathname.startsWith('/short-uploader') ? '/short-uploader'
    : '/local-upload-manager';
  const tabs = ['dashboard', 'channels', 'sheets', 'auto_engine', 'logs'].map(tab => ({
    href: 'https://app.scenebuilder.online' + root + '/app?tab=' + tab
  }));
  const activeLink = tabs.find(link => new URL(link.href).searchParams.get('tab') === activeTab);
  const doc = {
    readyState: 'complete',
    documentElement: null,
    body: {classList: {contains: () => false}},
    querySelector(q) { return q === '.lum-tabs a.active[href]' ? activeLink : null; },
    querySelectorAll(q) { return q === '.lum-tabs a[href]' ? tabs : []; },
    addEventListener() {}
  };
  const win = {
    location: {
      origin: location.origin,
      pathname: location.pathname,
      href: location.href,
      replace(next) { redirects.push(next); },
    },
    addEventListener() {},
    parent: null
  };
  win.parent = win;
  const context = {
    window: win,
    document: doc,
    history: {pushState() {}, replaceState() {}},
    localStorage: {
      getItem(k) { return local.has(k) ? local.get(k) : null; },
      setItem(k, v) { local.set(k, String(v)); },
      removeItem(k) { local.delete(k); }
    },
    MutationObserver: class { observe() {} },
    Element: class {},
    URL, setTimeout() {}, queueMicrotask(fn) { fn(); },
  };
  vm.runInNewContext(source, context, {filename: 'lum_ux_state_v2.js'});
  return {redirects, state: Object.fromEntries(local)};
}

test('direct Song Uploader ignores saved Upload Studio URL and opens its own Auto Engine', () => {
  const result = load('https://app.scenebuilder.online/song-uploader', 'auto_engine', {
    'lum.main.tab': 'channels',
    'lum.route': '/local-upload-manager/app?tab=channels',
  });
  assert.deepEqual(result.redirects, []);
  assert.equal(result.state['lum.song.main.tab'], 'auto_engine');
  assert.match(result.state['lum.song.route'], /^\/song-uploader\?tab=auto_engine$/);
  assert.equal(result.state['lum.route'], '/local-upload-manager/app?tab=channels');
});

test('Song URL explicitly requested tab stays in Song namespace', () => {
  const result = load('https://app.scenebuilder.online/song-uploader/app?tab=channels',
    'channels', {'lum.main.tab':'dashboard','lum.route':'/local-upload-manager/app?tab=dashboard'});
  assert.deepEqual(result.redirects, []);
  assert.equal(result.state['lum.song.main.tab'], 'channels');
});

test('even poisoned Song saved route cannot send a coerced tab to Upload Studio', () => {
  const result = load('https://app.scenebuilder.online/song-uploader/app?tab=upload',
    'dashboard', {
      'lum.song.main.tab': 'auto_engine',
      'lum.song.route': '/local-upload-manager/app?tab=auto_engine'
    });
  assert.equal(result.redirects.length, 1);
  assert.equal(new URL(result.redirects[0]).pathname, '/song-uploader/app');
  assert.equal(new URL(result.redirects[0]).searchParams.get('tab'), 'auto_engine');
});

test('original Upload Studio still restores its own saved tab and route', () => {
  const result = load('https://app.scenebuilder.online/local-upload-manager/app',
    'dashboard', {
      'lum.main.tab': 'channels',
      'lum.route': '/local-upload-manager/app?tab=channels'
    });
  assert.equal(result.redirects.length, 1);
  assert.equal(new URL(result.redirects[0]).pathname, '/local-upload-manager/app');
  assert.equal(new URL(result.redirects[0]).searchParams.get('tab'), 'channels');
});

test('Short Uploader never restores Song or Upload Studio route', () => {
  const result = load('https://app.scenebuilder.online/short-uploader/app?tab=upload',
    'dashboard', {
      'lum.main.tab':'auto_engine',
      'lum.route':'/local-upload-manager/app?tab=auto_engine',
      'lum.shortstudio.main.tab':'auto_engine',
      'lum.shortstudio.route':'/song-uploader/app?tab=auto_engine'
    });
  assert.equal(result.redirects.length, 1);
  assert.equal(new URL(result.redirects[0]).pathname, '/short-uploader/app');
});

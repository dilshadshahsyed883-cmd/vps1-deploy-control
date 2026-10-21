/* Persistent extension of the canonical native shell. No backend job writes. */
import {mount as mountStorage} from './storage_manager.js';
const registry = __SHELL_REGISTRY__;
const storageStyles = '__STORAGE_CSS__';
export const CONTRACT = 2;
let view = null, generation = 0, disposed = false;
const outlet = document.getElementById('vps1-shell-outlet');
const seed = document.getElementById('vps1-storage-seed');
const lifetime = new AbortController();
const own = new Set(['/workspace/', ...registry.apps.filter(x => x.mode === 'adapter').map(x => x.path)]);
const e = (tag, text) => {const node = document.createElement(tag); if (text) node.textContent = text; return node;};
function cleanup() {
  try {view?.unmount();} finally {try {view?.dispose();} finally {view = null;}}
}
function activeNavigation(path) {
  document.querySelectorAll('nav[aria-label="Persistent workspace"] a').forEach(link => {
    if (new URL(link.href).pathname === path) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}
function overview() {
  const section = e('section');
  section.append(e('h2', 'Workspace'));
  for (const app of registry.apps) {
    const link = e('a', app.id.replaceAll('-', ' '));
    link.href = app.path;
    const row = e('p'); row.append(link); section.append(row);
  }
  return section;
}
async function navigate(url, push = false) {
  const turn = ++generation;
  outlet.setAttribute('aria-busy', 'true');
  // Check identity before mounting any admin content. Backends also enforce roles.
  try {
    const res = await fetch('/api/session/identity', {credentials:'same-origin', cache:'no-store', signal:lifetime.signal});
    if (!res.ok || !(res.headers.get('content-type') || '').includes('application/json')) throw new Error('Sign in to continue');
    const identity = await res.json();
    if (identity.authenticated !== true || identity.is_admin !== true) throw new Error('Administrator access required');
    if (disposed || turn !== generation) return;
    cleanup();
    outlet.replaceChildren();
    if (url.pathname === '/workspace/') {
      outlet.append(overview());
    } else {
      const container = e('div'); container.dataset.appRoot = 'storage-pilot';
      const shadow = container.attachShadow({mode:'open'});
      const content = seed.content.cloneNode(true);
      if (content.querySelector('script, iframe, #app-sidebar, #vps1-native-site-sidebar')) throw new Error('Incompatible app content');
      content.querySelectorAll('link[rel="stylesheet"]').forEach(link => {link.href = storageStyles;});
      content.querySelectorAll('[href="/admin/storage"]').forEach(link => {link.href='/workspace/storage/';});
      const detail = content.querySelector('.sm-drive-detail');
      if (detail) detail.textContent = 'Each application verifies its own Drive delivery. This view only reads storage status.';
      const badge = content.querySelector('.sm-compact-indicator');
      if (badge) badge.textContent = 'Google Drive · app-owned verification';
      const style = e('style');
      style.textContent = ':host{display:block;min-width:0;--line:#e2e8f0;--muted:#64748b;--good:#116943;--bad:#b73146;font:14px system-ui;color:#0f172a}*{box-sizing:border-box}button,a{min-height:40px}button{cursor:pointer;padding:10px;border:1px solid var(--line);border-radius:8px;background:white;color:#18213b}a{color:#2457d6}button:focus-visible,a:focus-visible{outline:3px solid #2457d6;outline-offset:3px}';
      shadow.append(style, content); outlet.append(container);
      view = mountStorage(shadow, {identity, contract:CONTRACT});
    }
    if (push) history.pushState({shell:CONTRACT}, '', url);
    activeNavigation(url.pathname);
    document.title = url.pathname === '/workspace/' ? 'Workspace' : 'Storage Manager';
    outlet.focus({preventScroll:true});
  } catch (err) {
    if (disposed || turn !== generation) return;
    cleanup();
    const error = e('section'); error.setAttribute('role','alert');
    error.append(e('h2','Application unavailable'), e('p',err.message));
    const retry = e('button','Retry'); retry.type='button';
    retry.addEventListener('click', () => navigate(url, push), {once:true, signal:lifetime.signal});
    const legacy = e('a','Open supported Storage Manager page'); legacy.href='/admin/storage';
    error.append(retry, legacy); outlet.replaceChildren(error);
  } finally {
    if (turn === generation) outlet.removeAttribute('aria-busy');
  }
}
if (outlet && seed) {
  document.addEventListener('click', event => {
    const link = event.composedPath().find(node => node instanceof HTMLAnchorElement);
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || link.download || (link.target && link.target !== '_self')) return;
    const url = new URL(link.href);
    if (url.origin !== location.origin || !own.has(url.pathname)) return;
    event.preventDefault();
    if (url.href !== location.href) void navigate(url, true);
  }, {signal:lifetime.signal});
  window.addEventListener('popstate', () => {
    if (own.has(location.pathname)) void navigate(new URL(location.href));
    else location.reload();
  }, {signal:lifetime.signal});
  window.addEventListener('pagehide', event => {
    if (event.persisted) return; // bfcache resumes the same host and view.
    disposed = true; generation++; lifetime.abort(); cleanup();
  }, {once:true});
  void navigate(new URL(location.href));
}

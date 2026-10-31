/* Persistent extension of the canonical shell; app backends stay independent. */
import * as storage from "/static/shell-v2/storage_manager.gc-manual-20261031.js";
import * as dashboard from "/static/shell-v2/dashboard_control_center.4296b813993296e4a81d.js";
const registry = {"schema":2,"shell_version":2,"design_version":1,"geometry":{"sidebar":214,"mobile":760,"studio_max":1400,"rail":62},"apps":[{"id":"dashboard","path":"/","group":"general","mode":"adapter","owner":"dashboard","auth":"backend","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway","adapter":"dashboard","contract":2,"design":1,"role":"authenticated","smoke":"production.shell_v2_browser","legacy":"/?native=1","aliases":[]},{"id":"scene-builder","path":"/scene-builder/","group":"video","mode":"native-document","owner":"scene-builder","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"short-maker","path":"/short-maker/","group":"video","mode":"native-document","owner":"short-maker","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"video-merger","path":"/video-merger/","group":"video","mode":"native-document","owner":"video-merger","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"voice-studio","path":"/voice-studio/app","group":"voice","mode":"native-document","owner":"voice-studio","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"auto-caption-studio","path":"/auto-caption-studio/","group":"video","mode":"native-document","owner":"auto-caption-studio","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"auto-uploader","path":"/local-upload-manager/app","group":"upload","mode":"native-document","owner":"auto-uploader","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"auto-stream","path":"/auto-stream/","group":"upload","mode":"native-document","owner":"auto-stream","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"short-stream","path":"/short-stream/","group":"upload","mode":"native-document","owner":"short-stream","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"antidetect-browser","path":"/experiment-studio/antidetect-browser/","group":"experiment","mode":"native-document","owner":"antidetect-browser","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"self-media-library","path":"/self-media-library/","group":"experiment","mode":"native-document","owner":"self-media-library","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"admin","path":"/admin","group":"admin","mode":"alias","owner":"admin","auth":"backend","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway","target":"storage-manager"},{"id":"storage-manager","path":"/admin/storage","group":"admin","mode":"adapter","owner":"storage-manager","auth":"backend","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway","adapter":"storage","contract":2,"design":1,"role":"admin","smoke":"production.shell_v2_browser","legacy":"/admin/storage?native=1","aliases":["/workspace/storage/"]},{"id":"user-management","path":"/user-management","group":"admin","mode":"native-document","owner":"user-management","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"},{"id":"script-work","path":"/script-work","group":"video","mode":"native-document","owner":"script-work","auth":"backend","removal_condition":"Promote only after app-owned lifecycle adapter and authenticated browser regression pass","api":"preserved by existing gateway","assets":"preserved by existing gateway","websocket":"preserved by existing gateway"}]};
const styles = {"storage":"/static/shell-v2/storage_manager.2787c1e71b2d09bf0e46.css","dashboard":"/static/shell-v2/dashboard_control_center.522b61f874c0a1b3e96a.css"};
const adapters = {storage, dashboard};
export const CONTRACT = 2;
let view = null, generation = 0, disposed = false;
const outlet = document.getElementById('vps1-shell-outlet');
const lifetime = new AbortController();
const routes = new Map();
for (const app of registry.apps.filter(app => app.mode === 'adapter')) {
  for (const path of [app.path, ...(app.aliases || [])]) {
    if (routes.has(path)) throw new Error('Duplicate mounted route');
    routes.set(path, app);
  }
}
for (const alias of registry.apps.filter(app => app.mode === 'alias')) {
  const target = registry.apps.find(app => app.id === alias.target && app.mode === 'adapter');
  if (!target || routes.has(alias.path)) throw new Error('Invalid route alias');
  routes.set(alias.path, target);
}
const own = path => path === '/workspace/' || routes.has(path);
const e = (tag, text) => {const node = document.createElement(tag); if (text) node.textContent = text; return node;};
function cleanup() {
  try {view?.unmount();} finally {try {view?.dispose();} finally {view = null;}}
}
function activeNavigation(url, app) {
  const adminNavigation = document.getElementById('vps1-admin-navigation');
  if (adminNavigation) {
    adminNavigation.hidden = app?.group !== 'admin';
    adminNavigation.querySelectorAll('a').forEach(link => {
      const selected = new URL(link.href).pathname === app?.path;
      link.classList.toggle('active', selected);
      if (selected) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
  }
  document.querySelectorAll('#app-sidebar > nav > a').forEach(link => {
    const selected = app?.id === 'dashboard' ? new URL(link.href).pathname === '/' :
      app?.group === 'admin' && new URL(link.href).pathname === '/admin';
    link.classList.toggle('active', Boolean(selected));
    if (selected) link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
  });
}
function overview(identity) {
  const section = e('section'); section.append(e('h2', 'Applications'));
  for (const app of registry.apps) {
    if ((app.role === 'admin' || app.group === 'admin') && !identity.is_admin) continue;
    const link = e('a', app.id.replaceAll('-', ' ')); link.href = app.path;
    const row = e('p'); row.append(link); section.append(row);
  }
  return section;
}
function mountApplication(app, identity) {
  const adapter = adapters[app.adapter];
  if (!adapter || adapter.CONTRACT !== CONTRACT) throw new Error('Incompatible application adapter');
  const seed = document.getElementById(`vps1-${app.adapter}-seed`);
  if (!seed) throw new Error('Application content unavailable');
  const container = e('div'); container.dataset.appRoot = app.id;
  const shadow = container.attachShadow({mode:'open'});
  const content = seed.content.cloneNode(true);
  if (content.querySelector('script, iframe, #app-sidebar, #vps1-native-site-sidebar')) throw new Error('Incompatible app content');
  // Global Admin navigation belongs to the shell, never inside app content.
  content.querySelector('nav[aria-label="Administration sections"]')?.remove();
  content.querySelector('#vps1-admin-subtab-ui-v2')?.remove();
  content.querySelectorAll('link[rel="stylesheet"]').forEach(link => {link.href = styles[app.adapter];});
  const css = e('link'); css.rel='stylesheet'; css.href=styles[app.adapter];
  const style = e('style');
  style.textContent = ':host{display:block;min-width:0;--line:#e2e8f0;--muted:#64748b;--good:#116943;--bad:#b73146;font:14px system-ui;color:#0f172a}*{box-sizing:border-box}button,a{min-height:40px}button{cursor:pointer;padding:10px;border:1px solid var(--line);border-radius:8px;background:white;color:#18213b}a{color:#2457d6}button:focus-visible,a:focus-visible,select:focus-visible{outline:3px solid #2457d6;outline-offset:3px}';
  if (app.adapter === 'dashboard') style.textContent += ':host{background:#07111f;border-radius:12px;padding-top:16px}.dashboard-refresh{flex-wrap:wrap}';
  if (app.adapter === 'storage') {
    const detail = content.querySelector('.sm-drive-detail');
    if (detail) detail.textContent = 'Each application verifies its own Drive delivery. This view only reads storage status.';
    const badge = content.querySelector('.sm-compact-indicator');
    if (badge) badge.textContent = 'Google Drive · app-owned verification';
  }
  shadow.append(style, css, content); outlet.append(container);
  view = adapter.mount(shadow, {identity, contract:CONTRACT});
  if (typeof view?.unmount !== 'function' || typeof view?.dispose !== 'function') throw new Error('Incomplete app lifecycle');
}
async function navigate(url, push = false) {
  const turn = ++generation, app = routes.get(url.pathname);
  outlet.setAttribute('aria-busy', 'true');
  try {
    const res = await fetch('/api/session/identity', {credentials:'same-origin',cache:'no-store',signal:lifetime.signal});
    if (!res.ok || !(res.headers.get('content-type') || '').includes('application/json')) throw new Error('Sign in to continue');
    const identity = await res.json();
    window.dispatchEvent(new Event('vps1:session-refresh')); // Existing shell owns identity display.
    if (identity.authenticated !== true) throw new Error('Sign in to continue');
    if (app?.role === 'admin' && identity.is_admin !== true) throw new Error('Administrator access required');
    if (disposed || turn !== generation) return;
    cleanup(); outlet.replaceChildren();
    if (app) mountApplication(app, identity);else outlet.append(overview(identity));
    if (push) history.pushState({shell:CONTRACT}, '', routes.has(url.pathname) && app ? app.path + url.search + url.hash : url);
    activeNavigation(url, app);
    const title = app ? (app.id === 'dashboard' ? 'Dashboard' : 'Storage Manager') : 'Applications';
    document.title = title;
    const heading = document.querySelector('main > header h1'); if (heading) heading.textContent=title;
    outlet.focus({preventScroll:true});
  } catch (err) {
    if (disposed || turn !== generation) return;
    cleanup();
    const adminNavigation = document.getElementById('vps1-admin-navigation');
    if (adminNavigation) adminNavigation.hidden = true;
    const error = e('section'); error.setAttribute('role','alert');
    error.append(e('h2','Application unavailable'),e('p',err.message));
    const retry = e('button','Retry'); retry.type='button';
    retry.addEventListener('click',()=>navigate(url,push),{once:true,signal:lifetime.signal});
    const legacy = e('a','Open supported application page'); legacy.href=app?.legacy || '/?native=1';
    error.append(retry,legacy);outlet.replaceChildren(error);
  } finally {if (turn === generation) outlet.removeAttribute('aria-busy');}
}
if (outlet) {
  document.addEventListener('click',event=>{
    const link=event.composedPath().find(node=>node instanceof HTMLAnchorElement);
    if (!link || event.defaultPrevented || event.button!==0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || link.download || (link.target && link.target!=='_self')) return;
    const url=new URL(link.href);
    if (url.origin!==location.origin || !own(url.pathname) || url.searchParams.get('native')==='1') return;
    event.preventDefault();if(url.href!==location.href)void navigate(url,true);
  },{signal:lifetime.signal});
  window.addEventListener('popstate',()=>{
    if(own(location.pathname))void navigate(new URL(location.href));else location.reload();
  },{signal:lifetime.signal});
  document.addEventListener('visibilitychange',()=>{
    if(!document.hidden)void navigate(new URL(location.href));
  },{signal:lifetime.signal});
  window.addEventListener('pageshow',event=>{if(event.persisted)void navigate(new URL(location.href));},{signal:lifetime.signal});
  window.addEventListener('pagehide',event=>{
    if(event.persisted)return;disposed=true;generation++;lifetime.abort();cleanup();
  },{once:true});
  void navigate(new URL(location.href));
}

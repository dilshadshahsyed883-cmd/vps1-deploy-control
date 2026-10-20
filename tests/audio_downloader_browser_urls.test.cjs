// Regression: execute the actual inline Audio Downloader scripts AFTER emulated
// production Nginx sub_filter. Covers Queue, History, submit/status and proxy.
'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const text = fs.readFileSync('apps/voice-studio/part01/templates/voice_studio.html', 'utf8');
const origin = 'https://app.scenebuilder.online';
const apiRoot = '/voice-studio/tts/api/audio-downloader';
const calls = [];
const nodes = new Map();
function makeElement(tag='div') {
  return {
    tag, children: [], style: {}, dataset: {}, classList: {toggle(){}, add(){}, remove(){}},
    value: '', checked: false, hidden: false, disabled: false, textContent: '',
    append(...xs) { this.children.push(...xs); },
    appendChild(x) { this.children.push(x); },
    replaceChildren(...xs) { this.children = [...xs]; },
    addEventListener() {}, setAttribute() {}, remove() {}, click() {},
  };
}
function getElementById(id) {
  if (!nodes.has(id)) nodes.set(id, makeElement(id));
  return nodes.get(id);
}
const document = {
  readyState: 'loading', addEventListener() {}, createElement: makeElement,
  getElementById, querySelectorAll() { return []; },
  body: makeElement('body'), documentElement: makeElement('html'),
};
const location = {pathname: '/voice-studio/app', hash: '#vs-audio', href: origin + '/voice-studio/app'};
const window = {};
async function fetch(url, options={}) {
  calls.push({url,method:options.method || 'GET'});
  const path = new URL(url, origin).pathname;
  let response;
  if (path === apiRoot + '/jobs' && (options.method || 'GET') === 'POST') response = {created:1,batch_id:'TEST'};
  else if (path === apiRoot + '/jobs') response = {jobs:[]};
  else if (path === apiRoot + '/status') response = {ready:true,worker:true,upload_ready:true,queue:{}};
  else if (path === apiRoot + '/proxy') response = {profiles:[],enabled:false,origin:'independent'};
  else if (path === apiRoot + '/proxy/profiles') response = {profiles:[],enabled:false};
  else throw new Error('Browser generated unexpected URL: ' + url);
  return {ok:true,status:200,headers:{get(name){return name.toLowerCase()==='content-type'?'application/json':null}},async json(){return response}};
}
const context = vm.createContext({
  document, location, window, history:{replaceState(){}}, fetch, URL, FormData,
  setInterval(){return 1}, clearInterval(){}, setTimeout(){}, console, confirm(){return true},
});
function getScript(id) {
  const start = '<script id="' + id + '">';
  const begin = text.indexOf(start);
  assert.ok(begin !== -1,'missing '+id);
  const end = text.indexOf('</script>',begin);
  assert.ok(end > begin);
  return text.slice(begin + start.length,end);
}
function emulateNginx(source) {
  // These replacements reproduce the actual /voice-studio/jobs 404 captured
  // from the browser; the new scripts must remain unchanged.
  return source
    .replaceAll("'/jobs", "'/voice-studio/jobs")
    .replaceAll("'/status", "'/voice-studio/status")
    .replaceAll("'/profiles", "'/voice-studio/profiles")
    .replaceAll("'/enable", "'/voice-studio/enable");
}
function runScript(id, exports) {
  const source = getScript(id);
  assert.equal(emulateNginx(source),source,'Nginx still rewrites '+id);
  const end = source.lastIndexOf('})();');
  assert.ok(end > 0,'missing script IIFE '+id);
  const evaluated = source.slice(0,end) + 'globalThis.__browserProbe = {' + exports + '};\n' + source.slice(end);
  new vm.Script(evaluated,{filename:id}).runInContext(context);
  return context.__browserProbe;
}
async function check() {
  const audio = runScript('audio-downloader-v1-js','loadJobs,loadStatus,submit,jobTable');
  calls.length = 0;
  await audio.loadJobs();
  assert.deepEqual(calls.map(x=>x.url),[apiRoot+'/jobs?limit=200'],'Queue/History fetch must resolve once');
  calls.length = 0;
  await audio.loadStatus();
  assert.deepEqual(calls.map(x=>x.url),[apiRoot+'/status'],'status route must resolve once');
  getElementById('vsaLinks').value = 'https://www.youtube.com/watch?v=ABCDEFGHIJK';
  getElementById('vsaPermission').checked = true;
  getElementById('vsaQuality').value = '192';
  getElementById('vsaFormat').value = 'mp3';
  calls.length = 0;
  await audio.submit();
  assert.ok(calls.some(x=>x.url===apiRoot+'/jobs' && x.method==='POST'),'submit route');
  assert.ok(calls.filter(x=>x.url===apiRoot+'/jobs?limit=200').length >= 1,'queue refresh');
  // Exercise actual Cancel and Retry button callbacks via DOM-inserted controls.
  const queued = {job_id:'ADJ-AAAAAAAAAAAAAAAA',status:'queued',created_at:'2026-10-20T00:00:00Z'};
  const failed = {...queued,job_id:'ADJ-BBBBBBBBBBBBBBBB',status:'failed'};
  for (const job of [queued,failed]) {
    const controls = [];
    const saveAdd = document.createElement;
    document.createElement = function(tag) {
      const item = saveAdd(tag);
      if (tag==='button') {
        item.addEventListener = function(event,callback){if(event==='click') controls.push({label:item.textContent,callback})};
      }
      return item;
    };
    audio.jobTable([job],1);
    document.createElement = saveAdd;
    assert.equal(controls.length,1,'job should expose one action');
    // Backend mutating requests are mocked; no real job is created.
    calls.length = 0;
    await controls[0].callback();
    assert.ok(calls.some(x=>x.url===apiRoot+'/jobs/'+job.job_id+'/'+(job.status==='queued'?'cancel':'retry') && x.method==='POST'));
  }
  const proxy = runScript('audio-downloader-proxy-v1','request,load');
  calls.length = 0;
  await proxy.load();
  assert.deepEqual(calls.map(x=>x.url),[apiRoot+'/proxy']);
  calls.length = 0;
  await proxy.request('/profiles');
  assert.deepEqual(calls.map(x=>x.url),[apiRoot+'/proxy/profiles']);
  console.log('PASS: actual post-Nginx Queue, History, Status, Submit, Cancel, Retry, Proxy browser paths');
}
check().catch(e=>{console.error(e.stack||e);process.exitCode=1});
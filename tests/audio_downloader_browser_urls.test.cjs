// Regression: execute the actual inline Audio Downloader scripts AFTER emulated
// production Nginx sub_filter. Covers the consolidated Queue, submit/status and proxy.
'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const text = fs.readFileSync('apps/voice-studio/part01/templates/voice_studio.html', 'utf8');
const origin = 'https://app.scenebuilder.online';
const apiRoot = '/voice-studio/tts/api/audio-downloader';
const calls = [];
const nodes = new Map();
const localOptions = new Map();
let serverOptions = null;
let queueJobs = [];
function makeElement(tag='div') {
  return {
    tag, children: [], style: {}, dataset: {}, classList: {toggle(){}, add(){}, remove(){}},
    value: '', checked: false, hidden: false, disabled: false, textContent: '',
    append(...xs) { this.children.push(...xs); },
    appendChild(x) { this.children.push(x); },
    replaceChildren(...xs) { this.children = [...xs]; },
    addEventListener(event, callback) { (this.listeners ||= {})[event] = callback; },
    fire(event) { return this.listeners?.[event]?.({preventDefault(){},stopPropagation(){}}); },
    setAttribute() {}, remove() {}, click() {},
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
  else if (path === apiRoot + '/jobs') response = {jobs:queueJobs};
  else if (path === apiRoot + '/status') response = {ready:true,worker:true,upload_ready:true,queue:{}};
  else if (path === apiRoot + '/preferences' && (options.method || 'GET') === 'GET') {
    response = {saved:!!serverOptions,preferences:serverOptions};
  }
  else if (path === apiRoot + '/preferences' && options.method === 'PUT') {
    serverOptions = JSON.parse(options.body);
    response = {saved:true,preferences:serverOptions};
  }
  else if (path.startsWith(apiRoot + '/jobs/') && /\/(cancel|retry)$/.test(path) && options.method === 'POST') response = {ok:true};
  else if (path.startsWith(apiRoot + '/jobs/') && options.method === 'DELETE') response = {hidden:true};
  else if (path === apiRoot + '/proxy') response = {profiles:[],enabled:false,origin:'independent'};
  else if (path === apiRoot + '/proxy/profiles') response = {profiles:[],enabled:false};
  else throw new Error('Browser generated unexpected URL: ' + url);
  return {ok:true,status:200,headers:{get(name){return name.toLowerCase()==='content-type'?'application/json':null}},async json(){return response}};
}
const context = vm.createContext({
  document, location, window, history:{replaceState(){}}, fetch, URL, FormData,
  setInterval(){return 1}, clearInterval(){}, setTimeout(){}, console, confirm(){return true},
  localStorage:{getItem(k){return localOptions.get(k)||null},setItem(k,v){localOptions.set(k,v)}},
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
  const audio = runScript('audio-downloader-v1-js','loadJobs,loadStatus,submit,jobTable,tab,boot,applyDownloadPreferences,downloadPreferences,saveDownloadPreferences,loadDownloadPreferences');
  calls.length = 0;
  await audio.loadJobs();
  assert.deepEqual(calls.map(x=>x.url),[apiRoot+'/jobs?limit=200'],'Queue fetch must resolve once');
  for (const retired of ['data-vsa-section="drive"','data-vsa-section="history"','id="vsaRecent"','id="vsaHistory"','id="vsaDrive"','vsaHistoryRows','vsaDriveSummary']) {
    assert.ok(!text.includes(retired), 'retired UI must not remain: '+retired);
  }
  assert.deepEqual((text.match(/data-vsa-section="[^"]+"/g)||[]),[
    'data-vsa-section="downloader"','data-vsa-section="queue"','data-vsa-section="proxy"']);
  audio.tab('queue');
  assert.equal(getElementById('vsaQueue').hidden,false);
  assert.equal(getElementById('vsaDownloader').hidden,true);
  audio.tab('proxy');
  assert.equal(getElementById('vsaProxy').hidden,false);
  audio.tab('downloader');
  assert.equal(getElementById('vsaDownloader').hidden,false);
  assert.equal(getElementById('vsaQueue').hidden,true);
  // Completed, failed and queued jobs are all rendered in the single Queue page.
  queueJobs=[
    {job_id:'ADJ-CCCCCCCCCCCCCCCC',video_id:'ABCDEFGHIJK',status:'completed',format:'mp3',created_at:'2026-10-30T05:00:00Z',auto_upload:false},
    {job_id:'ADJ-DDDDDDDDDDDDDDDD',video_id:'ABCDEFGHIJK',status:'failed',format:'mp3',created_at:'2026-10-30T05:01:00Z',auto_upload:true},
    {job_id:'ADJ-EEEEEEEEEEEEEEEE',video_id:'ABCDEFGHIJK',status:'queued',format:'mp3',created_at:'2026-10-30T05:02:00Z',auto_upload:false}
  ];
  await audio.loadJobs();
  const rendered=getElementById('vsaQueueRows').children[0];
  assert.equal(rendered.children[0].children[1].children.length,3,'Queue must show current and terminal jobs together');
  queueJobs=[];
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
    assert.equal(controls.length,job.status==='queued'?1:2,
      'active jobs offer Cancel; terminal failures offer Retry and Delete History');
    // Backend mutating requests are mocked; no real job is created.
    calls.length = 0;
    await controls[0].callback();
    assert.ok(calls.some(x=>x.url===apiRoot+'/jobs/'+job.job_id+'/'+(job.status==='queued'?'cancel':'retry') && x.method==='POST'));
    if(job.status==='failed'){
      calls.length=0;
      await controls[1].callback();
      assert.ok(calls.some(x=>x.url===apiRoot+'/jobs/'+job.job_id && x.method==='DELETE'),
        'terminal history deletion must use the owner-scoped DELETE endpoint');
    }
  }
  function contents(node){return [node.textContent,...node.children.map(contents)].join(' ');}
  const authFailed = {...failed, auto_upload:true, error:'YOUTUBE_AUTH_REQUIRED: blocked'};
  const downloadText = contents(audio.jobTable([authFailed],1));
  assert.ok(downloadText.includes('Download failed · upload not started'));
  assert.ok(!downloadText.includes('Upload failed · manual Retry'));
  const driveFailed = {...failed,auto_upload:true,error:'Google Drive upload failed (AUTH_REQUIRED)'};
  assert.ok(contents(audio.jobTable([driveFailed],1)).includes('Upload failed · manual Retry'));
  // Persist all user-selectable fields, restoring them after a fresh page script.
  audio.applyDownloadPreferences({format:'wav',quality:256,auto_upload:false,
    download_thumbnail:true,auto_song_maker:true,keep_metadata:true});
  await audio.saveDownloadPreferences();
  assert.equal(serverOptions.auto_song_maker,true);
  assert.equal(serverOptions.auto_upload,false);
  assert.equal(serverOptions.format,'wav');
  await audio.loadStatus();
  await audio.loadJobs();
  const reloaded = runScript('audio-downloader-v1-js','loadDownloadPreferences,downloadPreferences,boot,saveDownloadPreferences');
  // Simulate real HTML defaults on refresh before preferences hydrate.
  getElementById('vsaSongMaker').checked=false;
  getElementById('vsaUpload').checked=true;
  getElementById('vsaThumbnail').checked=false;
  getElementById('vsaMetadata').checked=false;
  getElementById('vsaFormat').value='mp3';
  getElementById('vsaQuality').value='192';
  await reloaded.loadDownloadPreferences();
  assert.equal(JSON.stringify(reloaded.downloadPreferences()),JSON.stringify(serverOptions),
    'page refresh must restore the persisted selection rather than HTML defaults');
  // The actual user toggle handler enforces the dependency, and autosaves.
  location.hash = '';
  reloaded.boot();
  getElementById('vsaUpload').checked=true;
  getElementById('vsaUpload').fire('change');
  assert.equal(getElementById('vsaSongMaker').checked,false,
    'manually enabling Drive upload disables conflicting Song Maker auto queue');
  await reloaded.saveDownloadPreferences();
  assert.equal(serverOptions.auto_song_maker,false);
  getElementById('vsaSongMaker').checked=true;
  getElementById('vsaSongMaker').fire('change');
  assert.equal(getElementById('vsaUpload').checked,false);
  assert.equal(getElementById('vsaThumbnail').checked,true);
  await reloaded.saveDownloadPreferences();
  assert.equal(serverOptions.auto_song_maker,true);
  assert.equal(serverOptions.download_thumbnail,true);
  assert.equal(serverOptions.auto_upload,false);
  const proxy = runScript('audio-downloader-proxy-v1','request,load');
  calls.length = 0;
  await proxy.load();
  assert.deepEqual(calls.map(x=>x.url),[apiRoot+'/proxy']);
  calls.length = 0;
  await proxy.request('/profiles');
  assert.deepEqual(calls.map(x=>x.url),[apiRoot+'/proxy/profiles']);
  console.log('PASS: three-tab Audio UI, consolidated Queue history, saved preferences and authenticated browser paths');
}
check().catch(e=>{console.error(e.stack||e);process.exitCode=1});
"""Independent Auto Stream V2 control screen. Existing Short Stream UI intact."""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/engine", tags=["Auto Stream Studio"])

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auto Stream — Live Studio</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;color-scheme:light;color:#172136}
*{box-sizing:border-box}body{margin:0;background:#f5f6fb}header{padding:16px 25px;background:#141a31;color:#fff;display:flex;align-items:center;justify-content:space-between}
header a{color:#b4c8ff;text-decoration:none}main{max-width:1160px;margin:24px auto;padding:0 16px}
h1{font-size:23px;margin:0}h2{font-size:17px;margin:0 0 12px}
nav{display:flex;gap:9px;flex-wrap:wrap;margin:18px 0}nav button{min-width:120px}
button{border:1px solid #c5cce0;background:#fff;color:#172136;padding:11px 15px;border-radius:8px;cursor:pointer;font-weight:600}
button.primary{background:#5250d9;color:#fff;border-color:#5250d9}button:disabled{opacity:.55;cursor:not-allowed}
nav button[aria-selected=true]{background:#5250d9;color:#fff;border-color:#5250d9}
.panel{background:#fff;border:1px solid #e2e5f0;border-radius:12px;padding:18px;margin-top:12px;box-shadow:0 2px 10px #161c3008}
[hidden]{display:none!important}label{display:block;font-size:13px;font-weight:650;margin:12px 0 5px}
input,select{border:1px solid #c5cce0;border-radius:8px;padding:10px;font:inherit;max-width:100%;width:100%}
small,.muted{color:#627087}p{line-height:1.5}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.item{border:1px solid #e2e5f0;border-radius:9px;padding:12px;margin:9px 0}
.row{display:flex;align-items:center;flex-wrap:wrap;gap:9px}.row>*{flex:0 0 auto}
.message{white-space:pre-wrap;border-radius:8px;padding:12px;background:#edf1fb;margin:12px 0}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;overflow-wrap:anywhere}
</style></head>
<body><header><div><h1>Auto Stream</h1><small style="color:#bac4e1">Independent YouTube Live application</small></div><a href="/">Scene Builder Dashboard</a></header>
<main>
<div class="panel"><strong>Auto Stream Studio V3</strong><p id="health">Loading engine status...</p><small>Google access is separate from Auto Uploader. No upload jobs or streams start from this screen.</small></div>
<nav aria-label="Auto Stream workspace tabs">
<button data-tab="dashboard" aria-selected="true">Dashboard</button>
<button data-tab="channels" aria-selected="false">Channels</button>
<button id="oauth-settings-tab" data-tab="oauth-settings" aria-selected="false" hidden>OAuth Setup</button>
<button data-tab="sheets" aria-selected="false">Sheets Sync</button>
<button data-tab="studio" aria-selected="false">Stream Studio</button>
<button data-tab="live" aria-selected="false">YouTube Live</button>
</nav>
<section id="dashboard" class="panel" role="tabpanel">
<h2>Streaming dashboard</h2><div id="metrics" class="message">Checking...</div>
<p class="muted">Live publishing and automatic streaming remain disabled until they are separately authorized and verified.</p>
</section>
<section id="channels" class="panel" role="tabpanel" hidden>
<h2>YouTube Channels</h2>
<p class="muted">Use a separate Google OAuth connection for each chosen YouTube channel. Importing a channel ID does not grant live access.</p>
<div class="row"><button id="connect" class="primary">Connect YouTube + Sheets (Google OAuth)</button><button id="reload-channels">Refresh channels</button></div>
<div id="channel-list"></div>
<h2>Import channel metadata</h2>
<div class="grid"><div><label for="cid">YouTube channel ID</label><input id="cid" placeholder="UC..."></div><div><label for="ctitle">Channel name</label><input id="ctitle" placeholder="My YouTube Channel"></div></div>
<p><button id="import-channel">Import metadata only</button></p>
</section>
<section id="oauth-settings" class="panel" role="tabpanel" hidden>
<h2>Google OAuth Setup — Admin Only</h2>
<p>Connect a separate Google Cloud <strong>Web application OAuth client</strong> to Auto Stream. This does not use Auto Uploader credentials.</p>
<p><a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer">Google Cloud Console — Credentials</a></p>
<p class="muted">Enable YouTube Data API v3 and Google Sheets API. Add this exact Authorized redirect URI in Google Cloud:</p>
<div class="row"><code id="oauth-redirect">https://app.scenebuilder.online/short-stream/engine/oauth/callback</code><button id="copy-oauth-redirect" type="button">Copy</button></div>
<p id="oauth-client-state" class="message">Checking Google OAuth client configuration...</p>
<form id="oauth-client-form" autocomplete="off">
<div class="grid"><div><label for="oauth-client-id">Client ID</label><input id="oauth-client-id" name="oauth_client_id" type="text" autocomplete="off" maxlength="255" required placeholder="123-abc.apps.googleusercontent.com"></div>
<div><label for="oauth-client-secret">Client Secret</label><input id="oauth-client-secret" name="oauth_client_secret" type="password" autocomplete="new-password" maxlength="512" required placeholder="Google OAuth Client Secret"></div></div>
<p><button type="submit" class="primary">Save OAuth Credentials</button> <button id="remove-oauth-client" type="button">Remove Saved Client</button></p></form>
<small>Secret encrypted on VPS. After saving it is never shown again. Existing channel access and broadcasting controls are not enabled by this form.</small>
</section>
<section id="sheets" class="panel" role="tabpanel" hidden>
<h2>Google Sheets Sync</h2><p class="muted">Read-only Sheets; synced rows create DRAFT jobs only. Dry-run preview is default.</p>
<div class="grid"><div><label for="sheet-channel">Authorized channel</label><select id="sheet-channel"></select></div>
<div><label for="sheet-id">Spreadsheet ID</label><input id="sheet-id" placeholder="Google Sheet ID"></div>
<div><label for="sheet-range">A1 range</label><input id="sheet-range" value="Sheet1!A1:Z200"></div></div>
<p><button id="add-sheet" class="primary">Link read-only Sheet</button></p>
<div id="sheet-list"></div>
<p class="muted">Required row columns: channel_id, media_path, title, scheduled_at, enabled. Optional: continuous. Times must include timezone offset.</p>
</section>
<section id="studio" class="panel" role="tabpanel" hidden>
<h2>Streaming Jobs — Private YouTube Broadcast Preparation</h2>
<p class="muted">Create and prepare draft jobs independently. YouTube broadcast creation, ingest and Live transitions are disabled unless explicitly enabled on the VPS. No stream starts when you open this tab.</p>
<div class="grid">
<div><label for="job-channel">Authorized channel</label><select id="job-channel"></select></div>
<div><label for="job-media">Existing media file (relative path)</label><input id="job-media" placeholder="auto_stream_uploads/example.mp4"></div>
<div><label for="job-title">Broadcast title</label><input id="job-title" maxlength="100" placeholder="Scheduled private livestream"></div>
<div><label for="job-scheduled">Scheduled start time</label><input id="job-scheduled" type="datetime-local"></div>
</div>
<p><button id="create-job" class="primary">Create DRAFT Job</button> <button id="reload-jobs">Refresh Jobs</button></p>
<p id="live-controls-status" class="message">Checking YouTube mutation/transition gates...</p>
<div id="job-list"></div>
</section>
<section id="live" class="panel" role="tabpanel" hidden>
<h2>YouTube Live Status</h2><p class="muted">Verified directly against the YouTube Live API; a healthy encoder alone does not count as LIVE.</p>
<div class="grid"><div><label for="live-channel">Authorized channel</label><select id="live-channel"></select></div></div>
<p><button id="refresh-live" class="primary">Read YouTube Live Status</button></p>
<div id="live-list"></div>
</section>
<div id="msg" role="status" aria-live="polite"></div>
</main>
<script>
(function(){
"use strict";
// Public browser location is /auto-stream/; the independent engine keeps its own API namespace.
const root="/short-stream/engine";
const $=id=>document.getElementById(id);
function note(s){const m=$("msg");m.className="message";m.textContent=s;}
async function call(path,method,body){
 const res=await fetch(root+path,{method:method||"GET",credentials:"same-origin",
  headers:Object.assign({},body?{"Content-Type":"application/json"}:{}, (path==="/oauth/client" && method!=="GET")?{"X-Auto-Stream-Config-Action":"1"}:{}),
  body:body?JSON.stringify(body):undefined});
 const payload=await res.json();
 if(!res.ok)throw Error(typeof payload.detail==="string"?payload.detail:"Request failed");
 return payload;
}
function renderItems(id,items,fn){
 const parent=$(id);parent.replaceChildren();
 if(!items.length){const p=document.createElement("p");p.className="muted";p.textContent="None yet";parent.appendChild(p);return}
 items.forEach(x=>{const div=document.createElement("div");div.className="item";fn(div,x);parent.appendChild(div)});
}
function txt(div,s,cls){const el=document.createElement("p");if(cls)el.className=cls;el.textContent=s;div.appendChild(el);return el;}
let channels=[];
let ytFlags={};
async function refreshChannels(){
 channels=(await call("/channels")).channels;
 renderItems("channel-list",channels,(el,c)=>{
   txt(el,c.title+" — "+c.id);
   txt(el,c.authorized?"Google OAuth connected":"Metadata only — authorization required","muted");
   if(c.authorized){const b=document.createElement("button");b.textContent="Disconnect from Auto Stream";
     b.onclick=async()=>{if(!confirm("Remove Auto Stream authorization for this channel?"))return;
       try{await call("/channels/"+encodeURIComponent(c.id)+"/disconnect","POST");await refreshChannels();note("Auto Stream authorization removed locally");}catch(e){note(e.message)}};
     el.appendChild(b);}
 });
 for(const name of ["sheet-channel","live-channel","job-channel"]){
  const s=$(name);s.replaceChildren();
  channels.filter(c=>c.authorized).forEach(c=>{
    const opt=document.createElement("option");opt.value=c.id;opt.textContent=c.title+" ("+c.id+")";s.appendChild(opt);
  });
 }
}
async function refreshSheets(){
 const s=(await call("/sheets")).sources;
 renderItems("sheet-list",s,(el,row)=>{
 txt(el,row.channel_id+" — "+row.spreadsheet_id+" — "+row.a1_range,"mono");
 const preview=document.createElement("button");preview.textContent="Preview Sync";
 const apply=document.createElement("button");apply.textContent="Import DRAFT jobs";
 for(const item of [[preview,true],[apply,false]])item[0].onclick=async()=>{
   if(!item[1]&&!confirm("Import valid scheduled rows as DRAFT jobs? No streams will start."))return;
   try{const r=await call("/sheets/"+encodeURIComponent(row.id)+"/sync?dry_run="+item[1],"POST");
     note("Valid: "+r.total+"; Drafts created: "+r.created+"; Row errors: "+r.errors.length+
      (r.errors.length?"\\n"+r.errors.map(e=>"Row "+e.row+": "+e.reason).join("\\n"):""));
   }catch(e){note(e.message)}
 };
 el.appendChild(preview);el.appendChild(apply);
 });
}
async function refreshJobs(){
 const jobs=(await call("/jobs")).jobs;
 renderItems("job-list",jobs,(el,job)=>{
   txt(el,job.title+" — "+job.id);
   txt(el,"Channel: "+job.channel_id+" | Local job: "+job.status+
      " | Start: "+(job.scheduled_at||"manual"),"muted");
   const bar=document.createElement("div");bar.className="row";
   const action=(text,path,options={})=>{
     const btn=document.createElement("button");
     btn.textContent=text;
     btn.disabled=!!options.disabled;
     btn.onclick=async()=>{
       if(options.confirm&&!confirm(options.confirm))return;
       try{
         const result=await call("/jobs/"+encodeURIComponent(job.id)+path,"POST",options.body);
         note(text+" succeeded for "+job.id+"; verify status before further actions.");
         if(result.youtube_verified_live===true)note("YouTube verified Live: "+job.id);
         await refreshJobs();
       }catch(e){note(e.message)}
     };
     bar.appendChild(btn);
   };
   action("Prepare Job","/prepare",{disabled:!["DRAFT","READY","SCHEDULED"].includes(job.status)});
   action("Create PRIVATE YouTube Broadcast","/broadcast/provision",{
     disabled:!ytFlags.youtube_mutations_enabled || !["READY","SCHEDULED"].includes(job.status),
     confirm:"Create a real PRIVATE broadcast and stream on YouTube? This does not go Live."});
   const verify=document.createElement("button");verify.textContent="Verify YouTube";
   verify.onclick=async()=>{
     try{const result=await call("/jobs/"+encodeURIComponent(job.id)+"/broadcast/verify");
       note("YouTube: "+result.youtube_broadcast_status+
         " | Ingest: "+result.youtube_ingest_status+
         " | Verified LIVE: "+(result.youtube_verified_live?"YES":"NO"));}
     catch(e){note(e.message)}
   };
   bar.appendChild(verify);
   action("Start RTMPS Ingest","/broadcast/ingest",{
     disabled:!ytFlags.youtube_mutations_enabled||!ytFlags.publishing_enabled,
     confirm:"Start FFmpeg RTMPS transmission to the bound PRIVATE YouTube broadcast?"});
   action("Test Broadcast","/broadcast/transition",{
     disabled:!ytFlags.transitions_enabled,
     body:{status:"testing"},confirm:"Transition YouTube broadcast to testing after ACTIVE ingest?"});
   action("GO LIVE","/broadcast/transition",{
     disabled:!ytFlags.go_live_enabled||!ytFlags.transitions_enabled,
     body:{status:"live"},confirm:"GO LIVE on YouTube? This can publish to the configured audience."});
   action("Complete Broadcast","/broadcast/transition",{
     disabled:!ytFlags.transitions_enabled,
     body:{status:"complete"},confirm:"Complete YouTube live broadcast permanently?"});
   el.appendChild(bar);
 });
}
async function refreshYoutubeGates(){
 ytFlags=await call("/youtube/health");
 $("live-controls-status").textContent="YouTube provisioning: "+
  (ytFlags.youtube_mutations_enabled?"Enabled":"OFF")+
  " | FFmpeg ingest: "+(ytFlags.publishing_enabled?"Enabled":"OFF")+
  " | Transitions: "+(ytFlags.transitions_enabled?"Enabled":"OFF")+
  " | Public GO LIVE: "+(ytFlags.go_live_enabled?"Enabled":"OFF");
 await refreshJobs();
}
async function refreshHealth(){
 const a=await call("/health"),b=await call("/integrations/health");
 $("health").textContent="Backend: "+(a.ok?"Connected":"Unavailable")+
 "; OAuth configured: "+(b.oauth_configured?"Yes":"No — Google Cloud client setup required")+
 "; Publishing: "+(a.publishing_enabled?"Enabled":"OFF")+
 "; Scheduler: "+(a.scheduler_enabled?"Enabled":"OFF");
 $("connect").disabled=!b.oauth_configured;
 const jobs=await call("/jobs");
 $("metrics").textContent="Connected channels: "+b.channels_connected+
 " | Jobs: "+jobs.jobs.length+" | Encoder sessions: "+a.encoder_sessions+
 " | YouTube Live verification: read-only per channel";
}
const streamTabs=[...document.querySelectorAll("[data-tab]")];
const streamIdentity=document.querySelector('meta[name="vps1-username"]')?.content || '';
const streamStateKey=streamIdentity ? 'vps1.stream.active-tab.v1:'+streamIdentity : '';
function openStreamTab(id){
 if(!streamTabs.some(b=>b.dataset.tab===id && !b.hidden))id='dashboard';
 streamTabs.forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===id)));
 document.querySelectorAll("[role=tabpanel]").forEach(s=>s.hidden=s.id!==id);
 if(streamStateKey){try{sessionStorage.setItem(streamStateKey,id);}catch(_){}}
}
streamTabs.forEach(button=>button.onclick=()=>openStreamTab(button.dataset.tab));
let streamRestoredTab='dashboard';
if(streamStateKey){try{streamRestoredTab=sessionStorage.getItem(streamStateKey)||'dashboard';}catch(_){}}
openStreamTab(streamRestoredTab);
async function refreshOAuthSettings(){
 const tab=$("oauth-settings-tab");
 try{
   const s=await call("/oauth/client");
   tab.hidden=false;
   $("oauth-client-state").textContent=s.configured?
     "Client configured ("+s.source+"): "+s.client_id+" | Client Secret stored securely.":
     "Not configured. Add a Google OAuth Web Client ID and Client Secret below.";
   $("oauth-client-id").value=s.client_id||"";
   $("oauth-client-secret").value="";
   $("remove-oauth-client").disabled=s.source!=="studio";
 }catch(e){
   tab.hidden=true;
   if($("oauth-settings").hidden===false){
     $("oauth-settings").hidden=true;
     $("dashboard").hidden=false;
   }
 }
}
$("copy-oauth-redirect").onclick=async()=>{
 try{await navigator.clipboard.writeText($("oauth-redirect").textContent);note("Redirect URI copied.");}
 catch(e){note("Select the redirect URI above and copy it.")}
};
$("oauth-client-form").onsubmit=async e=>{
 e.preventDefault();
 const cid=$("oauth-client-id").value.trim(),secret=$("oauth-client-secret").value;
 try{
   if(!cid||!secret)throw Error("Client ID and Client Secret are required.");
   await call("/oauth/client","POST",{client_id:cid,client_secret:secret});
   $("oauth-client-secret").value="";
   await refreshOAuthSettings();
   await refreshHealth();
   note("Google OAuth client saved securely. Next: Connect your YouTube channel using Google consent.");
 }catch(e){
   $("oauth-client-secret").value="";
   note(e.message);
 }
};
$("remove-oauth-client").onclick=async()=>{
 if(!confirm("Remove Auto Stream's saved OAuth client? Connected channels must be disconnected first."))return;
 try{
   await call("/oauth/client","DELETE");
   $("oauth-client-id").value="";$("oauth-client-secret").value="";
   await refreshOAuthSettings();await refreshHealth();
   note("Saved Auto Stream Google OAuth client removed.");
 }catch(e){note(e.message)}
};
$("connect").onclick=async()=>{try{const x=await call("/oauth/start");window.location.assign(x.authorization_url);}catch(e){note(e.message)}};
$("reload-channels").onclick=()=>refreshChannels().catch(e=>note(e.message));
$("reload-jobs").onclick=()=>refreshYoutubeGates().catch(e=>note(e.message));
$("create-job").onclick=async()=>{
 try{
  const channel=$("job-channel").value;
  if(!channel)throw Error("Connect an authorized YouTube channel first.");
  const dt=$("job-scheduled").value;
  if(!dt)throw Error("Choose a future scheduled date and time.");
  const start=new Date(dt);
  if(!Number.isFinite(start.getTime())||start.getTime()<=Date.now()+120000)
    throw Error("Schedule at least two minutes in the future.");
  const data={channel_id:channel,media_path:$("job-media").value.trim(),
      title:$("job-title").value.trim(),
      scheduled_at:start.toISOString(),
      idempotency_key:"studio-"+(globalThis.crypto&&globalThis.crypto.randomUUID?
        globalThis.crypto.randomUUID().replaceAll("-",""):String(Date.now()))};
  const result=await call("/jobs","POST",data);
  note("DRAFT created: "+result.id+". No YouTube action or stream started.");
  await refreshJobs();
 }catch(e){note(e.message)}
};
$("import-channel").onclick=async()=>{
 try{await call("/channels/import","POST",{channel_id:$("cid").value,title:$("ctitle").value});
 await refreshChannels();note("Channel metadata imported. OAuth is still required for Live API access.");}
 catch(e){note(e.message)}
};
$("add-sheet").onclick=async()=>{
 try{await call("/sheets","POST",{channel_id:$("sheet-channel").value,
 spreadsheet_id:$("sheet-id").value,a1_range:$("sheet-range").value});
 await refreshSheets();note("Read-only Sheet linked; no sync jobs started.");}catch(e){note(e.message)}
};
$("refresh-live").onclick=async()=>{
 try{const c=$("live-channel").value;if(!c)throw Error("Connect a channel first");
 const result=await call("/channels/"+encodeURIComponent(c)+"/live");
 renderItems("live-list",result.broadcasts,(el,b)=>{
 txt(el,(b.title||"Untitled")+" — "+b.id);
 txt(el,"YouTube lifecycle: "+(b.life_cycle||"unknown")+
 " | Verified Live: "+(b.youtube_verified_live?"YES":"NO"),"muted");
 });note("YouTube status read completed; no publishing action performed.");}
 catch(e){note(e.message)}
};
Promise.all([refreshHealth(),refreshChannels(),refreshSheets(),refreshYoutubeGates(),refreshOAuthSettings()]).catch(e=>note(e.message));
})();
</script></body></html>
"""


@router.get("/studio", response_class=HTMLResponse)
def studio():
    return HTMLResponse(PAGE)

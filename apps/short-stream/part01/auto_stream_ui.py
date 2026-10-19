"""Independent Auto Stream V2 control screen. Existing Short Stream UI intact."""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/engine", tags=["Auto Stream Studio"])

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auto Stream — Channels & Sheets Sync</title>
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
<div class="panel"><strong>Engine V2</strong><p id="health">Loading engine status...</p><small>Google access is separate from Auto Uploader. No upload jobs or streams start from this screen.</small></div>
<nav aria-label="Auto Stream workspace tabs">
<button data-tab="dashboard" aria-selected="true">Dashboard</button>
<button data-tab="channels" aria-selected="false">Channels</button>
<button data-tab="sheets" aria-selected="false">Sheets Sync</button>
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
<section id="sheets" class="panel" role="tabpanel" hidden>
<h2>Google Sheets Sync</h2><p class="muted">Read-only Sheets; synced rows create DRAFT jobs only. Dry-run preview is default.</p>
<div class="grid"><div><label for="sheet-channel">Authorized channel</label><select id="sheet-channel"></select></div>
<div><label for="sheet-id">Spreadsheet ID</label><input id="sheet-id" placeholder="Google Sheet ID"></div>
<div><label for="sheet-range">A1 range</label><input id="sheet-range" value="Sheet1!A1:Z200"></div></div>
<p><button id="add-sheet" class="primary">Link read-only Sheet</button></p>
<div id="sheet-list"></div>
<p class="muted">Required row columns: channel_id, media_path, title, scheduled_at, enabled. Optional: continuous. Times must include timezone offset.</p>
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
const root=window.location.pathname.replace(/\\/engine\\/studio\\/?$/,"")+"/engine";
const $=id=>document.getElementById(id);
function note(s){const m=$("msg");m.className="message";m.textContent=s;}
async function call(path,method,body){
 const res=await fetch(root+path,{method:method||"GET",credentials:"same-origin",
  headers:body?{"Content-Type":"application/json"}:{},
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
 for(const name of ["sheet-channel","live-channel"]){
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
document.querySelectorAll("[data-tab]").forEach(button=>button.onclick=()=>{
 document.querySelectorAll("[data-tab]").forEach(b=>b.setAttribute("aria-selected",String(b===button)));
 document.querySelectorAll("[role=tabpanel]").forEach(s=>s.hidden=s.id!==button.dataset.tab);
});
$("connect").onclick=async()=>{try{const x=await call("/oauth/start");window.location.assign(x.authorization_url);}catch(e){note(e.message)}};
$("reload-channels").onclick=()=>refreshChannels().catch(e=>note(e.message));
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
Promise.all([refreshHealth(),refreshChannels(),refreshSheets()]).catch(e=>note(e.message));
})();
</script></body></html>
"""


@router.get("/studio", response_class=HTMLResponse)
def studio():
    return HTMLResponse(PAGE)

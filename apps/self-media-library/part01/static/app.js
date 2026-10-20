const state = { page:'dashboard', clips:[], total:0, offset:0, limit:48, selected:new Set(), current:null, sources:[], trash:[] };
const $ = s => document.querySelector(s); const $$ = s => [...document.querySelectorAll(s)];
// Nginx may rewrite quoted legacy /api/ paths; normalize both forms into
// exactly one app-scoped path so Scene Builder's /api never intercepts SML calls.
const libraryPath = path => {
  if (!path || !path.startsWith('/')) throw new Error('Invalid local library path');
  const rest = path.replace(/^\/(?:self-media-library\/)?/, '');
  return '/self-media-library/' + rest;
};
const mediaUrl = path => path ? libraryPath(path) : '';
const api = async (url, opts={}) => {
  const r=await fetch(libraryPath(url), {...opts,credentials:'same-origin'});
  const data=await r.json().catch(()=>({}));
  if(!r.ok){
    const error=new Error(typeof data.detail==='string'?data.detail:
      r.status===401||r.status===403?'Sign in to the website to access your library':
      'Library request failed (HTTP '+r.status+')');
    error.status=r.status;
    throw error;
  }
  return data;
};
const esc = s => String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const fmtTime = s => { s=Number(s||0); const m=Math.floor(s/60), sec=(s%60).toFixed(1).padStart(4,'0'); return `${m}:${sec}`; };
const fmtSize = n => n>1073741824?`${(n/1073741824).toFixed(1)} GB`:n>1048576?`${(n/1048576).toFixed(1)} MB`:`${Math.round(n/1024)} KB`;
const toast = msg => { const el=$('#toast'); el.textContent=msg; el.classList.remove('hidden'); clearTimeout(window._toast); window._toast=setTimeout(()=>el.classList.add('hidden'),2200); };

const pageMeta={dashboard:['Dashboard','Build and control your reusable clip library.'],sources:['Input','Add videos, images, Drive files and authorized YouTube media.'],processing:['Process','Inspect import queues, scene detection and metadata generation.'],library:['Library & Preview','Search, review and control approved videos and images.'],trash:['Trash','Restore clips or permanently delete them.'],settings:['API & Settings','Editor API and safe library storage settings.']};
function gotoPage(page){ state.page=page; $$('.page').forEach(x=>x.classList.remove('active')); $(`#page-${page}`).classList.add('active'); $$('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===page)); $('#pageTitle').textContent=pageMeta[page][0]; $('#pageSubtitle').textContent=pageMeta[page][1]; if(page==='library'){loadClips(true);loadImages();} if(page==='sources'||page==='processing') loadSources(); if(page==='sources')loadImports(); if(page==='trash') loadTrash(); if(page==='settings'){loadSettings();loadEditorStatus();} if(page==='dashboard') loadDashboard(); }
$$('.nav-item').forEach(b=>b.onclick=()=>gotoPage(b.dataset.page)); $$('[data-goto]').forEach(b=>b.onclick=()=>gotoPage(b.dataset.goto));

async function checkHealth(){ try{ const h=await api('/api/health'); $('#healthDot').classList.toggle('ok',h.ok&&h.ffmpeg); let msg=h.ffmpeg?'Engine online':'Engine online · FFmpeg missing'; if(h.drive_enabled) msg+=h.rclone?' · Drive ready':' · rclone missing'; $('#healthText').textContent=msg; }catch(e){ $('#healthDot').classList.remove('ok'); $('#healthText').textContent=e.status===401||e.status===403?'Website login required':'Engine unavailable · '+e.message; } }
async function loadDashboard(){ const [s,src,cl]=await Promise.all([api('/api/stats'),api('/api/sources'),api('/api/clips?limit=8')]); $('#stats').innerHTML=[['Library clips',s.clips,'Reusable assets'],['Drive verified',s.drive_verified||0,`${s.source_cleaned||0} sources auto-cleaned`],['Approved',s.approved,'Ready library clips'],['Source videos',s.sources,`${s.processing} processing`]].map(x=>`<div class="stat-card"><span>${x[0]}</span><strong>${x[1]}</strong><small>${x[2]}</small></div>`).join(''); renderSources(src.items.slice(0,5),'#recentSources'); renderClips(cl.items,'#recentClips',false); const denom=Math.max(1,s.clips); $('#healthSummary').innerHTML=`<div class="health-line"><span>Drive verified</span><b>${Math.round((s.drive_verified||0)/denom*100)}%</b></div><div class="health-bar"><i style="width:${(s.drive_verified||0)/denom*100}%"></i></div><div class="health-line"><span>Approved library</span><b>${s.approved}</b></div><div class="health-line"><span>Candidate review</span><b>${s.candidate}</b></div><div class="health-line"><span>Sources auto-cleaned</span><b>${s.source_cleaned||0}</b></div><div class="health-line"><span>Active processing</span><b>${s.processing}</b></div>`; }

function sourceRow(s, processing=false){ const status=esc(s.status); const cleaned=!!s.source_deleted_at; const hasClips=Number(s.clip_count||0)>0; const active=s.status==='processing'; const canProcess=!active&&!cleaned&&!hasClips; const failed=s.status==='failed'||s.drive_state==='sync_failed'||String(s.stage||'').includes('failed'); const drive=(s.drive_state||'pending').replaceAll('_',' '); const total=Number(s.drive_clip_total||0), verified=Number(s.drive_clip_verified||0), failedClips=Number(s.drive_clip_failed||0); const clipDrive=total?` · clips ${verified}/${total} verified${failedClips?` · ${failedClips} failed`:''}`:''; const cleanupLabel=cleaned?(s.source_cleanup_mode==='manual_failed'?' · source manually cleaned':' · source auto-cleaned'):''; const tech=s.width&&s.height?` · ${s.width}×${s.height} ${esc(s.orientation||'unknown')} · ${esc(s.codec||'codec?')} ${Number(s.fps||0).toFixed(1)}fps`:''; const driveNote=`Drive: ${drive}${clipDrive}${cleanupLabel}${tech}`; const retryDrive=s.drive_state==='sync_failed'&&hasClips&&!active?`<button class="ghost" data-drive-retry="${s.id}">Retry failed Drive</button>`:''; const manualCleanup=failed&&!active&&!cleaned?`<button class="danger-soft" data-manual-cleanup="${s.id}">Manual cleanup</button>`:''; const actions=`<div class="row-actions">${retryDrive}${manualCleanup}${!processing?`<button class="ghost" data-process="${s.id}" ${canProcess?'':'disabled'}>${cleaned?'Source cleaned':hasClips?'Already processed':'Process'}</button>`:''}</div>`; return `<div class="${processing?'processing-row':'source-row'}"><div class="source-main"><b title="${esc(s.filename)}">${esc(s.filename)}</b><span>${s.duration?fmtTime(s.duration):'Duration pending'} · ${fmtSize(s.size_bytes)} · ${esc(driveNote)}</span></div>${processing?`<div class="processing-stage">${esc((s.stage||'queued').replaceAll('_',' '))}</div>`:`<span class="state ${status}">${status}</span>`}<div class="progress"><i style="width:${Number(s.progress||0)}%"></i></div>${processing?(failed&&!cleaned?actions:`<b class="source-meta">${Number(s.progress||0)}%</b>`):actions}</div>`; }
function bindSourceActions(el){ el.querySelectorAll('[data-process]').forEach(b=>b.onclick=async()=>{ try{ await api(`/api/sources/${b.dataset.process}/process`,{method:'POST'}); toast('Processing started'); gotoPage('processing'); }catch(e){toast(e.message)} }); el.querySelectorAll('[data-drive-retry]').forEach(b=>b.onclick=async()=>{ b.disabled=true; try{ const d=await api(`/api/sources/${b.dataset.driveRetry}/drive-sync`,{method:'POST'}); const note=`${d.verified}/${d.total} verified · ${d.retried||0} retried · ${d.skipped_verified||0} already safe`; toast(d.source_cleaned?`Drive safe; source cleaned · ${note}`:`Drive sync complete · ${note}`); await loadSources(); }catch(e){toast(e.message);await loadSources()}finally{b.disabled=false} }); el.querySelectorAll('[data-manual-cleanup]').forEach(b=>b.onclick=async()=>{ const src=state.sources.find(x=>x.id===b.dataset.manualCleanup); const name=src?.filename||'this source'; if(!confirm(`Manually remove the local original source?\n\n${name}\n\nGenerated clips, metadata, and any Drive files will NOT be deleted. This source video cannot be reprocessed after cleanup.`))return; b.disabled=true; try{ const d=await api(`/api/sources/${b.dataset.manualCleanup}/manual-cleanup`,{method:'POST'}); toast(`Local source cleaned · ${fmtSize(d.bytes_removed||0)} freed`); await loadSources(); }catch(e){toast(e.message)}finally{b.disabled=false} }); }
function renderSources(items, target='#sourcesList'){ const el=$(target); if(!items.length){el.innerHTML='<div class="empty"><b>No source videos yet</b>Add your first video to start building the library.</div>';return;} el.innerHTML=items.map(x=>sourceRow(x,false)).join(''); bindSourceActions(el); }
async function loadSources(){ const d=await api('/api/sources'); state.sources=d.items; renderSources(d.items); const p=$('#processingList'); p.innerHTML=d.items.length?d.items.map(x=>sourceRow(x,true)).join(''):'<div class="empty"><b>Queue is empty</b>Add and process a source video.</div>'; bindSourceActions(p); }

function clipCard(c, selectable=true, trash=false){ const kws=(c.keywords||[]).slice(0,3).map(k=>`<span>${esc(k.keyword)}</span>`).join('')||'<span>untagged</span>'; const extra=(c.keywords||[]).length>3?`<span>+${c.keywords.length-3}</span>`:''; const img=c.thumb_url?`<img loading="lazy" src="${esc(mediaUrl(c.thumb_url))}" alt="">`:'<div class="placeholder">▶</div>'; const drive=c.drive_state==='verified'?'☁ verified':c.drive_state==='sync_failed'?'☁ failed':'☁ pending'; const orient=esc(c.source_orientation||'unknown'); return `<article class="clip-card ${state.selected.has(c.id)?'selected':''}" data-id="${c.id}">${selectable?'<div class="clip-check">✓</div>':''}<div class="clip-thumb">${img}<span class="duration-pill">${Number(c.duration).toFixed(1)}s</span></div><div class="clip-body"><div class="clip-topline"><span class="clip-id">${esc(c.id)}</span><span class="clip-quality">Q${c.quality||0}</span></div><div class="keyword-preview">${kws}${extra}</div><div class="card-footer">${trash?'Deleted · ':''}${esc(c.source_filename||'')} · ${orient} · ${drive}</div></div></article>`; }
function renderClips(items,target,selectable=true,trash=false){ const el=$(target); if(!items.length){ el.innerHTML='<div class="empty" style="grid-column:1/-1"><b>No clips here</b>Your library items will appear here.</div>'; return; } el.innerHTML=items.map(c=>clipCard(c,selectable,trash)).join(''); el.querySelectorAll('.clip-card').forEach(card=>{ card.querySelector('.clip-check')?.addEventListener('click',e=>{e.stopPropagation();toggleSelect(card.dataset.id)}); card.addEventListener('click',()=> trash?openTrashAction(card.dataset.id):openDrawer(card.dataset.id)); }); }

async function loadClips(reset=false){ if(reset){state.offset=0;state.clips=[];state.selected.clear();updateBulk();} const q=encodeURIComponent($('#clipSearch').value.trim()), st=$('#statusFilter').value, sort=$('#sortFilter').value, orient=$('#clipOrientation').value; const d=await api(`/api/clips?q=${q}&status=${st}&sort=${sort}&orientation=${orient}&limit=${state.limit}&offset=${state.offset}`); state.total=d.total; state.clips=reset?d.items:[...state.clips,...d.items]; state.offset=state.clips.length; renderClips(state.clips,'#libraryGrid',true,false); $('#resultCount').textContent=`${d.total} clip${d.total===1?'':'s'}`; $('#loadMore').classList.toggle('hidden',state.clips.length>=d.total); }
let searchTimer; $('#clipSearch').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadClips(true),300)}; $('#statusFilter').onchange=()=>loadClips(true); $('#clipOrientation').onchange=()=>loadClips(true); $('#sortFilter').onchange=()=>loadClips(true); $('#loadMore').onclick=()=>loadClips(false);
function toggleSelect(id){ state.selected.has(id)?state.selected.delete(id):state.selected.add(id); renderClips(state.clips,'#libraryGrid',true,false); updateBulk(); }
function updateBulk(){ $('#bulkBar').classList.toggle('hidden',state.selected.size===0); $('#selectedCount').textContent=state.selected.size; }
$('#clearSelection').onclick=()=>{state.selected.clear();renderClips(state.clips,'#libraryGrid');updateBulk()}; $('#selectVisible').onclick=()=>{state.clips.forEach(c=>state.selected.add(c.id));renderClips(state.clips,'#libraryGrid');updateBulk()};
$$('[data-bulk-status]').forEach(b=>b.onclick=async()=>{ if(!state.selected.size)return; await api('/api/clips/bulk/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[...state.selected],status:b.dataset.bulkStatus})}); toast(`Updated ${state.selected.size} clips`); loadClips(true); });
$('#bulkDelete').onclick=async()=>{ if(!state.selected.size)return; if(!confirm(`Move ${state.selected.size} selected clips to Trash?`))return; await Promise.all([...state.selected].map(id=>api(`/api/clips/${id}`,{method:'DELETE'}))); toast('Selected clips moved to Trash'); loadClips(true); };
$('#bulkKeyword').onclick=()=>$('#keywordModal').classList.remove('hidden'); $('#cancelKeywordModal').onclick=()=>$('#keywordModal').classList.add('hidden');
async function bulkKeyword(action){ const keyword=$('#bulkKeywordInput').value.trim(); if(!keyword)return; await api('/api/clips/bulk/keywords',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[...state.selected],keyword,action})}); $('#keywordModal').classList.add('hidden'); $('#bulkKeywordInput').value=''; toast(`Keyword ${action==='add'?'added':'removed'}`); loadClips(true); }
$('#addBulkKeyword').onclick=()=>bulkKeyword('add'); $('#removeBulkKeyword').onclick=()=>bulkKeyword('remove');

function openDrawer(id){ const c=state.clips.find(x=>x.id===id) || state.current; if(!c)return; state.current=JSON.parse(JSON.stringify(c)); $('#drawerTitle').textContent=c.id; $('#drawerVideo').src=mediaUrl(c.clip_url); $('#drawerDescription').textContent=c.description||'No AI description yet.'; $('#drawerSource').textContent=c.source_filename||'Unknown'; $('#drawerTime').textContent=`${fmtTime(c.start_time)} → ${fmtTime(c.end_time)}`; $('#drawerQuality').textContent=`${c.quality||0}/100`; $('#drawerDrive').textContent=(c.drive_state||'pending').replaceAll('_',' '); $('#drawerStatus').value=c.status; renderDrawerKeywords(); $('#drawerBackdrop').classList.remove('hidden'); $('#clipDrawer').classList.remove('hidden'); }
function renderDrawerKeywords(){ const el=$('#keywordChips'); el.innerHTML=(state.current.keywords||[]).map((k,i)=>`<span>${esc(k.keyword)} <button data-rm="${i}">×</button></span>`).join('')||'<span>no keywords</span>'; el.querySelectorAll('[data-rm]').forEach(b=>b.onclick=()=>{state.current.keywords.splice(Number(b.dataset.rm),1);renderDrawerKeywords()}); }
function closeDrawer(){ $('#drawerBackdrop').classList.add('hidden'); $('#clipDrawer').classList.add('hidden'); $('#drawerVideo').pause(); $('#drawerVideo').removeAttribute('src'); }
$('#closeDrawer').onclick=closeDrawer; $('#drawerBackdrop').onclick=closeDrawer; $('#addKeyword').onclick=()=>{const v=$('#newKeyword').value.trim().toLowerCase();if(!v)return;if(!state.current.keywords.some(k=>k.keyword===v))state.current.keywords.push({keyword:v,source:'manual',confidence:1});$('#newKeyword').value='';renderDrawerKeywords()}; $('#newKeyword').onkeydown=e=>{if(e.key==='Enter')$('#addKeyword').click()};
$('#saveClip').onclick=async()=>{ const id=state.current.id, kws=state.current.keywords.map(k=>k.keyword); await api(`/api/clips/${id}/keywords`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({keywords:kws})}); await api('/api/clips/bulk/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[id],status:$('#drawerStatus').value})}); toast('Clip changes saved'); closeDrawer(); loadClips(true); };
$('#deleteClip').onclick=async()=>{ if(!confirm('Move this clip to Trash?'))return; await api(`/api/clips/${state.current.id}`,{method:'DELETE'}); toast('Clip moved to Trash'); closeDrawer(); loadClips(true); };
$('#reanalyzeClip').onclick=async()=>{ $('#reanalyzeClip').disabled=true; try{await api(`/api/clips/${state.current.id}/reanalyze`,{method:'POST'});toast('Visual analysis queued; follow progress in Process');closeDrawer();loadClips(true);}catch(e){toast(e.message)}finally{$('#reanalyzeClip').disabled=false} };
$('#syncClipDrive').onclick=async()=>{ if(!state.current)return; $('#syncClipDrive').disabled=true; try{await api(`/api/clips/${state.current.id}/drive-sync`,{method:'POST'});toast('Drive copy verified');closeDrawer();loadClips(true);}catch(e){toast(e.message)}finally{$('#syncClipDrive').disabled=false} };

async function loadTrash(){ const d=await api('/api/clips?trash=true&limit=200'); state.trash=d.items; renderClips(d.items,'#trashGrid',false,true); }
async function openTrashAction(id){ const c=state.trash.find(x=>x.id===id); if(!c)return; const restore=confirm(`${c.id}\n\nOK = Restore clip\nCancel = keep in Trash`); if(restore){await api(`/api/clips/${id}/restore`,{method:'POST'});toast('Clip restored');loadTrash();} }

async function loadSettings(){ const d=await api('/api/settings'),v=d.values,r=d.drive_runtime||{}; $('#clipMin').value=v.clip_min_seconds||2; $('#clipMax').value=v.clip_max_seconds||4; $('#sceneThreshold').value=v.scene_threshold||.32; $('#aiAuto').checked=v.ai_auto==='true'; $('#dupToggle').checked=v.duplicate_detection==='true'; $('#qualityToggle').checked=v.quality_filter==='true'; $('#driveEnabled').checked=v.drive_enabled==='true'; $('#driveRemote').value=v.drive_remote||'gdrive'; $('#driveRoot').value=v.drive_root||'Self Media Library'; $('#driveVerify').checked=v.drive_verify!=='false'; $('#driveSyncMetadata').checked=v.drive_sync_metadata!=='false'; $('#sourceCleanup').checked=v.source_cleanup==='after_drive_verified'; $('#aiPill').textContent=v.ai_configured==='true'?'Vision AI connected':'AI adapter ready · provider not connected'; $('#aiPill').classList.toggle('ready',v.ai_configured==='true'); const ready=!!r.rclone_available&&!!r.config_exists&&!!r.remote_configured; $('#drivePill').textContent=ready?(v.drive_enabled==='true'?'Runtime Drive ready':'Runtime Drive configured · disabled'):(r.rclone_available?'Runtime Drive needs setup':'rclone not installed'); $('#drivePill').classList.toggle('ready',ready); $('#driveRuntimeState').textContent=!r.rclone_available?'rclone missing':!r.config_exists?'Runtime config file not created':!r.remote_configured?`Remote '${r.remote||'gdrive'}' is not configured`:`Remote '${r.remote}' configured`; $('#driveRuntimeState').className=ready?'drive-good':'drive-warn'; $('#driveConfigPath').textContent=r.config_path||v.rclone_config_path||'Runtime config path unavailable'; const last=r.last_test_status||v.drive_last_test_status||'never'; $('#driveLastTest').textContent=last==='never'?'Drive read/write test has not been run yet':`Last Drive test: ${last.toUpperCase()}${r.last_test_at?' · '+r.last_test_at:''}${r.last_test_detail?' · '+r.last_test_detail:''}`; }
$('#saveSettings').onclick=async()=>{ const values={clip_min_seconds:$('#clipMin').value,clip_max_seconds:$('#clipMax').value,scene_threshold:$('#sceneThreshold').value,ai_auto:String($('#aiAuto').checked),duplicate_detection:String($('#dupToggle').checked),quality_filter:String($('#qualityToggle').checked),drive_enabled:String($('#driveEnabled').checked),drive_remote:$('#driveRemote').value.trim()||'gdrive',drive_root:$('#driveRoot').value.trim()||'Self Media Library',drive_verify:String($('#driveVerify').checked),drive_sync_metadata:String($('#driveSyncMetadata').checked),source_cleanup:$('#sourceCleanup').checked?'after_drive_verified':'keep'}; await api('/api/settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})}); toast('Settings saved'); checkHealth(); };
$('#testDrive').onclick=async()=>{ $('#testDrive').disabled=true; $('#testDrive').textContent='Testing…'; try{ const d=await api('/api/drive/test',{method:'POST'}); $('#drivePill').textContent=`Verified · ${d.remote}:${d.root}`; $('#drivePill').classList.add('ready'); toast('Drive read/write verification passed'); }catch(e){ $('#drivePill').textContent='Drive test failed'; $('#drivePill').classList.remove('ready'); toast(e.message); }finally{$('#testDrive').disabled=false;$('#testDrive').textContent='Test Drive';loadSettings()} };

const sourceFile=$('#sourceFile'), drop=$('#dropPanel');
$('#chooseSource').onclick=()=>sourceFile.click();
$('#topAddSource').onclick=()=>{gotoPage('sources');sourceFile.click()};
sourceFile.onchange=()=>uploadFiles(Array.from(sourceFile.files||[]));
['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('drag')}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('drag')}));
drop.addEventListener('drop',e=>{uploadFiles(Array.from(e.dataTransfer?.files||[]))});
async function uploadFiles(files){
  if(!files.length)return;
  const allowed=/\.(mp4|mov|mkv|webm|m4v|avi)$/i;
  const invalid=files.filter(f=>!allowed.test(f.name));
  files=files.filter(f=>allowed.test(f.name));
  if(!files.length){toast('Choose supported video files');return}
  const button=$('#chooseSource'), status=$('#sourceUploadResult');
  button.disabled=true;
  let ok=0,duplicate=0,failed=invalid.length,firstSource=null;
  try{
    for(let i=0;i<files.length;i++){
      const file=files[i],fd=new FormData();
      fd.append('file',file);
      button.textContent='Uploading '+(i+1)+'/'+files.length;
      status.textContent='Uploading '+(i+1)+'/'+files.length+': '+file.name;
      try{
        const saved=await api('/api/sources/upload',{method:'POST',body:fd});
        if(saved.duplicate)duplicate++; else ok++;
        if(!firstSource && !saved.duplicate)firstSource=saved.id;
      }catch(e){failed++;status.textContent='Failed '+file.name+': '+e.message}
    }
    await loadSources();
    status.textContent=ok+' new · '+duplicate+' already in library · '+failed+' failed';
    toast(status.textContent);
    if(files.length===1&&firstSource&&confirm('Start processing this uploaded video now?')){
      await api('/api/sources/'+encodeURIComponent(firstSource)+'/process',{method:'POST'});
      gotoPage('processing');
    }else if(ok && files.length>1){status.textContent+=' · Select Process for each queued source.'}
  }finally{button.disabled=false;button.textContent='Choose videos';sourceFile.value=''}
}
$('#refreshSources').onclick=loadSources; $('#refreshProcessing').onclick=loadSources;
setInterval(()=>{if(state.page==='processing')loadSources();},3000);

checkHealth(); loadDashboard();


// Self Media Library V2: additive image intake, in-page review and durable import queue.
let imageRows=[], selectedImage=null;
async function loadImages() {
  const q=encodeURIComponent($('#imageSearch').value.trim());
  const status=encodeURIComponent($('#imageStatus').value),orientation=encodeURIComponent($('#imageOrientation').value);
  try {
    const data=await api('/api/v1/images?q='+q+'&status='+status+'&orientation='+orientation+'&limit=200');
    imageRows=data.items;
    $('#imagesGrid').innerHTML=imageRows.length?imageRows.map(i=>'<button class="image-card" type="button" data-image="'+esc(i.id)+'"><img loading="lazy" src="'+esc(mediaUrl(i.thumb_url))+'" alt="'+esc(i.original_name)+'"><b>'+esc(i.original_name)+'</b><small>'+esc(i.status)+' · '+esc(i.drive_state)+' · '+esc(i.width)+'×'+esc(i.height)+' · '+esc(i.orientation)+' · '+esc(i.source_kind||'legacy')+'</small></button>').join(''):'<p class="empty">No images yet. Upload one in Input.</p>';
    $$('#imagesGrid [data-image]').forEach(el=>el.onclick=()=>openImageEditor(el.dataset.image));
  } catch(e) { $('#imagesGrid').textContent=e.message; }
}
$('#imageSearch').oninput=()=>{clearTimeout(window.imageDelay);window.imageDelay=setTimeout(loadImages,300)};
$('#imageStatus').onchange=loadImages; $('#imageOrientation').onchange=loadImages;
function openImageEditor(id){
  selectedImage=imageRows.find(i=>i.id===id);
  if(!selectedImage)return;
  $('#imageEditorPreview').src=mediaUrl(selectedImage.content_url);
  $('#imageEditorHeading').textContent=selectedImage.original_name;
  $('#imageTechnical').textContent=[selectedImage.width+'×'+selectedImage.height,selectedImage.orientation,selectedImage.source_kind||'legacy',selectedImage.sha256?'SHA-256 '+selectedImage.sha256.slice(0,16)+'…':''].filter(Boolean).join(' · ');
  $('#imageDisplayName').value=selectedImage.original_name;
  $('#imageKeywords').value=selectedImage.keywords.join(', ');
  $('#imageDescription').value=selectedImage.description||'';
  $('#imageReviewStatus').value=selectedImage.status;
  $('#imageEditor').classList.remove('hidden');
}
$('#imageClose').onclick=()=>{$('#imageEditor').classList.add('hidden');selectedImage=null};
$('#imageSave').onclick=async()=>{
  if(!selectedImage)return;
  try {
    await api('/api/v1/images/'+encodeURIComponent(selectedImage.id),{
      method:'PATCH',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({display_name:$('#imageDisplayName').value,keywords:$('#imageKeywords').value.split(',').map(x=>x.trim()).filter(Boolean),description:$('#imageDescription').value,status:$('#imageReviewStatus').value})
    });
    toast('Image updated');$('#imageClose').click();await loadImages();
  }catch(e){toast(e.message)}
};
$('#imageDriveRetry').onclick=async()=>{
  if(!selectedImage)return;
  try{const result=await api('/api/v1/images/'+encodeURIComponent(selectedImage.id)+'/drive-sync',{method:'POST'});toast(result.ok?'Image verified on Drive':result.detail||result.state);await loadImages()}
  catch(e){toast(e.message)}
};
$('#imageTrash').onclick=async()=>{
  if(!selectedImage||!confirm('Move image to trash?'))return;
  try{await api('/api/v1/images/'+encodeURIComponent(selectedImage.id),{method:'DELETE'});toast('Image moved to trash');$('#imageClose').click();await loadImages()}
  catch(e){toast(e.message)}
};
$('#uploadImages').onclick=async()=>{
  const files=Array.from($('#imageFiles').files||[]);
  if(!files.length){toast('Choose one or more image files');return}
  $('#uploadImages').disabled=true;
  let ok=0,fail=0;
  for(const file of files){
    const fd=new FormData();fd.append('file',file);
    try{await api('/api/v1/images/upload',{method:'POST',body:fd});ok++}
    catch(e){fail++;$('#imageUploadResult').textContent='Upload error: '+e.message}
  }
  $('#uploadImages').disabled=false;$('#imageFiles').value='';
  toast(ok+' imported'+(fail?', '+fail+' failed':''));
  if(ok)gotoPage('library');
};
async function loadImports(){
  try{
    const result=await api('/api/v1/media/imports');
    $('#importHistory').innerHTML=result.items.length?result.items.map(item=>'<div class="source-row"><div class="source-main"><b>'+esc(item.source_type)+' · '+esc(item.name)+'</b><span>'+esc(item.message||item.status)+'</span></div><span class="state">'+esc(item.status)+'</span></div>').join(''):'<p class="empty">No queued imports</p>';
  }catch(e){$('#importHistory').textContent=e.message}
}
$('#submitImport').onclick=async()=>{
  const source=$('#importSource').value,value=$('#importValue').value.trim();
  if(!value){toast('Enter a source path or video URL');return}
  const button=$('#submitImport');button.disabled=true;
  try{
    const result=await api('/api/v1/media/imports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      source,value,rights_confirmed:$('#importRights').checked,use_proxy:$('#importProxy').checked
    })});
    toast('Import queued: '+result.job_id);$('#importValue').value='';loadImports();
  }catch(e){toast(e.message)}finally{button.disabled=false}
};
async function loadEditorStatus(){
  try{
    const s=await api('/api/v1/media/status');
    $('#editorApiPill').textContent=s.editor_token_configured?'Editor API enabled':'Editor API key required';
    $('#editorApiPill').classList.toggle('ready',s.editor_token_configured);
    $('#editorApiInfo').textContent=s.images+' stored images · '+s.imports_processing+' imports running';
  }catch(e){$('#editorApiInfo').textContent=e.message}
}
setInterval(()=>{if(state.page==='sources')loadImports()},6000);

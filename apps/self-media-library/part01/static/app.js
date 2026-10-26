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

const pageMeta={dashboard:['Dashboard','Build and control your reusable clip library.'],sources:['Input','Add videos, images and authorized YouTube media.'],processing:['Process','Inspect import queues, scene detection and metadata generation.'],library:['Library & Preview','Search, review and control approved videos and images.'],trash:['Trash','Restore clips or permanently delete them.'],settings:['API & Settings','Editor API and safe library storage settings.']};
// Library & Preview: content-only subtabs; existing API actions and selections stay mounted.
const SML_LIBRARY_TABS = ['media', 'ai', 'people', 'face', 'duplicates', 'trash'];
const SML_MEDIA_TABS = ['videos', 'images'];
let smlLibraryActiveTab = 'media';
let smlMediaActiveTab = 'videos';
function setLibraryTab(tab, focus=false){
  if(!SML_LIBRARY_TABS.includes(tab)) return;
  smlLibraryActiveTab = tab;
  $$('[data-library-tab]').forEach(button=>{
    const active=button.dataset.libraryTab===tab;
    button.classList.toggle('active',active);
    button.setAttribute('aria-selected',String(active));
    button.tabIndex=active?0:-1;
    if(active&&focus)button.focus();
  });
  $$('[data-library-pane]').forEach(pane=>{pane.hidden=pane.dataset.libraryPane!==tab;});
  // The original /trash navigation now points to this same six-tab workspace.
  if(tab==='trash') loadTrash().catch(e=>toast('Trash could not load: '+e.message));
}
function setLibraryMediaTab(tab,focus=false){
  if(!SML_MEDIA_TABS.includes(tab))return;
  smlMediaActiveTab=tab;
  $$('[data-library-media]').forEach(button=>{
    const active=button.dataset.libraryMedia===tab;
    button.classList.toggle('active',active);
    button.setAttribute('aria-selected',String(active));
    button.tabIndex=active?0:-1;
    if(active&&focus)button.focus();
  });
  $$('[data-media-pane]').forEach(pane=>{pane.hidden=pane.dataset.mediaPane!==tab;});
}
function bindTabArrows(selector,attribute,values,activate){
  $$(selector).forEach(button=>button.addEventListener('keydown',event=>{
    const key=event.key;
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(key))return;
    event.preventDefault();
    const position=values.indexOf(button.dataset[attribute]);
    const next=key==='Home'?0:key==='End'?values.length-1:
      (position+(key==='ArrowRight'?1:-1)+values.length)%values.length;
    activate(values[next],true);
  }));
}
$$('[data-library-tab]').forEach(button=>button.addEventListener('click',()=>setLibraryTab(button.dataset.libraryTab)));
$$('[data-library-media]').forEach(button=>button.addEventListener('click',()=>setLibraryMediaTab(button.dataset.libraryMedia)));
bindTabArrows('[data-library-tab]','libraryTab',SML_LIBRARY_TABS,setLibraryTab);
bindTabArrows('[data-library-media]','libraryMedia',SML_MEDIA_TABS,setLibraryMediaTab);
function gotoPage(page){
  const trashRequest=page==='trash';
  if(trashRequest)page='library';
  state.page=page;
  $$('.page').forEach(x=>x.classList.remove('active'));
  $('#page-'+page).classList.add('active');
  $$('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===page));
  $('#pageTitle').textContent=pageMeta[page][0];
  $('#pageSubtitle').textContent=pageMeta[page][1];
  if(page==='library'){
    setLibraryTab(trashRequest?'trash':'media');
    loadClips(true);
    loadImages();
  }
  if(page==='sources'||page==='processing')loadSources();
  if(page==='sources'){loadImports();loadSmlProxyProfiles();}
  if(page==='settings'){loadSettings();loadEditorStatus();}
  if(page==='dashboard')loadDashboard();
}
$$('.nav-item').forEach(b=>b.onclick=()=>gotoPage(b.dataset.page));
$$('[data-goto]').forEach(b=>b.onclick=()=>gotoPage(b.dataset.goto));

async function checkHealth(){ try{ const h=await api('/api/health'); $('#healthDot').classList.toggle('ok',h.ok&&h.ffmpeg); let msg=h.ffmpeg?'Engine online':'Engine online · FFmpeg missing'; msg+=' · VPS local storage'; $('#healthText').textContent=msg; }catch(e){ $('#healthDot').classList.remove('ok'); $('#healthText').textContent=e.status===401||e.status===403?'Website login required':'Engine unavailable · '+e.message; } }
async function loadDashboard(){
 const [s,src,cl,storage]=await Promise.all([api('/api/stats'),api('/api/sources'),api('/api/clips?limit=8'),api('/api/local/storage')]);
 $('#stats').innerHTML=[['Video clips',storage.clips,'Stored on VPS'],['Images',storage.images,'Reusable local assets'],['Library storage',fmtSize(storage.library_bytes),'VPS available: '+fmtSize(storage.free_bytes)],['Pending reviews',storage.pending_reviews,storage.sources_cleaned+' sources cleaned']].map(x=>`<div class="stat-card"><span>${x[0]}</span><strong>${x[1]}</strong><small>${x[2]}</small></div>`).join('');
 renderSources(src.items.slice(0,5),'#recentSources');renderClips(cl.items,'#recentClips',false);
 $('#healthSummary').textContent='VPS used: '+fmtSize(storage.vps_used_bytes)+' / '+fmtSize(storage.total_bytes)+' · '+s.processing+' processing · cleanup waits for complete selection and verified local clips.';
}
function sourceRow(s,processing=false){
 const canProcess=!s.source_deleted_at&&!Number(s.clip_count||0)&&s.status!=='processing';
 return `<div class="source-row"><div class="source-main"><b>${esc(s.filename)}</b><span>${canProcess?'Awaiting start':esc(s.stage)} · ${fmtSize(s.size_bytes)} · ${Number(s.clip_count||0)} clips ${s.source_deleted_at?'· original cleaned':''}</span>${s.error?'<small>'+esc(s.error)+'</small>':''}</div><span class="state">${esc(s.status)}</span>${canProcess?`<button class="ghost" data-process="${esc(s.id)}">Process</button>`:''}</div>`;
}
function bindSourceActions(el){el.querySelectorAll('[data-process]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api('/api/sources/'+b.dataset.process+'/process',{method:'POST'});gotoPage('processing')}catch(e){toast(e.message);b.disabled=false}})}

function renderSources(items, target='#sourcesList'){ const el=$(target); if(!items.length){el.innerHTML='<div class="empty"><b>No source videos yet</b>Add your first video to start building the library.</div>';return;} el.innerHTML=items.map(x=>sourceRow(x,false)).join(''); bindSourceActions(el); }
async function loadSources(){ const d=await api('/api/sources'); state.sources=d.items; renderSources(d.items); const p=$('#processingList'); p.innerHTML=d.items.length?d.items.map(x=>sourceRow(x,true)).join(''):'<div class="empty"><b>Queue is empty</b>Add and process a source video.</div>'; bindSourceActions(p); loadPairReviews(); }

function clipCard(c, selectable=true, trash=false){ const kws=(c.keywords||[]).slice(0,3).map(k=>`<span>${esc(k.keyword)}</span>`).join('')||'<span>untagged</span>'; const extra=(c.keywords||[]).length>3?`<span>+${c.keywords.length-3}</span>`:''; const img=c.thumb_url?`<img loading="lazy" src="${esc(mediaUrl(c.thumb_url))}" alt="">`:'<div class="placeholder">▶</div>'; const drive='VPS · '+(c.local_selection==='pending'?'pair review pending':c.local_selection==='kept'?'verified selection':'local asset'); const orient=esc(c.source_orientation||'unknown'); return `<article class="clip-card ${state.selected.has(c.id)?'selected':''}" data-id="${c.id}">${selectable?'<div class="clip-check">✓</div>':''}<div class="clip-thumb">${img}<span class="duration-pill">${Number(c.duration).toFixed(1)}s</span></div><div class="clip-body"><div class="clip-topline"><span class="clip-id">${esc(c.id)}</span><span class="clip-quality">Q${c.quality||0}</span></div><div class="keyword-preview">${kws}${extra}</div><div class="card-footer">${trash?'Deleted · ':''}${esc(c.source_filename||'')} · ${orient} · ${drive}</div>${trash?'<div class="row-actions trash-actions"><button type="button" class="ghost" data-restore-clip>Restore</button><button type="button" class="danger-soft" data-purge-clip>Delete forever</button></div>':''}</div></article>`; }
function renderClips(items,target,selectable=true,trash=false){ const el=$(target); if(!items.length){ el.innerHTML='<div class="empty" style="grid-column:1/-1"><b>No clips here</b>Your library items will appear here.</div>'; return; } el.innerHTML=items.map(c=>clipCard(c,selectable,trash)).join(''); el.querySelectorAll('.clip-card').forEach(card=>{ card.querySelector('.clip-check')?.addEventListener('click',e=>{e.stopPropagation();toggleSelect(card.dataset.id)}); if(trash){card.querySelector('[data-restore-clip]').onclick=()=>restoreTrashClip(card.dataset.id);card.querySelector('[data-purge-clip]').onclick=()=>purgeTrashClip(card.dataset.id);}else card.addEventListener('click',()=>openDrawer(card.dataset.id)); }); }

async function loadClips(reset=false){ if(reset){state.offset=0;state.clips=[];state.selected.clear();updateBulk();} const q=encodeURIComponent($('#clipSearch').value.trim()), st=$('#statusFilter').value, sort=$('#sortFilter').value, orient=$('#clipOrientation').value; const d=await api(`/api/clips?q=${q}&status=${st}&sort=${sort}&orientation=${orient}&limit=${state.limit}&offset=${state.offset}&date_from=${$('#localFrom').value}&date_to=${$('#localTo').value}`); state.total=d.total; state.clips=reset?d.items:[...state.clips,...d.items]; state.offset=state.clips.length; renderClips(state.clips,'#libraryGrid',true,false); $('#resultCount').textContent=`${d.total} clip${d.total===1?'':'s'}`; $('#loadMore').classList.toggle('hidden',state.clips.length>=d.total); }
let searchTimer; $('#clipSearch').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>loadClips(true),300)}; $('#statusFilter').onchange=()=>loadClips(true); $('#clipOrientation').onchange=()=>loadClips(true); $('#sortFilter').onchange=()=>loadClips(true); $('#loadMore').onclick=()=>loadClips(false);
function toggleSelect(id){ state.selected.has(id)?state.selected.delete(id):state.selected.add(id); renderClips(state.clips,'#libraryGrid',true,false); updateBulk(); }
function updateBulk(){ $('#bulkBar').classList.toggle('hidden',state.selected.size===0); $('#selectedCount').textContent=state.selected.size; }
$('#clearSelection').onclick=()=>{state.selected.clear();renderClips(state.clips,'#libraryGrid');updateBulk()}; $('#selectVisible').onclick=()=>{state.clips.forEach(c=>state.selected.add(c.id));renderClips(state.clips,'#libraryGrid');updateBulk()};
document.querySelectorAll('[data-bulk-status]').forEach(b=>b.onclick=async()=>{
  if(!state.selected.size)return;
  const ids=[...state.selected], buttons=document.querySelectorAll('#bulkBar button');
  buttons.forEach(button=>button.disabled=true);
  try{
    const result=await api('/api/clips/bulk/status',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ids,status:b.dataset.bulkStatus})});
    await loadClips(true);
    const skipped=Array.isArray(result.skipped)?result.skipped:[];
    toast(`Reviewed ${result.updated||0} of ${ids.length} clips`+
      (skipped.length?` · ${skipped.length} protected/unavailable (first: ${skipped[0].reason})`:''));
    if(skipped.length){
      state.selected=new Set(skipped.map(item=>item.id));
      renderClips(state.clips,'#libraryGrid');
      updateBulk();
    }
  }catch(error){toast('Clip review failed: '+error.message)}
  finally{buttons.forEach(button=>button.disabled=false)}
});
$('#bulkDelete').onclick=async()=>{ if(!state.selected.size)return; if(!confirm(`Move ${state.selected.size} selected clips to Trash?`))return; const button=$('#bulkDelete');button.disabled=true;try{const r=await api('/api/clips/bulk/trash',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[...state.selected]})});toast(r.trashed+' clips moved to Trash');await loadClips(true)}catch(e){toast('Unable to delete: '+e.message)}finally{button.disabled=false} };
$('#bulkKeyword').onclick=()=>$('#keywordModal').classList.remove('hidden'); $('#cancelKeywordModal').onclick=()=>$('#keywordModal').classList.add('hidden');
async function bulkKeyword(action){ const keyword=$('#bulkKeywordInput').value.trim(); if(!keyword)return; await api('/api/clips/bulk/keywords',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[...state.selected],keyword,action})}); $('#keywordModal').classList.add('hidden'); $('#bulkKeywordInput').value=''; toast(`Keyword ${action==='add'?'added':'removed'}`); loadClips(true); }
$('#addBulkKeyword').onclick=()=>bulkKeyword('add'); $('#removeBulkKeyword').onclick=()=>bulkKeyword('remove');

function openDrawer(id){ const c=state.clips.find(x=>x.id===id) || state.current; if(!c)return; state.current=JSON.parse(JSON.stringify(c)); $('#drawerTitle').textContent=c.id; $('#drawerVideo').src=mediaUrl(c.clip_url); $('#drawerDescription').textContent=c.description||'No AI description yet.'; $('#drawerSource').textContent=c.source_filename||'Unknown'; $('#drawerTime').textContent=`${fmtTime(c.start_time)} → ${fmtTime(c.end_time)}`; $('#drawerQuality').textContent=`${c.quality||0}/100`; $('#drawerDrive').textContent='VPS local'; $('#drawerStatus').value=c.status; renderDrawerKeywords(); $('#drawerBackdrop').classList.remove('hidden'); $('#clipDrawer').classList.remove('hidden'); }
function renderDrawerKeywords(){ const el=$('#keywordChips'); el.innerHTML=(state.current.keywords||[]).map((k,i)=>`<span>${esc(k.keyword)} <button data-rm="${i}">×</button></span>`).join('')||'<span>no keywords</span>'; el.querySelectorAll('[data-rm]').forEach(b=>b.onclick=()=>{state.current.keywords.splice(Number(b.dataset.rm),1);renderDrawerKeywords()}); }
function closeDrawer(){ $('#drawerBackdrop').classList.add('hidden'); $('#clipDrawer').classList.add('hidden'); $('#drawerVideo').pause(); $('#drawerVideo').removeAttribute('src'); }
$('#closeDrawer').onclick=closeDrawer; $('#drawerBackdrop').onclick=closeDrawer; $('#addKeyword').onclick=()=>{const v=$('#newKeyword').value.trim().toLowerCase();if(!v)return;if(!state.current.keywords.some(k=>k.keyword===v))state.current.keywords.push({keyword:v,source:'manual',confidence:1});$('#newKeyword').value='';renderDrawerKeywords()}; $('#newKeyword').onkeydown=e=>{if(e.key==='Enter')$('#addKeyword').click()};
$('#saveClip').onclick=async()=>{ const id=state.current.id, kws=state.current.keywords.map(k=>k.keyword); await api(`/api/clips/${id}/keywords`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({keywords:kws})}); await api('/api/clips/bulk/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:[id],status:$('#drawerStatus').value})}); toast('Clip changes saved'); closeDrawer(); loadClips(true); };
$('#deleteClip').onclick=async()=>{ if(!confirm('Move this clip to Trash?'))return;try{await api(`/api/clips/${encodeURIComponent(state.current.id)}`,{method:'DELETE'});toast('Clip moved to Trash');closeDrawer();await loadClips(true)}catch(e){toast('Unable to delete: '+e.message)} };
$('#reanalyzeClip').onclick=async()=>{ $('#reanalyzeClip').disabled=true; try{await api(`/api/clips/${state.current.id}/reanalyze`,{method:'POST'});toast('Visual analysis queued; follow progress in Process');closeDrawer();loadClips(true);}catch(e){toast(e.message)}finally{$('#reanalyzeClip').disabled=false} };


async function loadTrash(){ const d=await api('/api/clips?trash=true&limit=200'); state.trash=d.items; renderClips(d.items,'#trashGrid',false,true); await loadImageTrash(); }
async function restoreTrashClip(id){try{await api(`/api/clips/${encodeURIComponent(id)}/restore`,{method:'POST'});toast('Clip restored');await loadTrash()}catch(e){toast(e.message)}}
async function purgeTrashClip(id){if(!confirm('Permanently remove this clip and its local files from VPS? This cannot be undone.'))return;try{await api(`/api/clips/${encodeURIComponent(id)}/permanent`,{method:'DELETE'});toast('Clip permanently deleted');await loadTrash()}catch(e){toast('Unable to delete: '+e.message)}}

async function loadSettings(){const d=await api('/api/settings'),v=d.values;$('#clipMin').value=v.clip_min_seconds||2;$('#clipMax').value=v.clip_max_seconds||4;$('#sceneThreshold').value=v.scene_threshold||.32;$('#aiAuto').checked=v.ai_auto==='true';$('#dupToggle').checked=v.duplicate_detection==='true';$('#qualityToggle').checked=v.quality_filter==='true';$('#aiPill').textContent=v.ai_configured==='true'?'Vision AI connected':'AI adapter ready';loadLocalStorage();}
$('#saveSettings').onclick=async()=>{try{const values={clip_min_seconds:$('#clipMin').value,clip_max_seconds:$('#clipMax').value,scene_threshold:$('#sceneThreshold').value,ai_auto:String($('#aiAuto').checked),duplicate_detection:String($('#dupToggle').checked),quality_filter:String($('#qualityToggle').checked)};await api('/api/settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})});toast('Settings saved')}catch(e){toast(e.message)}};

// Owner-approved compact SML Input controls: retain all existing endpoints/IDs.
const smlUploadTabs=Array.from(document.querySelectorAll('[data-sml-upload]'));
function setSmlUploadTab(kind){
  smlUploadTabs.forEach(button=>{const active=button.dataset.smlUpload===kind;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;});
  for(const pane of document.querySelectorAll('.sml-upload-pane'))pane.hidden=pane.id!=='sml-upload-'+kind;
}
smlUploadTabs.forEach(button=>button.addEventListener('click',()=>setSmlUploadTab(button.dataset.smlUpload)));
const smlImportButtons=Array.from(document.querySelectorAll('[data-sml-import]'));
function setSmlImportSource(source){
  const select=document.getElementById('importSource');select.value=source;
  smlImportButtons.forEach(button=>{const active=button.dataset.smlImport===source;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;});
  document.getElementById('smlImportLabel').textContent='YouTube video URL';
  document.getElementById('importValue').placeholder='https://www.youtube.com/watch?v=...';
  document.getElementById('smlImportHelp').textContent='Only import videos you have permission to reuse.';
  select.dispatchEvent(new Event('change',{bubbles:true}));
}
smlImportButtons.forEach(button=>button.addEventListener('click',()=>setSmlImportSource(button.dataset.smlImport)));
// Import source buttons update the existing select; native change updates eligibility.
document.getElementById('smlChooseImages').addEventListener('click',()=>document.getElementById('imageFiles').click());
document.getElementById('smlChooseFolder').addEventListener('click',()=>document.getElementById('aiFolderInput').click());
document.getElementById('imageFiles').addEventListener('change',event=>{const n=event.target.files.length;document.getElementById('imageUploadResult').textContent=n?n+' image(s) selected. Choose Add selected images to upload.':'';});
const smlImageDrop=document.getElementById('smlImageDrop');
['dragenter','dragover'].forEach(type=>smlImageDrop.addEventListener(type,event=>{event.preventDefault();smlImageDrop.classList.add('drag');}));
['dragleave','drop'].forEach(type=>smlImageDrop.addEventListener(type,event=>{event.preventDefault();smlImageDrop.classList.remove('drag');}));
smlImageDrop.addEventListener('drop',event=>{
  const files=Array.from(event.dataTransfer?.files||[]).filter(file=>/\.(jpe?g|png|webp)$/i.test(file.name));
  if(!files.length){document.getElementById('imageUploadResult').textContent='Choose JPG, PNG, or WEBP images.';return;}
  const transfer=new DataTransfer();files.forEach(file=>transfer.items.add(file));
  const input=document.getElementById('imageFiles');input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));
});

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

const processedFiles=$('#processedClipFiles'), processedDrop=$('#processedDropPanel');
$('#chooseProcessed').onclick=()=>processedFiles.click();
processedFiles.onchange=()=>uploadProcessedClips(Array.from(processedFiles.files||[]));
['dragenter','dragover'].forEach(ev=>processedDrop.addEventListener(ev,e=>{e.preventDefault();processedDrop.classList.add('drag')}));
['dragleave','drop'].forEach(ev=>processedDrop.addEventListener(ev,e=>{e.preventDefault();processedDrop.classList.remove('drag')}));
processedDrop.addEventListener('drop',e=>{e.preventDefault();uploadProcessedClips(Array.from(e.dataTransfer?.files||[]))});
async function uploadProcessedClips(files){
  if(!files.length)return;
  const button=$('#chooseProcessed'), status=$('#processedUploadResult');
  const allowed=/\.(mp4|mov|mkv|webm|m4v|avi)$/i;
  let success=0,duplicates=0,failed=0;
  button.disabled=true;
  try{
    for(let i=0;i<files.length;i++){
      const file=files[i];
      if(!allowed.test(file.name)){failed++;continue;}
      button.textContent='Saving '+(i+1)+'/'+files.length;
      status.textContent='Saving '+(i+1)+'/'+files.length+': '+file.name;
      const data=new FormData();data.append('file',file);
      try{
        const saved=await api('/api/clips/processed/upload',{method:'POST',body:data});
        if(saved.duplicate)duplicates++;else success++;
      }catch(e){failed++;status.textContent='Failed '+file.name+': '+e.message;}
    }
    const summary=success+' saved and automatically approved · '+duplicates+' duplicates skipped · '+failed+' failed';
    status.textContent=summary;toast(summary);
    if(success){
      $('#statusFilter').value='approved';
      $('#clipSearch').value='';$('#clipOrientation').value='all';$('#sortFilter').value='newest';
      await loadDashboard();
      await loadClips(true);
      gotoPage('library');
    }
  }finally{button.disabled=false;button.textContent='Choose processed clips';processedFiles.value='';}
}

$('#refreshSources').onclick=loadSources; $('#refreshProcessing').onclick=loadSources;
setInterval(()=>{if(state.page==='processing')loadSources();},3000);

checkHealth(); loadDashboard();


// Self Media Library V2: image library uses the same guarded bulk workflow as clips.
let imageRows=[], selectedImage=null, imageTotal=0, imageTotalAll=0, imageRequest=0;
const imageSelected=new Set();
function updateImageBulk(){
  const selected=imageSelected.size;
  $('#imageBulkBar').classList.toggle('hidden',selected===0);
  $('#imageSelectedCount').textContent=selected;
  $('#imageCountSummary').textContent='Total images: '+imageTotalAll+
    ' · Selected images: '+selected+
    (imageTotal!==imageTotalAll?' · Matching: '+imageTotal:'')+
    (imageRows.length<imageTotal?' · Showing: '+imageRows.length:'');
}
async function loadImages(reset=true){
  const request=++imageRequest;
  if(reset){imageRows=[];imageSelected.clear();updateImageBulk();}
  const offset=imageRows.length;
  const q=encodeURIComponent($('#imageSearch').value.trim());
  const status=encodeURIComponent($('#imageStatus').value),orientation=encodeURIComponent($('#imageOrientation').value);
  const more=$('#loadMoreImages');
  more.disabled=true;
  try{
    const data=await api('/api/v1/images?q='+q+'&status='+status+'&orientation='+orientation+
      '&limit=200&offset='+offset+'&date_from='+encodeURIComponent($('#localFrom').value)+
      '&date_to='+encodeURIComponent($('#localTo').value));
    if(request!==imageRequest)return;
    imageTotal=data.total;imageTotalAll=data.total_images;
    imageRows=reset?data.items:imageRows.concat(data.items);
    $('#imagesGrid').innerHTML=imageRows.length?imageRows.map(i=>
      '<div class="image-tile"><label class="image-select"><input type="checkbox" data-image-select="'+esc(i.id)+'"'+
      (imageSelected.has(i.id)?' checked':'')+'> Select</label>'+
      '<button class="image-card" type="button" data-image="'+esc(i.id)+'" title="'+esc(i.original_name)+'">'+
      '<span class="image-preview-frame"><img loading="lazy" decoding="async" src="'+esc(mediaUrl(i.thumb_url))+'" alt="Preview of '+esc(i.original_name)+'"></span>'+
      '<b class="image-card-name" title="'+esc(i.original_name)+'">'+esc(i.original_name)+'</b>'+
      '<small class="image-card-meta">'+esc(i.status)+' · VPS local · '+esc(i.width)+'×'+esc(i.height)+' · '+esc(i.orientation)+' · '+esc(i.source_kind||'legacy')+'</small>'+
      '</button></div>').join(''):'<p class="empty">No images match these filters.</p>';
    $$('#imagesGrid [data-image]').forEach(el=>el.onclick=()=>openImageEditor(el.dataset.image));
    $$('#imagesGrid [data-image-select]').forEach(el=>el.onchange=()=>{
      if(el.checked)imageSelected.add(el.dataset.imageSelect);
      else imageSelected.delete(el.dataset.imageSelect);
      updateImageBulk();
    });
    more.classList.toggle('hidden',imageRows.length>=imageTotal);
    updateImageBulk();
  }catch(e){
    if(request===imageRequest)$('#imagesGrid').textContent=e.message;
  }finally{if(request===imageRequest)more.disabled=false}
}
$('#loadMoreImages').onclick=()=>loadImages(false);
$('#imageSearch').oninput=()=>{clearTimeout(window.imageDelay);window.imageDelay=setTimeout(()=>loadImages(true),300)};
$('#imageStatus').onchange=()=>loadImages(true);
$('#imageOrientation').onchange=()=>loadImages(true);
$('#clearImageSelection').onclick=()=>{imageSelected.clear();$$('#imagesGrid [data-image-select]').forEach(el=>el.checked=false);updateImageBulk()};
$$('[data-image-bulk-status]').forEach(button=>button.onclick=()=>runImageBulk('/api/v1/images/bulk/status',
  {ids:[...imageSelected],status:button.dataset.imageBulkStatus},'Image review status updated'));
$('#imageBulkTrash').onclick=async()=>{
  if(!imageSelected.size||!confirm('Move '+imageSelected.size+' selected images to Trash?'))return;
  await runImageBulk('/api/v1/images/bulk/trash',{ids:[...imageSelected]},'Images moved to Trash');
};
$('#imageBulkKeyword').onclick=()=>{$('#imageKeywordModal').classList.remove('hidden');$('#imageBulkKeywordInput').focus()};
$('#cancelImageKeywordModal').onclick=()=>$('#imageKeywordModal').classList.add('hidden');
async function runImageBulk(path,body,message){
  if(!imageSelected.size)return;
  const buttons=$$('#imageBulkBar button');
  buttons.forEach(button=>button.disabled=true);
  try{
    const result=await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast((result.updated||result.trashed||imageSelected.size)+' · '+message);
    await loadImages(true);
    return true;
  }catch(e){toast('Image bulk operation failed: '+e.message);return false}
  finally{buttons.forEach(button=>button.disabled=false)}
}
async function submitImageKeyword(action){
  const keyword=$('#imageBulkKeywordInput').value.trim();
  if(!keyword||!imageSelected.size){toast('Select images and enter a keyword');return}
  const ok=await runImageBulk('/api/v1/images/bulk/keywords',{ids:[...imageSelected],keyword,action},'Image keyword '+action);
  if(ok){$('#imageKeywordModal').classList.add('hidden');$('#imageBulkKeywordInput').value=''}
}
$('#addImageBulkKeyword').onclick=()=>submitImageKeyword('add');
$('#removeImageBulkKeyword').onclick=()=>submitImageKeyword('remove');
async function loadImageTrash(){
  const host=$('#imageTrashGrid');
  if(!host)return;
  try{
    const data=await api('/api/v1/images/trash?limit=500');
    host.innerHTML=data.items.length?data.items.map(i=>
      '<div class="image-tile"><div class="image-card">'+
      '<span class="image-preview-frame"><img loading="lazy" src="'+esc(mediaUrl(i.thumb_url))+'" alt="Trashed image"></span>'+
      '<b class="image-card-name">'+esc(i.original_name)+'</b>'+
      '<small class="image-card-meta">In Trash · '+esc(i.id)+'</small>'+
      '<div class="row-actions"><button class="ghost" type="button" data-image-restore="'+esc(i.id)+'">Restore</button>'+
      '<button class="danger-soft" type="button" data-image-purge="'+esc(i.id)+'">Delete forever</button></div></div></div>').join(''):
      '<p class="empty">No images in Trash.</p>';
    host.querySelectorAll('[data-image-restore]').forEach(b=>b.onclick=async()=>{
      b.disabled=true;
      try{await api('/api/v1/images/'+encodeURIComponent(b.dataset.imageRestore)+'/restore',{method:'POST'});
        toast('Image restored');await loadImageTrash();await loadImages(true)}
      catch(e){toast(e.message);b.disabled=false}
    });
    host.querySelectorAll('[data-image-purge]').forEach(b=>b.onclick=async()=>{
      if(!confirm('Permanently delete this image and metadata? This cannot be undone.'))return;
      b.disabled=true;
      try{await api('/api/local/delete',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({media_type:'image',ids:[b.dataset.imagePurge]})});
        toast('Image permanently deleted');await loadImageTrash();await loadImages(true)}
      catch(e){toast(e.message);b.disabled=false}
    });
  }catch(e){host.textContent='Could not load trashed images: '+e.message}
}
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
// Never silently downgrade a requested proxy import to the VPS direct connection.
let smlProxyProfileReady=null;
let smlProxySaved={enabled:false,active_id:'',profiles:[]};
async function smlProxyRequest(action,payload){
  return api('/api/v1/media/proxy/'+action,{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
  });
}
function resetSmlProxyForm(){
  $('#smlProxyForm').reset();
  $('#smlProxyId').value='';
  $('#smlProxyCancel').hidden=true;
  $('#smlProxySave').textContent='Save profile';
  $('#smlProxyPass').placeholder='Proxy password';
}
function renderSmlProxyProfiles(){
  const p=smlProxySaved;
  $('#smlProxyEnabled').checked=p.enabled===true;
  $('#smlProxyCount').textContent=p.profiles.length+' profiles';
  $('#smlProxyList').innerHTML=p.profiles.length?p.profiles.map(profile=>{
    const active=profile.id===p.active_id;
    return '<div class="sml-proxy-row"><div class="sml-proxy-info"><b>'+esc(profile.name)+(active?' · Active':'')+'</b><small>'+esc(profile.host)+':'+esc(profile.port)+' · '+esc(profile.username)+' · Password saved: '+(profile.has_password?'Yes':'No')+'</small></div><div class="sml-proxy-actions">'+
      '<button type="button" class="ghost" data-sml-proxy-test="'+esc(profile.id)+'">Test</button>'+
      (!active?'<button type="button" class="ghost" data-sml-proxy-activate="'+esc(profile.id)+'">Set active</button>':'')+
      '<button type="button" class="ghost" data-sml-proxy-edit="'+esc(profile.id)+'">Edit</button>'+
      '<button type="button" class="ghost" data-sml-proxy-delete="'+esc(profile.id)+'">Delete</button></div></div>';
  }).join(''):'<p class="empty">No saved proxy profiles. Add your first one above.</p>';
  const list=$('#smlProxyList');
  list.querySelectorAll('[data-sml-proxy-edit]').forEach(button=>button.onclick=()=>{
    const profile=p.profiles.find(x=>x.id===button.dataset.smlProxyEdit);
    if(!profile)return;
    $('#smlProxyId').value=profile.id;
    $('#smlProxyName').value=profile.name;
    $('#smlProxyHost').value=profile.host;
    $('#smlProxyPort').value=profile.port;
    $('#smlProxyUser').value=profile.username;
    $('#smlProxyPass').value='';
    $('#smlProxyPass').placeholder='Leave blank to keep saved password';
    $('#smlProxyCancel').hidden=false;
    $('#smlProxySave').textContent='Update profile';
    $('#smlProxyName').focus();
  });
  list.querySelectorAll('[data-sml-proxy-activate]').forEach(button=>button.onclick=async()=>{
    try{await smlProxyRequest('active',{id:button.dataset.smlProxyActivate});await loadSmlProxyProfiles();await loadImports();toast('Active proxy selected')}
    catch(e){toast(e.message)}
  });
  list.querySelectorAll('[data-sml-proxy-test]').forEach(button=>button.onclick=async()=>{
    button.disabled=true;$('#smlProxyTestResult').textContent='Testing proxy connection…';
    try{
      const r=await smlProxyRequest('test',{id:button.dataset.smlProxyTest});
      $('#smlProxyTestResult').textContent=r.connectivity+' · Proxy connection works; YouTube video/login access not verified.';
    }catch(e){$('#smlProxyTestResult').textContent='Proxy test failed: '+e.message}
    finally{button.disabled=false}
  });
  list.querySelectorAll('[data-sml-proxy-delete]').forEach(button=>button.onclick=async()=>{
    if(!confirm('Delete this SML proxy profile? Saved video jobs remain unchanged.'))return;
    try{await smlProxyRequest('delete',{id:button.dataset.smlProxyDelete});await loadSmlProxyProfiles();await loadImports();toast('Proxy profile deleted')}
    catch(e){toast(e.message)}
  });
}
async function loadSmlProxyProfiles(){
  try{smlProxySaved=await api('/api/v1/media/proxy');renderSmlProxyProfiles();}
  catch(e){$('#smlProxyList').textContent='Proxy settings unavailable: '+e.message}
}
$('#smlProxyManager').addEventListener('toggle',()=>{if($('#smlProxyManager').open)loadSmlProxyProfiles()});
$('#smlOpenProxyManager').onclick=()=>{
  $('#smlProxyOptions').open=true;
  $('#smlProxyManager').open=true;
  loadSmlProxyProfiles();
  $('#smlProxyManager').scrollIntoView({behavior:'smooth',block:'nearest'});
};
$('#smlProxyCancel').onclick=resetSmlProxyForm;
$('#smlProxyForm').onsubmit=async(event)=>{
  event.preventDefault();
  const saveButton=$('#smlProxySave');saveButton.disabled=true;
  try{
    await smlProxyRequest('save',{
      id:$('#smlProxyId').value,name:$('#smlProxyName').value.trim(),
      host:$('#smlProxyHost').value.trim(),port:Number($('#smlProxyPort').value),
      username:$('#smlProxyUser').value.trim(),password:$('#smlProxyPass').value
    });
    resetSmlProxyForm();await loadSmlProxyProfiles();await loadImports();
    toast('SML proxy profile saved. Enable selected profile to use it.');
  }catch(e){toast(e.message)}
  finally{saveButton.disabled=false}
};
$('#smlProxyEnabled').onchange=async()=>{
  const enabled=$('#smlProxyEnabled').checked;
  try{await smlProxyRequest('toggle',{enabled});await loadSmlProxyProfiles();await loadImports();toast(enabled?'SML proxy enabled':'SML proxy disabled')}
  catch(e){$('#smlProxyEnabled').checked=!enabled;toast(e.message)}
};
function renderImports(items, proxyConfigured=smlProxyProfileReady){
  const active=items.filter(item=>item.status==='queued'||item.status==='processing').length;
  $('#importQueueStatus').textContent=active+' active / waiting · '+items.length+' recent jobs'+(proxyConfigured===false?' · proxy unavailable':'');
  const list=$('#importHistory');
  list.innerHTML=items.length?items.map(item=>{
    const status=['queued','processing','completed','failed'].includes(item.status)?item.status:'unknown';
    const progress=status==='completed'?100:Math.min(99,Math.max(0,Number(item.progress)||0));
    const directRetry=status==='failed'&&item.use_proxy&&proxyConfigured===false;
    const retry=status==='failed'?'<button type="button" class="ghost" data-import-retry="'+esc(item.id)+'"'+(directRetry?' data-import-direct="true" title="Connect directly from the VPS without a proxy"':'')+'>'+(directRetry?'Retry direct':'Retry')+'</button>':'';
    const retryProxy=status==='failed'&&!item.use_proxy&&proxyConfigured===true?
      '<button type="button" class="ghost" data-import-retry="'+esc(item.id)+'" data-import-with-proxy="true">Retry via proxy</button>':'';
    return '<div class="source-row sml-import-job" data-import-job="'+esc(item.id)+'"><div class="source-main"><b>'+esc(item.name)+'</b><span>'+esc(item.message||status)+'</span><small>Job '+esc(item.id)+' · '+esc(status)+' · '+progress+'%</small><progress max="100" value="'+progress+'" aria-label="Import '+esc(item.id)+' progress"></progress></div><span class="state">'+esc(status)+'</span>'+retry+retryProxy+'</div>';
  }).join(''):'<p class="empty">No YouTube imports yet. Queue a video URL above.</p>';
  list.querySelectorAll('[data-import-retry]').forEach(button=>button.onclick=async()=>{
    const direct=button.dataset.importDirect==='true';
    const withProxy=button.dataset.importWithProxy==='true';
    if(direct&&!confirm('This retry will download directly through the VPS connection WITHOUT a proxy. Continue?'))return;
    if(withProxy&&!confirm('Retry this saved YouTube URL using the active proxy profile?'))return;
    button.disabled=true;
    try{
      await api('/api/v1/media/imports/'+encodeURIComponent(button.dataset.importRetry)+'/retry',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({direct_without_proxy:direct,with_proxy:withProxy})
      });
      toast(withProxy?'Proxy retry queued':direct?'Direct import queued (no proxy)':'Import retry queued');
      await loadImports();
    }catch(e){toast(e.message);button.disabled=false}
  });
}
async function loadImports(){
  try{
    const result=await api('/api/v1/media/imports');
    smlProxyProfileReady=result.proxy_configured===true;
    $('#importProxyState').textContent=smlProxyProfileReady?'Selected proxy route ready (optional). Use proxy checkbox above to opt in.':'No active proxy. Open Manage proxy profiles to add and enable one, or import directly.';
    renderImports(result.items||[],smlProxyProfileReady);
    updateInputEligibility();
  }catch(e){
    $('#importQueueStatus').textContent='Queue unavailable';
    $('#importHistory').textContent='Cannot load import queue: '+e.message;
  }
}
$('#refreshImports').onclick=loadImports;
$('#importProxy').addEventListener('change',updateInputEligibility);
$('#submitImport').onclick=async()=>{
  const source=$('#importSource').value,value=$('#importValue').value.trim();
  if(!value){toast('Enter a source path or video URL');return}
  const button=$('#submitImport');button.disabled=true;
  try{
    const result=await api('/api/v1/media/imports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      source,value,rights_confirmed:$('#importRights').checked,use_proxy:$('#importProxy').checked
    })});
    renderImports([{id:result.job_id,name:value,status:'queued',progress:0,message:'Waiting for worker'}]);
    toast('Import queued: '+result.job_id);$('#importValue').value='';await loadImports();updateInputEligibility();
  }catch(e){toast(e.message)}finally{updateInputEligibility()}
};
async function loadEditorStatus(){
  try{
    const s=await api('/api/v1/media/status');
    $('#editorApiPill').textContent=s.editor_token_configured?'Editor API enabled':'Editor API key required';
    $('#editorApiPill').classList.toggle('ready',s.editor_token_configured);
    $('#editorApiInfo').textContent=s.images+' stored images · '+s.imports_processing+' imports running';
  }catch(e){$('#editorApiInfo').textContent=e.message}
}
setInterval(()=>{if(state.page==='sources')loadImports()},3000);

// Selection-dependent input actions remain unavailable until input is present.
function updateInputEligibility(){
 const files=document.getElementById('imageFiles'),upload=document.getElementById('uploadImages');if(upload)upload.disabled=!files?.files?.length;
 const value=document.getElementById('importValue')?.value.trim()||'',source=document.getElementById('importSource')?.value;
 const proxyUnavailable=document.getElementById('importProxy')?.checked&&smlProxyProfileReady===false;
 const submit=document.getElementById('submitImport');if(submit){submit.disabled=!value||proxyUnavailable||(source==='youtube'&&!document.getElementById('importRights')?.checked);submit.title=proxyUnavailable?'Proxy unavailable — uncheck the proxy option to import directly.':submit.disabled?'Add a valid source and confirm YouTube usage rights.':'';}
 const batch=document.getElementById('aiBatchImport');if(batch)batch.disabled=!document.getElementById('aiBatchUrls')?.value.trim()||!document.getElementById('aiBatchRights')?.checked;
}
document.addEventListener('input',updateInputEligibility);document.addEventListener('change',updateInputEligibility);updateInputEligibility();

(() => {
  const $ = (id) => document.getElementById(id);
  const projects = [
    {id:1,name:'World Uncovered',handle:'@worlduncovered',initials:'WU',avatar:'',sheet:'SYNCED',total:120,working:5,approval:3,next:'5 Sep · 10:00 AM',more:'+4 more today',status:'RUNNING',daily:5,gap:'2 hours',first:'10:00 AM',active:true},
    {id:2,name:'History of Simple Things',handle:'@historysimplethings',initials:'HS',avatar:'alt',sheet:'SYNCED',total:150,working:4,approval:2,next:'5 Sep · 11:00 AM',more:'+3 more today',status:'RUNNING',daily:5,gap:'3 hours',first:'11:00 AM',active:true},
    {id:3,name:'Everyday Engineering',handle:'@everydayengineering',initials:'EE',avatar:'orange',sheet:'SYNCED',total:88,working:2,approval:1,next:'5 Sep · 1:30 PM',more:'+2 more today',status:'BLOCKED',daily:4,gap:'3 hours',first:'9:30 AM',active:true},
    {id:4,name:'Archive Documentary',handle:'@archivedoc',initials:'AD',avatar:'alt',sheet:'SYNCED',total:64,working:0,approval:0,next:'—',more:'',status:'PAUSED',daily:3,gap:'4 hours',first:'12:00 PM',active:false}
  ];
  const videos = [
    {id:'WU-TR-001',thumb:'t1',title:'Life in Iceland: The Most Beautiful Country on Earth',script:'DONE',scene:'DONE',voice:'DONE',video:'RENDERING',approval:'NOT REQUIRED',upload:'5 Sep 2026|10:00 AM|AUTO',status:'RUNNING'},
    {id:'WU-TR-002',thumb:'t2',title:'Life in Colombia: A Land of Incredible Diversity',script:'DONE',scene:'DONE',voice:'DONE',video:'DONE',approval:'APPROVED',upload:'5 Sep 2026|12:00 PM|AUTO',status:'READY'},
    {id:'WU-TR-003',thumb:'t3',title:'Life in Cuba: Culture, History and Real People',script:'DONE',scene:'DONE',voice:'DONE',video:'DONE',approval:'PENDING',upload:'5 Sep 2026|2:00 PM|AUTO',status:'APPROVAL'},
    {id:'WU-TR-004',thumb:'t4',title:'Life in Netherlands: Beyond the Windmills',script:'WORKING',scene:'WAITING',voice:'WAITING',video:'WAITING',approval:'NOT REQUIRED',upload:'5 Sep 2026|4:00 PM|AUTO',status:'RUNNING'},
    {id:'WU-TR-005',thumb:'t5',title:'Life in China: The Future Is Already Here',script:'DONE',scene:'DONE',voice:'WORKING',video:'WAITING',approval:'PENDING',upload:'6 Sep 2026|10:00 AM|MANUAL',status:'RUNNING'},
    {id:'WU-TR-006',thumb:'t6',title:'Dubai: A City of Dreams and Possibilities',script:'DONE',scene:'DONE',voice:'DONE',video:'ERROR',approval:'NOT REQUIRED',upload:'—|—|MANUAL',status:'ERROR'},
    {id:'WU-TR-007',thumb:'t7',title:'Life in Vietnam: Beauty in Simplicity',script:'DONE',scene:'DONE',voice:'DONE',video:'DONE',approval:'APPROVED',upload:'6 Sep 2026|2:00 PM|AUTO',status:'READY'},
    {id:'WU-TR-008',thumb:'t8',title:'Life in America: A Nation of Opportunity',script:'DONE',scene:'DONE',voice:'DONE',video:'DONE',approval:'APPROVED',upload:'UPLOADED|4 Sep 2026 6:00 PM|VIEW',status:'UPLOADED'}
  ];
  const editableContent = Object.fromEntries(videos.flatMap((v,i)=>[
    [`${v.id}:Script`, {text:`${v.title}\n\nEvery place has a story hidden behind the images people usually see. In this documentary we move beyond the postcard view and look at everyday life, culture, landscapes, people, and the small details that explain what makes this place distinctive.\n\nOur journey begins with the environment and the rhythm of daily life. We then move through history, modern changes, local traditions, food, work, travel, and the experiences of ordinary people. The goal is not simply to list facts, but to build a clear narrative that keeps the viewer curious from one scene to the next.\n\nAs the story develops, each section connects naturally to the next, using visual moments and human details to maintain momentum. The final section brings the main ideas together and leaves the viewer with a stronger understanding of the place than they had at the beginning.`, versions:[{label:'V2 Current',when:'Today · mock',text:'Current saved script version.'},{label:'V1',when:'Earlier · mock',text:'Previous script version before the latest edit.'}]}],
    [`${v.id}:Scene`, {text:`SCENE PLAN — ${v.title}\n\n01 · Opening hook — strong establishing visuals, 0:00–0:20\n02 · Natural environment — wide landscapes and movement, 0:20–1:30\n03 · People and daily life — adults, streets, work, culture, 1:30–3:00\n04 · History / context — archival-feel visual references, 3:00–4:30\n05 · Modern life — infrastructure, cities, transport, 4:30–6:00\n06 · Human details — food, relationships, routines, 6:00–7:30\n07 · Closing sequence — strongest visuals and conclusion, 7:30–end\n\nVisual rule: keep shots concise, varied, and directly connected to narration.`, versions:[{label:'V2 Current',when:'Today · mock',text:'Current scene plan.'},{label:'V1',when:'Earlier · mock',text:'Previous scene plan version.'}]}]
  ]));
  let editorState={id:null,type:null,original:''};
  const videoOps = Object.fromEntries(videos.map(v=>[v.id,{paused:false,locked:false,manual:v.upload.includes('MANUAL'),outdated:[]} ]));
  videoOps['WU-TR-005'].outdated=['Voice','Audio','Video'];
  let commandVideoId=null;
  const downstreamFor=(type)=>type==='Script'?['Voice','Audio','Video']:type==='Scene'?['Video']:[];
  const opFlags=(v)=>{const o=videoOps[v.id]||{};const flags=[];if(o.manual)flags.push(['M','MANUAL','manual']);if(o.locked)flags.push(['L','LOCKED','locked']);if(o.outdated?.length)flags.push(['O',`${o.outdated.length} OUTDATED`,'outdated']);if(v.approval==='PENDING')flags.push(['A','APPROVAL','approval']);return flags;};
  const titleFlags=(v)=>opFlags(v).map(f=>`<span class="af-title-flag ${f[2]}" title="${f[1]}">${f[0]}</span>`).join('');

  let selectedProject = null;
  let currentThumb = null;
  let thumbnailView = 'queue';
  let selected = new Set();
  let scheduleDraftChanged = false;

  // AUTOFLOW_BACKEND_BINDING_V1
  const backend={ready:false,enabled:false,state:null,videoMap:new Map(),profileMap:new Map()};
  const apiJson=async(path,opts={})=>{const res=await fetch(path,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opts.headers||{})},...opts});const text=await res.text();let data={};if(text){try{data=JSON.parse(text);}catch{data={detail:text};}}if(!res.ok){const raw=data?.detail;const detail=typeof raw==='string'?raw:(raw?.message||data?.error||(raw?JSON.stringify(raw):''));const e=new Error(detail||`HTTP ${res.status}`);e.status=res.status;throw e;}return data;}; // API_READ_ONCE_V1
  const bVideo=(id)=>backend.videoMap.get(id)||null;
  const bProjectId=(id)=>bVideo(id)?.project_id||selectedProject?.id||null;
  const assetBy=(v,t)=>(v?.assets||[]).find(a=>a.asset_type===t)||null;
  const stageFromAsset=(a)=>!a?'WAITING':a.outdated?'OUTDATED':(a.text_content||a.file_url||a.source_url)?'DONE':'WAITING';
  const dateParts=(iso,mode='AUTO')=>{if(!iso)return `—|—|${mode}`;try{const d=new Date(iso);return `${d.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'})}|${d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'})}|${mode}`;}catch{return `${iso}|—|${mode}`;}};
  const mapBackendVideo=(v,i)=>({id:v.permanent_id,thumb:`t${(i%8)+1}`,title:v.title||v.permanent_id,script:stageFromAsset(assetBy(v,'script')),scene:stageFromAsset(assetBy(v,'scene')),voice:stageFromAsset(assetBy(v,'voice')||assetBy(v,'audio')),video:stageFromAsset(assetBy(v,'video')),approval:v.approval_status||'PENDING',upload:v.uploaded_at?`UPLOADED|${new Date(v.uploaded_at).toLocaleString()}|VIEW`:dateParts(v.schedule_at,v.schedule_mode||'AUTO'),status:v.status||'WAITING'});
  function syncBackendIntoUi(state){backend.state=state;backend.videoMap=new Map((state.videos||[]).map(v=>[v.permanent_id,v]));backend.profileMap=new Map((state.voice_profiles||[]).map(p=>[p.name,p]));if(!(state.videos||[]).length){backend.enabled=false;return;}backend.enabled=true;const mapped=(state.videos||[]).map(mapBackendVideo);videos.splice(0,videos.length,...mapped);const pRows=(state.projects||[]).map(p=>{const pv=(state.videos||[]).filter(v=>v.project_id===p.id);const scheduled=pv.filter(v=>v.schedule_at&&!v.uploaded_at).sort((a,b)=>String(a.schedule_at).localeCompare(String(b.schedule_at)));return {id:p.id,name:p.name,handle:'',initials:(p.name||'AF').split(/\s+/).map(x=>x[0]).join('').slice(0,2).toUpperCase(),avatar:'',sheet:'SYNCED',total:pv.length,working:pv.filter(v=>['RUNNING','WAITING','PAUSED'].includes(v.status)).length,approval:pv.filter(v=>v.approval_status==='PENDING').length,next:scheduled[0]?.schedule_at?new Date(scheduled[0].schedule_at).toLocaleString():'—',more:scheduled.length>1?`+${scheduled.length-1} more`:'',status:p.enabled?'RUNNING':'PAUSED',daily:p.settings?.daily_upload_limit||5,gap:`${p.settings?.upload_gap_minutes||120} min`,first:p.settings?.first_upload_time||'09:00',active:p.active};});projects.splice(0,projects.length,...pRows);for(const v of state.videos||[]){const locked=(v.assets||[]).some(a=>a.locked),manual=(v.assets||[]).some(a=>a.mode==='MANUAL')||v.schedule_mode==='MANUAL',outdated=(v.assets||[]).filter(a=>a.outdated).map(a=>a.asset_type==='scene'?'Scene':a.asset_type[0].toUpperCase()+a.asset_type.slice(1));videoOps[v.permanent_id]={paused:!!v.paused,locked,manual,outdated};for(const a of v.assets||[]){if(a.asset_type==='script'||a.asset_type==='scene'){const type=a.asset_type==='scene'?'Scene':'Script';editableContent[`${v.permanent_id}:${type}`]={text:a.text_content||'',versions:[]};}}}if((state.voice_profiles||[]).length){const sel=$('af-voice-profile');sel.innerHTML=(state.voice_profiles||[]).map(p=>`<option value="${p.name}">${p.name}</option>`).join('');for(const p of state.voice_profiles||[])voiceProfiles[p.name]={dbid:p.id,id:p.voice_id,stability:Math.round(Number(p.stability)*100),similarity:Math.round(Number(p.similarity)*100),style:Math.round(Number(p.style)*100),boost:!!p.speaker_boost};updateVoiceProfile(sel.value);}selected.clear();commandVideoId=null;controlVideoId=videos.find(v=>v.status!=='UPLOADED')?.id||videos[0]?.id||null;}
  async function loadBackendState(){try{const state=await apiJson('/api/master-controller/control/state');backend.ready=true;syncBackendIntoUi(state);renderProjects();renderAttentionQueue();if(selectedProject&&backend.enabled){selectedProject=projects.find(p=>p.id===selectedProject.id)||projects[0];renderChannelHeader();renderVideos();renderSchedule();renderThumbs();renderControlCenter();}}catch(e){backend.ready=true;backend.enabled=false;console.warn('Auto Flow backend unavailable; keeping mock fallback',e);}}
  async function refreshBackend(){if(!backend.enabled)return;try{syncBackendIntoUi(await apiJson('/api/master-controller/control/state'));renderProjects();renderAttentionQueue();if(selectedProject){selectedProject=projects.find(p=>p.id===selectedProject.id)||projects[0];renderChannelHeader();renderVideos();renderSchedule();renderThumbs();renderControlCenter();}}catch(e){toast(`Backend refresh failed: ${e.message}`);}}
  const controlApi=(id,action,extra={})=>apiJson(`/api/master-controller/projects/${bProjectId(id)}/videos/${encodeURIComponent(id)}/control`,{method:'POST',body:JSON.stringify({action,...extra})});

  const badgeClass = (value) => String(value).toLowerCase().replace(/\s+/g,'').replace('pending','approval').replace('done','approved');
  const toast = (msg) => { const t=$('af-toast'); t.textContent=msg; t.classList.add('show'); clearTimeout(toast.timer); toast.timer=setTimeout(()=>t.classList.remove('show'),2200); };
  const avatar = (p) => `<div class="af-avatar ${p.avatar||''}">${p.initials}</div>`;
  const projectMatches = (p) => {
    const q=$('af-global-search').value.trim().toLowerCase(); const f=$('af-global-filter').value;
    if(q && !`${p.name} ${p.handle}`.toLowerCase().includes(q)) return false;
    if(f==='attention' && !['BLOCKED','ERROR'].includes(p.status) && !p.approval) return false;
    if(f==='running' && p.status!=='RUNNING') return false;
    if(f==='approval' && !p.approval) return false;
    if(f==='archived' && p.status!=='ARCHIVED') return false;
    return true;
  };

  const controlStages = [
    {name:'Script',status:'READY',failed:0,outdated:0},
    {name:'Scene Plan',status:'READY',failed:0,outdated:0},
    {name:'Voice',status:'RUNNING',failed:0,outdated:1},
    {name:'Audio',status:'READY',failed:0,outdated:1},
    {name:'Video',status:'ATTENTION',failed:1,outdated:2},
    {name:'Thumbnail',status:'APPROVAL',failed:0,outdated:0},
    {name:'Approval',status:'PENDING',failed:0,outdated:0},
    {name:'Upload',status:'SCHEDULED',failed:0,outdated:0}
  ];
  const voiceProfiles = {
    'Declan Documentary': {id:'declan_sage_01',stability:50,similarity:80,style:10,boost:true},
    'Natural Male 02': {id:'natural_male_02',stability:62,similarity:76,style:5,boost:true},
    'Warm Narrator': {id:'warm_narrator_07',stability:45,similarity:84,style:18,boost:false}
  };
  let controlVideoId = 'WU-TR-003';
  const controlAssets = ['Script','Scene Plan','Voice Profile','Audio','Video','Thumbnail','Schedule'];
  const assetState = Object.fromEntries(controlAssets.map(a=>[a,{mode:a==='Schedule'?'MANUAL':'AUTO',locked:['Audio','Thumbnail'].includes(a)}]));

  function attentionItems(){
    if(backend.enabled&&backend.state?.attention)return backend.state.attention.map(x=>({kind:String(x.type||'').toLowerCase().replace('missed_manual_slot','error'),icon:x.type==='ERROR'?'!':x.type==='APPROVAL'?'A':x.type==='OUTDATED'?'O':'!',id:x.permanent_id,stage:(x.stage||'Details').replace('scene','Scene').replace('script','Script'),label:String(x.type||'ATTENTION').replaceAll('_',' '),detail:x.message||x.permanent_id}));
    const items=[];
    videos.forEach(v=>{
      if(v.status==='ERROR'||v.video==='ERROR')items.push({kind:'error',icon:'!',id:v.id,stage:'Video',label:'Render Error',detail:v.title});
      if(v.approval==='PENDING')items.push({kind:'approval',icon:'A',id:v.id,stage:'Approval',label:'Thumbnail Approval',detail:v.title});
      const o=videoOps[v.id]; if(o?.outdated?.length)items.push({kind:'outdated',icon:'O',id:v.id,stage:o.outdated[0],label:`${o.outdated.length} Outdated Stage${o.outdated.length>1?'s':''}`,detail:`${v.title} · ${o.outdated.join(' → ')}`});
    });
    return items;
  }
  function renderAttentionQueue(){const list=$('af-attention-queue-list');if(!list)return;const items=attentionItems();$('af-attention-count').textContent=`${items.length} item${items.length===1?'':'s'}`;list.innerHTML=items.length?items.map((x,i)=>`<button class="af-attention-item ${x.kind}" data-attention-item="${i}"><span class="af-attention-icon">${x.icon}</span><span class="af-attention-copy"><strong>${x.label} · ${x.id}</strong><span>${x.detail}</span></span><span class="af-attention-go">→</span></button>`).join(''):`<div class="af-attention-empty">Everything is clear. No errors, approvals or outdated dependencies.</div>`;document.querySelectorAll('[data-attention-item]').forEach(b=>b.onclick=()=>{const x=items[Number(b.dataset.attentionItem)];activateTopTab('projects');openProject(projectIdForVideo(x.id));if(String(x.stage||'').toLowerCase()==='thumbnail'){setSubtab('thumbnails');renderThumbs();setTimeout(()=>document.querySelector(`[data-thumb-card=\"${CSS.escape(x.id)}\"]`)?.scrollIntoView({block:'center'}),20);}else{setSubtab('videos');selectVideoForControl(x.id);if(x.stage==='Script'||x.stage==='Scene')openEditor(x.id,x.stage);else openDetail(x.id,x.stage);}});
    const err=items.filter(x=>x.kind==='error').length,app=items.filter(x=>x.kind==='approval').length,blocked=items.filter(x=>x.kind==='blocked'||x.kind==='outdated').length;const eb=document.querySelector('[data-attention="error"] b'),ab=document.querySelector('[data-attention="approval"] b'),bb=document.querySelector('[data-attention="blocked"] b');if(eb)eb.textContent=`${err} Error${err===1?'':'s'}`;if(ab)ab.textContent=`${app} Approval${app===1?'':'s'}`;if(bb)bb.textContent=`${blocked} Blocked`;
  }
  function selectVideoForControl(id){const v=videos.find(x=>x.id===id);if(!v)return;commandVideoId=id;const strip=$('af-video-command-strip');strip.hidden=false;$('af-command-video-id').textContent=id;$('af-command-video-title').textContent=v.title;$('af-command-flags').innerHTML=opFlags(v).map(f=>`<span class="af-op-flag ${f[2]}">${f[0]} · ${f[1]}</span>`).join('')||'<span class="af-op-flag">AUTO</span>';const o=videoOps[id];const dep=$('af-command-dependency');if(o.outdated?.length){dep.hidden=false;dep.innerHTML=`<b>Dependency warning:</b> ${o.outdated.join(' → ')} ${o.outdated.length===1?'is':'are'} OUTDATED. Automation will not silently rebuild these mock stages.`;}else dep.hidden=true;document.querySelectorAll('tr[data-video]').forEach(r=>r.classList.toggle('af-row-selected',r.dataset.video===id));}
  async function runVideoCommand(action){const v=videos.find(x=>x.id===commandVideoId);if(!v)return toast('Select a video first');const o=videoOps[v.id];if(backend.enabled&&bVideo(v.id)){try{if(action==='Pause'){await controlApi(v.id,'pause');toast(`${v.id} paused · backend`);await refreshBackend();return;}if(action==='Resume'){await controlApi(v.id,'resume');toast(`${v.id} resumed · backend`);await refreshBackend();return;}if(action==='Retry'){await controlApi(v.id,'retry');toast(`${v.id} retry queued`);await refreshBackend();return;}if(action==='Rebuild From Here'){await controlApi(v.id,'rebuild');toast(`${v.id} rebuild queued`);await refreshBackend();return;}if(action==='Lock Asset'){setSubtab('control');controlVideoId=v.id;renderControlCenter();toast(`${v.id} · choose the exact asset to lock`);return;}if(action==='Start From Here'){setSubtab('control');controlVideoId=v.id;renderControlCenter();toast(`${v.id} · choose the exact stage`);return;}if(action==='Change Voice'){setSubtab('control');controlVideoId=v.id;renderControlCenter();toast(`${v.id} · Voice Profile control opened`);return;}if(action==='Replace Asset'){setSubtab('control');controlVideoId=v.id;renderControlCenter();toast(`${v.id} · choose asset to replace`);return;}if(action==='Schedule'){setSubtab('schedule');toast(`${v.id} · schedule board opened`);return;}}catch(e){toast(`${action} failed: ${e.message}`);return;}}if(action==='Pause'){o.paused=true;toast(`${v.id} paused · mock fallback`);}else if(action==='Resume'){o.paused=false;toast(`${v.id} resumed · mock fallback`);}else if(action==='Retry'){if(v.status==='ERROR'){v.status='RUNNING';v.video='RENDERING';}toast(`${v.id} retry staged · mock fallback`);renderVideos();renderAttentionQueue();}else if(action==='Start From Here'){toast(`${v.id} · choose exact stage from Control Center`);setSubtab('control');controlVideoId=v.id;renderControlCenter();}else if(action==='Change Voice'){setSubtab('control');controlVideoId=v.id;renderControlCenter();toast(`${v.id} · Voice Profile control opened`);}else if(action==='Replace Asset'){setSubtab('control');controlVideoId=v.id;renderControlCenter();toast(`${v.id} · manual asset controls opened`);}else if(action==='Lock Asset'){o.locked=!o.locked;toast(`${v.id} ${o.locked?'LOCKED':'UNLOCKED'} · mock fallback`);renderVideos();selectVideoForControl(v.id);}else if(action==='Schedule'){setSubtab('schedule');toast(`${v.id} · schedule board opened`);}else if(action==='Rebuild From Here'){if(!o.outdated.length)return toast('No outdated stages to rebuild');const stages=[...o.outdated];o.outdated=[];if(stages.includes('Voice'))v.voice='WORKING';if(stages.includes('Video'))v.video='WAITING';v.status='RUNNING';toast(`${v.id} rebuild staged from ${stages[0]} · mock fallback`);renderVideos();renderAttentionQueue();selectVideoForControl(v.id);}}

  function renderProjects(){
    let rows=projects.filter(projectMatches);
    $('af-project-body').innerHTML=rows.map(p=>`<tr data-project="${p.id}"><td><div class="af-project-name">${avatar(p)}<div><strong>${p.name}</strong><small>${p.handle}</small></div></div></td><td><span class="af-badge approved">✓ ${p.sheet}</span></td><td>${p.total}</td><td>${p.working}</td><td>${p.approval||'—'}</td><td><div class="af-next-upload"><strong>${p.next}</strong>${p.more?`<small data-schedule-project="${p.id}">${p.more}</small>`:''}</div></td><td><span class="af-badge ${badgeClass(p.status)}">${p.status}</span></td><td><button class="af-row-menu">⋯</button></td></tr>`).join('')||`<tr><td colspan="8">No matching channels.</td></tr>`;
    document.querySelectorAll('[data-project]').forEach(r=>r.onclick=(e)=>{ if(e.target.closest('[data-schedule-project]')) return; openProject(Number(r.dataset.project)); });
    document.querySelectorAll('[data-schedule-project]').forEach(el=>el.onclick=(e)=>{e.stopPropagation(); openProject(Number(el.dataset.scheduleProject)); setSubtab('schedule');});
  }
  function openProject(id){try{sessionStorage.setItem('afProjectId',String(id))}catch{}; selectedProject=projects.find(p=>p.id===id)||projects[0]; $('af-project-list-view').classList.remove('active'); $('af-project-detail-view').classList.add('active'); renderChannelHeader(); renderVideos(); renderSchedule(); renderThumbs(); renderControlCenter(); renderAttentionQueue(); }
  function renderChannelHeader(){ const p=selectedProject; $('af-channel-header').innerHTML=`<section class="af-channel-card"><div class="af-channel-top"><div class="af-channel-id">${avatar(p)}<div><h2>${p.name}</h2><p>${p.handle} · <span class="af-badge approved">Sheet SYNCED</span> · Last sync 18 sec ago</p></div></div><button class="af-next-focus" id="af-next-focus"><small>NEXT UPLOAD</small><strong>${p.next}</strong><span>${p.more||'No more today'}</span></button><div class="af-channel-kpis"><div class="af-stat-mini"><strong>${p.total}</strong><span>Total</span></div><div class="af-stat-mini"><strong>${p.working}</strong><span>Running</span></div><div class="af-stat-mini approval-kpi"><strong>${p.approval}</strong><span>Approval</span></div><div class="af-stat-mini"><strong>18</strong><span>Scheduled</span></div><div class="af-stat-mini"><strong>92</strong><span>Uploaded</span></div><div class="af-stat-mini attention-kpi"><strong>${p.status==='BLOCKED'?2:0}</strong><span>Errors</span></div></div></div><div class="af-channel-controls"><div class="af-control"><label>Daily Limit<input value="${p.daily}" type="number"></label></div><div class="af-control"><label>Upload Gap<select><option>${p.gap}</option><option>2 hours</option><option>3 hours</option><option>4 hours</option></select></label></div><div class="af-control"><label>First Upload<input type="time" value="${p.first.replace(' AM','').replace(' PM','')==='10:00'?'10:00':'09:30'}"></label></div><label class="af-approval-toggle"><input type="checkbox" checked><span>Thumbnail Approval</span></label><div class="af-stage-mini"><label><input type="checkbox" checked> Script</label><label><input type="checkbox" checked> Scene</label><label><input type="checkbox" checked> Voice</label><label><input type="checkbox" checked> Video</label><label><input type="checkbox" checked> Upload</label></div><div class="af-header-actions"><button class="af-btn ghost" id="af-pause-schedule">Pause Scheduling</button><button class="af-btn ghost" id="af-pause-workflow">Pause Workflow</button></div></div></section>`;
    $('af-pause-schedule').onclick=()=>toast('Scheduling pause is frontend-only in this preview'); $('af-pause-workflow').onclick=()=>toast('Workflow pause is frontend-only in this preview'); $('af-next-focus').onclick=()=>setSubtab('schedule');
  }
  const stageBadge=(v)=>`<span class="af-badge ${badgeClass(v)}">${v}</span>`;
  function videoMatches(v){ const q=$('af-video-search').value.trim().toLowerCase(); const f=$('af-video-filter').value; if(q&&!`${v.id} ${v.title}`.toLowerCase().includes(q))return false; if(f==='attention'&&!['ERROR','BLOCKED'].includes(v.status))return false; if(f==='approval'&&v.approval!=='PENDING')return false; if(f==='scheduled'&&!v.upload.includes('AUTO')&&!v.upload.includes('MANUAL'))return false; if(f==='uploaded'&&v.status!=='UPLOADED')return false; return true; }
  function renderVideos(){
    $('af-video-body').innerHTML=videos.filter(videoMatches).map(v=>{const up=v.upload.split('|');const uploaded=v.status==='UPLOADED';return `<tr data-video="${v.id}"><td><input type="checkbox" class="af-row-check" data-check="${v.id}" ${selected.has(v.id)?'checked':''}></td><td><b>${v.id}</b></td><td><div class="af-thumb ${v.thumb}" data-thumb="${v.id}"></div></td><td class="af-full-title"><span>${v.title}</span><div class="af-title-flags">${titleFlags(v)}</div></td><td class="af-status-cell" data-stage="Script">${stageBadge(v.script)}</td><td class="af-status-cell" data-stage="Scene">${stageBadge(v.scene)}</td><td class="af-status-cell" data-stage="Voice">${stageBadge(v.voice)}</td><td class="af-status-cell" data-stage="Video">${stageBadge(v.video)}</td><td class="af-status-cell approval-cell">${stageBadge(v.approval)}</td><td class="af-upload-cell ${uploaded?'youtube-view':''}">${uploaded?`<strong>VIEW</strong><small>Open YouTube</small>`:`<strong>${up[0]}</strong><small>${up[1]}</small><span class="af-badge ${badgeClass(up[2])}">${up[2]}</span>`}</td><td>${stageBadge(v.status)}</td></tr>`}).join('');
    document.querySelectorAll('.af-row-check').forEach(c=>c.onclick=(e)=>{e.stopPropagation(); c.checked?selected.add(c.dataset.check):selected.delete(c.dataset.check); updateSelection();});
    document.querySelectorAll('[data-thumb]').forEach(t=>t.onclick=(e)=>{e.stopPropagation(); openThumb(t.dataset.thumb);});
    document.querySelectorAll('.af-status-cell').forEach(c=>{c.classList.add('af-stage-click');c.onclick=(e)=>{e.stopPropagation();const id=c.closest('tr').dataset.video;const stage=c.dataset.stage||'Approval';selectVideoForControl(id);if(stage==='Script')openEditor(id,'Script');else if(stage==='Scene')openEditor(id,'Scene');else openDetail(id,stage);};});
    document.querySelectorAll('tr[data-video]').forEach(r=>r.onclick=()=>{selectVideoForControl(r.dataset.video);openDetail(r.dataset.video,'Details');});
    document.querySelectorAll('.youtube-view').forEach(c=>c.onclick=(e)=>{e.stopPropagation();toast('Uploaded item would open YouTube directly');}); updateSelection(); if(commandVideoId)selectVideoForControl(commandVideoId);
  }
  function updateSelection(){ $('af-selected-count').textContent=selected.size?`${selected.size} selected across channels`:''; $('af-bulk-btn').disabled=!selected.size; }
  function formatDuration(words){const sec=Math.max(1,Math.round((words/145)*60));return `${Math.floor(sec/60)}:${String(sec%60).padStart(2,'0')}`;}
  function updateEditorMetrics(){const text=$('af-editor-text').value.trim();const words=text?text.split(/\s+/).filter(Boolean).length:0;$('af-editor-words').textContent=words;$('af-editor-duration').textContent=formatDuration(words);$('af-editor-save-state').textContent=text===editorState.original?'Saved':'Unsaved changes';}
  function renderEditorHistory(){const key=`${editorState.id}:${editorState.type}`;const d=editableContent[key];$('af-editor-history-list').innerHTML=(d?.versions||[]).map((v,i)=>`<article class="af-version"><strong>${v.label}</strong><small>${v.when}</small><p>${v.text}</p><button data-restore-version="${i}">Restore this version</button></article>`).join('');document.querySelectorAll('[data-restore-version]').forEach(b=>b.onclick=async()=>{const idx=Number(b.dataset.restoreVersion);const ver=d.versions[idx];if(backend.enabled&&ver?.backendId&&bVideo(editorState.id)){try{const type=editorState.type==='Scene'?'scene':'script';const r=await apiJson(`/api/master-controller/projects/${bProjectId(editorState.id)}/videos/${encodeURIComponent(editorState.id)}/assets/${type}/restore/${ver.backendId}`,{method:'POST'});$('af-editor-text').value=r.asset?.text_content||'';editorState.original=$('af-editor-text').value;toast(`${ver.label} restored · downstream marked OUTDATED`);await refreshBackend();await openEditor(editorState.id,editorState.type);return;}catch(e){toast(`Restore failed: ${e.message}`);return;}}if(idx===0){$('af-editor-text').value=editorState.original;}else{$('af-editor-text').value=`${editorState.original}\n\n[Restored mock content from ${d.versions[idx].label}]`;}updateEditorMetrics();toast(`${d.versions[idx].label} loaded — Save to keep it`);});}

  async function openEditor(id,type){const key=`${id}:${type}`;let d=editableContent[key];const v=videos.find(x=>x.id===id);if(backend.enabled&&bVideo(id)){try{const at=type==='Scene'?'scene':'script';const [asset,versions]=await Promise.all([apiJson(`/api/master-controller/projects/${bProjectId(id)}/videos/${encodeURIComponent(id)}/assets/${at}`),apiJson(`/api/master-controller/projects/${bProjectId(id)}/videos/${encodeURIComponent(id)}/assets/${at}/versions`)]);d=editableContent[key]={text:asset.asset?.text_content||'',versions:(versions.versions||[]).map(x=>({label:`V${x.version}`,when:new Date(x.created_at).toLocaleString(),text:(x.text_content||'').slice(0,220)||'Saved version',backendId:x.id}))};}catch(e){toast(`Could not load ${type}: ${e.message}`);return;}}if(!d)return toast(`${type} editor data is not available`);editorState={id,type,original:d.text};$('af-editor-kicker').textContent=type==='Scene'?'SCENE PLAN':'SCRIPT';$('af-editor-title').textContent=`${type==='Scene'?'Scene Plan':'Script'} Editor`;$('af-editor-subtitle').textContent=`${id} · ${v?.title||id}${backend.enabled?' · LIVE BACKEND':' · MOCK FALLBACK'}`;$('af-editor-text').value=d.text;$('af-editor-history').hidden=true;$('af-editor-modal').hidden=false;updateEditorMetrics();renderEditorHistory();setTimeout(()=>$('af-editor-text').focus(),30);}

  function closeEditor(force=false){if(!force&&$('af-editor-text').value!==editorState.original&&!confirm('Discard unsaved changes?'))return;$('af-editor-modal').hidden=true;$('af-editor-history').hidden=true;}
  function openDetail(id,stage){ const v=videos.find(x=>x.id===id); const d=$('af-detail-drawer'); $('af-detail-content').innerHTML=`<div class="af-drawer-head"><div><span class="af-ai-icon">${stage==='Details'?'#':'✓'}</span><div><strong>${v.id}</strong><small>${stage}</small></div></div><button id="af-detail-close">×</button></div><div class="af-detail-pad"><div class="af-detail-title"><div class="af-thumb ${v.thumb}"></div><div><h3>${v.title}</h3>${stageBadge(v.status)}</div></div><div class="af-detail-tabs"><button class="active">Details</button><button>Files</button><button>Schedule</button><button>History</button></div><div class="af-detail-row"><b>Title</b><span>${v.title}</span></div><div class="af-detail-row"><b>Script</b><span>${v.script} · Open in Docs</span></div><div class="af-detail-row"><b>Scene Plan</b><span>${v.scene} · Open in Docs</span></div><div class="af-detail-row"><b>Voice</b><span>${v.voice} · Audio preview</span></div><div class="af-detail-row"><b>Video</b><span>${v.video} · Preview before upload</span></div><div class="af-detail-row"><b>Thumbnail</b><span>${v.approval}</span></div><div class="af-detail-row"><b>Upload</b><span>${v.upload.replaceAll('|',' · ')}</span></div><div class="af-detail-actions"><button class="af-btn ghost">Pause Item</button><button class="af-btn ghost">Retry Current Stage</button><button class="af-btn primary">Open Stage Actions</button></div></div>`; d.classList.add('open');d.setAttribute('aria-hidden','false');$('af-detail-close').onclick=()=>{d.classList.remove('open');d.setAttribute('aria-hidden','true');}; }
  // AUTOFLOW_THUMBNAIL_PHASE1_UI_V1
  function openThumb(id){
    currentThumb=videos.find(x=>x.id===id); if(!currentThumb)return;
    const bv=bVideo(id), job=bv?.thumbnail_job||null, url=String(job?.result_url||bv?.thumbnail_url||job?.thumbnail_url||'');
    $('af-large-thumb').className=`af-large-thumb ${currentThumb.thumb||''}`;
    $('af-large-thumb').style.backgroundImage=/^(https?:\/\/|\/)/i.test(url)?`url("${url.replaceAll('"','%22')}")`:'';
    $('af-large-thumb').style.backgroundSize=url?'cover':''; $('af-large-thumb').style.backgroundPosition=url?'center':'';
    $('af-thumb-id').textContent=currentThumb.id; $('af-thumb-title').textContent=currentThumb.title;
    $('af-thumb-history').innerHTML=job?`<span>Current · V${job.version||1} · ${job.status||'PENDING'}</span>`:'<span>Current thumbnail</span>';
    $('af-reject-box').hidden=true; $('af-thumb-modal').hidden=false;
  }

  function renderSchedule(){ const sched=videos.filter(v=>v.status!=='UPLOADED'&&v.upload!=='—|—|MANUAL').slice(0,5); $('af-schedule-list').innerHTML=sched.map(v=>{const up=v.upload.split('|');return `<div class="af-schedule-row" draggable="true" data-sched="${v.id}"><span class="af-grip">⋮⋮</span><div class="af-thumb ${v.thumb}" data-thumb="${v.id}"></div><strong>${v.title}</strong><input type="time" value="${up[1].startsWith('10')?'10:00':up[1].startsWith('12')?'12:00':up[1].startsWith('2')?'14:00':'16:00'}"><span class="af-badge ${badgeClass(up[2])}">${up[2]}</span><span>${stageBadge(v.status)}</span></div>`}).join(''); document.querySelectorAll('#af-schedule-list [data-thumb]').forEach(t=>t.onclick=()=>openThumb(t.dataset.thumb)); document.querySelectorAll('#af-schedule-list input[type=time]').forEach(i=>i.onchange=()=>{scheduleDraftChanged=true;toast('Draft time changed · Save Order to apply');}); let dragged=null; document.querySelectorAll('[data-sched]').forEach(row=>{row.ondragstart=()=>dragged=row;row.ondragover=(e)=>e.preventDefault();row.ondrop=(e)=>{e.preventDefault();if(dragged&&dragged!==row){row.parentNode.insertBefore(dragged,row);scheduleDraftChanged=true;toast('Draft order changed');}}}); }
  const thumbEsc=(x)=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const thumbDone=(s)=>['COMPLETED','APPROVED','AUTO_APPROVED'].includes(String(s||'').toUpperCase());
  function thumbnailRows(){
    if(backend.enabled&&Array.isArray(backend.state?.thumbnail_jobs)){
      return backend.state.thumbnail_jobs.filter(j=>!selectedProject||Number(j.project_id)===Number(selectedProject.id));
    }
    return videos.map((v,i)=>({id:i+1,project_id:selectedProject?.id||1,permanent_id:v.id,title:v.title,status:v.approval==='APPROVED'?'COMPLETED':v.approval==='PENDING'?'WAITING_APPROVAL':'PENDING',version:1,prompt:'',reference_image:'',thumbnail_url:'',error_text:''}));
  }
  function renderThumbs(){
    const grid=$('af-thumbnail-grid'); if(!grid||!selectedProject)return;
    const jobs=thumbnailRows(); const queued=jobs.filter(j=>!thumbDone(j.status)&&String(j.status).toUpperCase()!=='REMOVED'); const completed=jobs.filter(j=>thumbDone(j.status));
    $('af-thumb-queue-count').textContent=queued.length; $('af-thumb-completed-count').textContent=completed.length;
    $('af-thumb-view-queue').classList.toggle('active',thumbnailView==='queue'); $('af-thumb-view-completed').classList.toggle('active',thumbnailView==='completed');
    const projectState=(backend.state?.projects||[]).find(p=>Number(p.id)===Number(selectedProject.id)); if($('af-thumb-approval-toggle')&&projectState?.settings)$('af-thumb-approval-toggle').checked=!!projectState.settings.thumbnail_approval_required;
    const rows=(thumbnailView==='completed'?completed:queued);
    if(!rows.length){grid.innerHTML=`<div class="af-thumb-empty"><strong>${thumbnailView==='completed'?'No completed thumbnails yet':'Thumbnail queue is clear'}</strong><span>${thumbnailView==='completed'?'Completed items for this channel will appear here.':'Sheet sync will add ready thumbnail jobs here.'}</span></div>`;return;}
    grid.innerHTML=rows.map(j=>{const st=String(j.status||'PENDING').toUpperCase(),url=String(j.result_url||j.thumbnail_url||j.drive_url||''),ref=String(j.reference_image||''),err=String(j.error_text||'');return `<article class="af-thumb-queue-row" data-thumb-card="${thumbEsc(j.permanent_id)}"><div class="af-thumb-queue-preview" ${url?`data-thumb-preview-url="${encodeURIComponent(url)}"`:''}><span>${url?'':'16:9'}</span></div><div class="af-thumb-queue-main"><div class="af-thumb-queue-title"><b>${thumbEsc(j.permanent_id)}</b>${stageBadge(st)}</div><strong>${thumbEsc(j.title||j.permanent_id)}</strong><p>${thumbEsc(j.prompt||'No thumbnail prompt')}</p><small>V${Number(j.version||1)}${ref?' · Reference attached':''}${j.flow_profile?` · ${thumbEsc(j.flow_profile)}`:''}</small>${err?`<em>${thumbEsc(err)}</em>`:''}</div><span class="af-thumb-queue-go">→</span></article>`}).join('');
    document.querySelectorAll('[data-thumb-preview-url]').forEach(el=>{try{const u=decodeURIComponent(el.dataset.thumbPreviewUrl||'');if(/^(https?:\/\/|\/)/i.test(u)){el.style.backgroundImage=`url("${u.replaceAll('"','%22')}")`;el.classList.add('has-image');}}catch{}});
    document.querySelectorAll('[data-thumb-card]').forEach(c=>c.onclick=()=>openThumb(c.dataset.thumbCard));
  }


  function renderControlCenter(){
    if(!$('af-cc-pipeline')) return;
    const videoSelect=$('af-cc-video');
    videoSelect.innerHTML=videos.filter(v=>v.status!=='UPLOADED').map(v=>`<option value="${v.id}" ${v.id===controlVideoId?'selected':''}>${v.id} — ${v.title}</option>`).join('');
    $('af-cc-pipeline').innerHTML=controlStages.map((s,i)=>`<article class="af-cc-stage"><div class="af-cc-stage-top"><strong>${i+1}. ${s.name}</strong><input type="checkbox" checked title="Enable stage"></div><div class="af-cc-stage-meta"><span>${stageBadge(s.status)}</span><span><b>${s.failed}</b> failed · <b>${s.outdated}</b> outdated</span></div><div class="af-cc-stage-actions"><button data-cc-stage="${s.name}" data-kind="start">Start From Here</button><button data-cc-stage="${s.name}" data-kind="pause">Pause Stage</button></div></article>`).join('');
    document.querySelectorAll('[data-cc-stage]').forEach(b=>b.onclick=async()=>{const label=b.dataset.ccStage,kind=b.dataset.kind;if(backend.enabled&&bVideo(controlVideoId)&&kind==='start'){try{await controlApi(controlVideoId,'start_from_here',{stage:label});toast(`Start From Here · ${label} queued`);await refreshBackend();}catch(e){toast(`Start failed: ${e.message}`);}return;}if(kind==='pause'&&backend.enabled&&bVideo(controlVideoId)){try{await controlApi(controlVideoId,'pause');toast(`${controlVideoId} paused at ${label}`);await refreshBackend();}catch(e){toast(`Pause failed: ${e.message}`);}return;}toast(`${kind==='start'?'Start From Here':'Pause Stage'} · ${label} · mock fallback`);});
    renderControlAssets();
  }
  function renderControlAssets(){
    const v=videos.find(x=>x.id===controlVideoId)||videos[0];
    $('af-cc-manual-title').textContent=`${v.id} · Manual Assets`;
    const note={Script:`${v.script} · editable full-screen script`, 'Scene Plan':`${v.scene} · editable full-screen scene plan`, 'Voice Profile':'Declan Documentary · per-video override allowed', Audio:`${v.voice==='DONE'?'Ready':'Waiting'} · preview / replace`, Video:`${v.video} · preview / replace`, Thumbnail:`${v.approval} · preview / replace`, Schedule:v.upload.replaceAll('|',' · ')};
    $('af-cc-assets').innerHTML=controlAssets.map(a=>{const s=assetState[a];return `<div class="af-cc-asset"><strong>${a}</strong><span class="af-badge ${s.mode.toLowerCase()}">${s.mode}</span><span class="af-lock ${s.locked?'locked':''}">${s.locked?'🔒 LOCKED':'○ UNLOCKED'}</span><span class="af-cc-asset-note">${note[a]}</span><div class="af-cc-asset-actions"><button data-asset-action="preview" data-asset="${a}">${a==='Script'||a==='Scene Plan'?'Edit':'Preview'}</button><button data-asset-action="replace" data-asset="${a}">${a==='Script'||a==='Scene Plan'?'Open':'Replace'}</button><button data-asset-action="lock" data-asset="${a}">${s.locked?'Unlock':'Lock'}</button></div></div>`}).join('');
    document.querySelectorAll('[data-asset-action]').forEach(b=>b.onclick=async()=>{
      const a=b.dataset.asset, action=b.dataset.assetAction;
      const at=a==='Scene Plan'?'scene':a==='Voice Profile'?'voice':a.toLowerCase();
      if(action==='lock'){if(backend.enabled&&bVideo(controlVideoId)&&at!=='voice'){try{await controlApi(controlVideoId,assetState[a].locked?'unlock':'lock',{asset_type:at});assetState[a].locked=!assetState[a].locked;toast(`${a} ${assetState[a].locked?'locked':'unlocked'} · backend`);await refreshBackend();renderControlAssets();}catch(e){toast(`Lock failed: ${e.message}`);}return;}assetState[a].locked=!assetState[a].locked;toast(`${a} ${assetState[a].locked?'locked':'unlocked'} · mock fallback`);renderControlAssets();return;}
      if((a==='Script'||a==='Scene Plan') && action==='preview'){openEditor(controlVideoId,a==='Scene Plan'?'Scene':'Script');return;}
      if(a==='Voice Profile'){toast('Use the Voice Profile panel to apply an override');return;}
      if(action==='replace'){if(backend.enabled&&bVideo(controlVideoId)){const value=prompt(`${a} replacement URL/path${a==='Script'||a==='Scene Plan'?' or use Edit instead':''}:`);if(!value)return;try{await controlApi(controlVideoId,'replace',{asset_type:at,file_url:value});toast(`${a} → MANUAL + LOCKED`);await refreshBackend();renderControlAssets();}catch(e){toast(`Replace failed: ${e.message}`);}return;}assetState[a].mode='MANUAL';assetState[a].locked=true;toast(`${a} replacement → MANUAL + LOCKED · mock fallback`);renderControlAssets();return;}
      toast(`${a} preview opened`);
    });
  }

  function updateVoiceProfile(name){
    const p=voiceProfiles[name]; if(!p) return;
    $('af-voice-id').textContent=p.id;$('af-stability').value=p.stability;$('af-similarity').value=p.similarity;$('af-style').value=p.style;$('af-speaker-boost').checked=p.boost;
    $('af-stability-value').textContent=p.stability;$('af-similarity-value').textContent=p.similarity;$('af-style-value').textContent=p.style;
  }

  function activateTopTab(name){document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));document.querySelectorAll('.af-tab-panel').forEach(p=>p.classList.toggle('active',p.id===`af-tab-${name}`));try{sessionStorage.setItem('afMainTab',name)}catch{}}
  function projectIdForVideo(id){const bv=bVideo(id);if(backend.enabled&&bv?.project_id!=null)return bv.project_id;const prefix=String(id||'').split('-')[0].toUpperCase();return projects.find(p=>String(p.initials||'').toUpperCase()===prefix)?.id||projects[0]?.id||null;}
  function setSubtab(name){document.querySelectorAll('[data-subtab]').forEach(b=>b.classList.toggle('active',b.dataset.subtab===name));document.querySelectorAll('.af-subpanel').forEach(p=>p.classList.toggle('active',p.id===`af-sub-${name}`));try{sessionStorage.setItem('afProjectSubtab',name)}catch{}}
  document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>activateTopTab(b.dataset.tab));
  document.querySelectorAll('[data-subtab]').forEach(b=>b.onclick=()=>setSubtab(b.dataset.subtab));
  $('af-thumb-view-queue').onclick=()=>{thumbnailView='queue';renderThumbs();};
  $('af-thumb-view-completed').onclick=()=>{thumbnailView='completed';renderThumbs();};
  $('af-thumb-refresh').onclick=async()=>{if(backend.enabled){await refreshBackend();toast('Thumbnail queue refreshed');}else{renderThumbs();toast('Thumbnail preview refreshed');}};
  $('af-thumb-approval-toggle').onchange=async(e)=>{if(!selectedProject||!backend.enabled)return toast('Approval setting is available when the project backend is connected');try{await apiJson(`/api/master-controller/projects/${selectedProject.id}/control/settings`,{method:'POST',body:JSON.stringify({thumbnail_approval_required:e.target.checked})});toast(`Thumbnail approval ${e.target.checked?'required':'automatic'}`);await refreshBackend();}catch(err){e.target.checked=!e.target.checked;toast(`Approval setting failed: ${err.message}`);}};
  $('af-back-projects').onclick=()=>{$('af-project-detail-view').classList.remove('active');$('af-project-list-view').classList.add('active');selectedProject=null;try{sessionStorage.removeItem('afProjectId')}catch{}};
  $('af-global-search').oninput=renderProjects;$('af-global-filter').onchange=renderProjects;$('af-global-sort').onchange=()=>toast(`Global ${$('af-global-sort').selectedOptions[0].text} applied`);$('af-sync').onclick=async()=>{if(!backend.ready){toast('Backend is not available · mock data kept');return;}const btn=$('af-sync');const label=btn.textContent;btn.disabled=true;btn.textContent='↻ Syncing…';try{const r=await apiJson('/api/master-controller/google/sync-all',{method:'POST',body:JSON.stringify({trigger_new:true})});const configured=(r.results||[]).filter(x=>x.configured).length;const imported=(r.results||[]).reduce((n,x)=>n+Number(x.sheet?.imported||0),0);const fresh=(r.results||[]).reduce((n,x)=>n+Number(x.sheet?.new||0),0);const removed=(r.results||[]).reduce((n,x)=>n+Number(x.sheet?.removed||0),0);const errors=(r.errors||[]).length;toast(configured?`Google sync · ${imported} rows · ${fresh} new · ${removed} removed${errors?` · ${errors} error`:''}`:'Google sync · no configured project resources');await refreshBackend();}catch(e){toast(`Google sync failed: ${e.message}`);}finally{btn.disabled=false;btn.textContent=label;}};
  document.querySelectorAll('[data-attention]').forEach(b=>b.onclick=()=>{activateTopTab('projects');$('af-project-detail-view').classList.remove('active');$('af-project-list-view').classList.add('active');selectedProject=null;$('af-global-filter').value=b.dataset.attention==='approval'?'approval':'attention';renderProjects();toast('Opened Projects · filtered to needs attention');});
  $('af-video-search').oninput=renderVideos;$('af-video-filter').onchange=renderVideos;$('af-select-all').onchange=(e)=>{videos.forEach(v=>e.target.checked?selected.add(v.id):selected.delete(v.id));renderVideos();};
  $('af-bulk-btn').onclick=()=>toast('Bulk actions panel ready for backend wiring');
  document.querySelectorAll('[data-video-command]').forEach(b=>b.onclick=()=>runVideoCommand(b.dataset.videoCommand));
  $('af-attention-toggle').onclick=()=>{const list=$('af-attention-queue-list');const hidden=list.hidden=!list.hidden;$('af-attention-toggle').textContent=hidden?'Expand':'Collapse';};

  $('af-ai-btn').onclick=()=>{$('af-ai-drawer').classList.add('open');$('af-ai-drawer').setAttribute('aria-hidden','false');};document.querySelector('[data-close="ai"]').onclick=()=>{$('af-ai-drawer').classList.remove('open');$('af-ai-drawer').setAttribute('aria-hidden','true');};
  $('af-editor-text').oninput=updateEditorMetrics;
  $('af-editor-close').onclick=()=>closeEditor();$('af-editor-cancel').onclick=()=>closeEditor();
  $('af-editor-save').onclick=async()=>{const key=`${editorState.id}:${editorState.type}`;const changed=$('af-editor-text').value!==editorState.original;if(backend.enabled&&bVideo(editorState.id)){try{const at=editorState.type==='Scene'?'scene':'script';const r=await apiJson(`/api/master-controller/projects/${bProjectId(editorState.id)}/videos/${encodeURIComponent(editorState.id)}/assets/${at}`,{method:'POST',body:JSON.stringify({text_content:$('af-editor-text').value})});editorState.original=r.asset?.text_content||$('af-editor-text').value;editableContent[key].text=editorState.original;updateEditorMetrics();toast(`${editorState.type==='Scene'?'Scene Plan':'Script'} saved · backend${r.outdated?.length?' · downstream OUTDATED':''}`);await refreshBackend();await openEditor(editorState.id,editorState.type);return;}catch(e){toast(`Save failed: ${e.message}`);return;}}editableContent[key].text=$('af-editor-text').value;editableContent[key].versions.unshift({label:`V${editableContent[key].versions.length+1} Current`,when:'Just now · mock',text:'Saved from Auto Flow full-screen editor.'});if(changed){const o=videoOps[editorState.id];o.outdated=[...new Set([...(o.outdated||[]),...downstreamFor(editorState.type)])];}editorState.original=editableContent[key].text;updateEditorMetrics();renderEditorHistory();renderVideos();renderAttentionQueue();if(commandVideoId===editorState.id)selectVideoForControl(editorState.id);toast(`${editorState.type==='Scene'?'Scene Plan':'Script'} saved · mock fallback${changed?' · downstream OUTDATED':''}`);};
  $('af-editor-doc').onclick=()=>{if(backend.enabled&&bVideo(editorState.id)){const at=editorState.type==='Scene'?'scene':'script',a=assetBy(bVideo(editorState.id),at);if(a?.source_url){window.open(a.source_url,'_blank','noopener');return;}toast('Google Doc URL is not stored for this asset yet');return;}toast('Google Doc would open here after external Docs sync wiring');};
  $('af-editor-history-btn').onclick=()=>{$('af-editor-history').hidden=!$('af-editor-history').hidden;renderEditorHistory();};$('af-editor-history-close').onclick=()=>{$('af-editor-history').hidden=true;};
  $('af-editor-modal').onclick=e=>{if(e.target===$('af-editor-modal'))closeEditor();};
  $('af-thumb-close').onclick=()=>$('af-thumb-modal').hidden=true;$('af-thumb-modal').onclick=(e)=>{if(e.target===$('af-thumb-modal'))$('af-thumb-modal').hidden=true;};
  $('af-approve-thumb').onclick=async()=>{if(!currentThumb)return;if(backend.enabled&&bVideo(currentThumb.id)){try{await apiJson(`/api/master-controller/projects/${bProjectId(currentThumb.id)}/videos/${encodeURIComponent(currentThumb.id)}/thumbnail/approve`,{method:'POST',body:'{}'});toast(`${currentThumb.id} thumbnail approved`);$('af-thumb-modal').hidden=true;await refreshBackend();return;}catch(e){toast(`Approval failed: ${e.message}`);return;}}currentThumb.approval='APPROVED';toast(`${currentThumb.id} thumbnail approved · mock fallback`);$('af-thumb-modal').hidden=true;renderVideos();renderThumbs();};
  $('af-reject-thumb').onclick=()=>{$('af-reject-box').hidden=false;$('af-reject-reason').focus();};$('af-cancel-reject').onclick=()=>{$('af-reject-box').hidden=true;$('af-reject-reason').value='';};
  $('af-redesign-thumb').onclick=()=>{$('af-reject-box').hidden=false;$('af-reject-reason').focus();toast('Enter redesign reason for ChatGPT');};
  $('af-send-redesign').onclick=async()=>{const reason=$('af-reject-reason').value.trim();if(!reason){toast('Rejection reason is required');return;}if(!currentThumb)return;if(backend.enabled&&bVideo(currentThumb.id)){try{await apiJson(`/api/master-controller/projects/${bProjectId(currentThumb.id)}/videos/${encodeURIComponent(currentThumb.id)}/thumbnail/reject`,{method:'POST',body:JSON.stringify({reason})});toast('Thumbnail rejected · backend recorded reason');$('af-thumb-modal').hidden=true;await refreshBackend();return;}catch(e){toast(`Reject failed: ${e.message}`);return;}}currentThumb.approval='PENDING';toast('Redesign request staged · mock fallback');$('af-thumb-modal').hidden=true;renderVideos();renderThumbs();};
  $('af-save-order').onclick=()=>{if(!scheduleDraftChanged){toast('No schedule changes to save');return;}scheduleDraftChanged=false;toast('Schedule draft saved in frontend preview');};

  document.querySelectorAll('[data-cc-action]').forEach(b=>b.onclick=async()=>{const action=b.dataset.ccAction;if(action==='Apply Voice Profile'){const name=$('af-voice-profile').value,p=voiceProfiles[name];if(backend.enabled&&p?.dbid){const ids=$('af-cc-scope').value==='selected'?[...selected]:$('af-cc-scope').value==='one'?[controlVideoId]:videos.filter(v=>v.status!=='UPLOADED').map(v=>v.id);try{for(const id of ids.filter(x=>bVideo(x)))await controlApi(id,'change_voice',{voice_profile_id:p.dbid});toast(`Voice profile applied to ${ids.length} video${ids.length===1?'':'s'} · existing audio not regenerated`);await refreshBackend();}catch(e){toast(`Voice apply failed: ${e.message}`);}return;}toast(`${action} · mock fallback`);return;}if(action==='Save Voice Profile'){const name=$('af-voice-profile').value,p=voiceProfiles[name];if(backend.ready){try{const existing=backend.profileMap.get(name);const body={name,voice_id:$('af-voice-id').textContent.trim(),stability:Number($('af-stability').value)/100,similarity:Number($('af-similarity').value)/100,style:Number($('af-style').value)/100,speaker_boost:$('af-speaker-boost').checked};await apiJson(existing?`/api/master-controller/voice-profiles/${existing.id}`:'/api/master-controller/voice-profiles',{method:'POST',body:JSON.stringify(body)});toast(`Voice profile ${existing?'updated':'created'} · backend`);const state=await apiJson('/api/master-controller/control/state');backend.state=state;backend.profileMap=new Map((state.voice_profiles||[]).map(x=>[x.name,x]));for(const x of state.voice_profiles||[])voiceProfiles[x.name]={dbid:x.id,id:x.voice_id,stability:Math.round(Number(x.stability)*100),similarity:Math.round(Number(x.similarity)*100),style:Math.round(Number(x.style)*100),boost:!!x.speaker_boost};return;}catch(e){toast(`Save profile failed: ${e.message}`);return;}}}if(action==='Rebuild From Changed Stage'&&backend.enabled&&bVideo(controlVideoId)){try{await controlApi(controlVideoId,'rebuild');toast(`${controlVideoId} rebuild queued`);await refreshBackend();}catch(e){toast(`Rebuild failed: ${e.message}`);}return;}toast(`${action} · backend endpoint not bound yet`);});
  $('af-cc-scope').onchange=()=>{const v=$('af-cc-scope').value;$('af-cc-video').disabled=v!=='one';$('af-cc-scope-note').textContent=v==='channel'?'Entire channel selected':v==='selected'?`${selected.size} selected video${selected.size===1?'':'s'}`:'One video selected';};
  $('af-cc-video').onchange=()=>{controlVideoId=$('af-cc-video').value;renderControlAssets();};
  $('af-voice-profile').onchange=()=>{updateVoiceProfile($('af-voice-profile').value);toast(`Voice profile selected: ${$('af-voice-profile').value}`);};
  ['stability','similarity','style'].forEach(k=>{$(`af-${k}`).oninput=()=>{$(`af-${k}-value`).textContent=$(`af-${k}`).value;};});
  $('af-cc-voice-action').onclick=()=>{setSubtab('control');$('af-voice-profile').focus();toast('Choose an Algrow voice profile for the selected scope');};
  $('af-emergency-open').onclick=()=>{$('af-emergency-modal').hidden=false;};
  const closeEmergency=()=>{$('af-emergency-modal').hidden=true;};$('af-emergency-close').onclick=closeEmergency;$('af-emergency-cancel').onclick=closeEmergency;
  $('af-emergency-modal').onclick=e=>{if(e.target===$('af-emergency-modal'))closeEmergency();};
  $('af-emergency-apply').onclick=()=>{const chosen=[...document.querySelectorAll('#af-emergency-modal input:checked')].map(x=>x.value);if(!chosen.length){toast('Select at least one worker/stage');return;}toast(`Emergency stop staged for: ${chosen.join(', ')}`);closeEmergency();};

  const autoFlowLink=[...document.querySelectorAll('a[href="/master-controller"]')][0];
  const hostSidebar=autoFlowLink?.closest('aside') || autoFlowLink?.closest('nav')?.parentElement;
  if(hostSidebar){hostSidebar.classList.add('af-host-sidebar');$('af-mobile-menu').onclick=()=>hostSidebar.classList.toggle('af-mobile-open');document.addEventListener('click',e=>{if(window.innerWidth<=820&&hostSidebar.classList.contains('af-mobile-open')&&!hostSidebar.contains(e.target)&&e.target!==$('af-mobile-menu'))hostSidebar.classList.remove('af-mobile-open');});}else{$('af-mobile-menu').style.display='none';}
  // AUTOFLOW_CHATGPT_EXTENSION_V1_UI
  let chatgptBridgeTimer=null;
  const cgEsc=(v)=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function initChatGPTBridgeUI(){
    const panel=$('af-tab-resources'); if(!panel||$('af-chatgpt-bridge-card'))return;
    panel.insertAdjacentHTML('beforeend',`<div class="af-card" id="af-chatgpt-bridge-card">
      <div class="af-card-head"><div><small>CHATGPT</small><h2>Controls & Browser Workers</h2><p>Auto Flow is the controller. Chrome workers only execute ChatGPT jobs; project Sheet/Docs/Drive references remain on VPS1.</p></div><div><a class="af-btn ghost" id="af-cg-download" href="/static/downloads/autoflow-chatgpt-extension-v1.3.0.zip" download>Download Extension v1.3.0</a> <button class="af-btn ghost" id="af-cg-refresh">Refresh</button></div></div>
      <div class="af-resource-list">
        <div><strong>Pair Extension</strong><span><input id="af-cg-pair-key" readonly placeholder="Generate permanent pairing key" style="min-width:300px;max-width:100%;padding:8px;border:1px solid #d9dee7;border-radius:8px"> <button class="af-btn primary" id="af-cg-pair">Generate Key</button> <button class="af-btn ghost" id="af-cg-copy">Copy</button> <button class="af-btn ghost" id="af-cg-pair-delete">Delete Key</button></span><b class="af-pill neutral" id="af-cg-pair-status">Not generated</b></div>
        <div><strong>Global Parallel Safety Limit</strong><span><input id="af-cg-global-limit" type="number" min="1" max="20" value="8" style="width:80px;padding:8px;border:1px solid #d9dee7;border-radius:8px"> <button class="af-btn ghost" id="af-cg-save-global">Save</button></span><b class="af-pill neutral" id="af-cg-global-state">—</b></div>
        <div><strong>Project Parallel Limit</strong><span><select id="af-cg-project" style="min-width:220px;padding:8px;border:1px solid #d9dee7;border-radius:8px"></select> <input id="af-cg-project-limit" type="number" min="1" max="20" value="3" style="width:70px;padding:8px;border:1px solid #d9dee7;border-radius:8px"> <button class="af-btn ghost" id="af-cg-save-project">Save</button></span><b class="af-pill neutral" id="af-cg-project-state">—</b></div>
        <div><strong>Connection Test</strong><span>Creates a test-only NEW CHAT job. It does not write to project Docs/Sheets.</span><button class="af-btn primary" id="af-cg-test">Send Test Job</button></div>
      </div>
      <div style="margin-top:14px"><div class="af-card-head"><div><small>LIVE WORKERS</small><h3 style="margin:2px 0">Paired ChatGPT Accounts</h3></div><span id="af-cg-summary">Loading…</span></div><div id="af-cg-workers" class="af-resource-list"><div><span>Loading workers…</span></div></div></div>
      <div style="margin-top:14px"><div class="af-card-head"><div><small>RECENT</small><h3 style="margin:2px 0">ChatGPT Jobs</h3></div></div><div id="af-cg-jobs" class="af-resource-list"><div><span>No jobs loaded.</span></div></div></div>
    </div>`);
    $('af-cg-pair').onclick=generateChatGPTPairing;
    $('af-cg-copy').onclick=async()=>{const v=$('af-cg-pair-key').value;if(!v)return toast('Generate a pairing key first');try{await navigator.clipboard.writeText(v);toast('Pairing key copied');}catch{toast('Could not copy pairing key');}};
    $('af-cg-pair-delete').onclick=deleteChatGPTPairingKey;
    const savedPairKey=localStorage.getItem('autoflowChatGPTPermanentPairKey')||''; if(savedPairKey)$('af-cg-pair-key').value=savedPairKey;
    $('af-cg-refresh').onclick=refreshChatGPTBridgeUI;
    $('af-cg-save-global').onclick=saveChatGPTGlobalLimit;
    $('af-cg-project').onchange=loadChatGPTProjectSettings;
    $('af-cg-save-project').onclick=saveChatGPTProjectSettings;
    $('af-cg-test').onclick=sendChatGPTTestJob;
    refreshChatGPTBridgeUI();
    clearInterval(chatgptBridgeTimer); chatgptBridgeTimer=setInterval(refreshChatGPTBridgeUI,10000);
  }
  function cgProjectOptions(){
    const sel=$('af-cg-project'); if(!sel)return;
    const old=sel.value; const rows=(backend.state?.projects?.length?backend.state.projects:projects)||[];
    sel.innerHTML=rows.map(p=>`<option value="${cgEsc(p.id)}">${cgEsc(p.name||`Project ${p.id}`)}</option>`).join('');
    if(old&&[...sel.options].some(o=>o.value===old))sel.value=old;
  }
  async function refreshChatGPTPairingState(){
    if(!$('af-cg-pair-status'))return;
    try{
      const r=await apiJson('/api/master-controller/chatgpt/pairings');
      if(r.active){$('af-cg-pair-status').textContent='Permanent';$('af-cg-pair-status').className='af-pill success';}
      else{$('af-cg-pair-status').textContent='Not generated';$('af-cg-pair-status').className='af-pill neutral';$('af-cg-pair-key').value='';localStorage.removeItem('autoflowChatGPTPermanentPairKey');}
    }catch{}
  }
  async function generateChatGPTPairing(){
    try{
      const r=await apiJson('/api/master-controller/chatgpt/pairings',{method:'POST',body:'{}'});
      const key=`${location.origin}|${r.code}`;
      $('af-cg-pair-key').value=key;
      localStorage.setItem('autoflowChatGPTPermanentPairKey',key);
      $('af-cg-pair-status').textContent='Permanent';
      $('af-cg-pair-status').className='af-pill success';
      toast('Permanent extension pairing key generated');
    }catch(e){toast(`Pairing key failed: ${e.message}`);}
  }
  async function deleteChatGPTPairingKey(){
    if(!confirm('Delete the permanent pairing key? Already paired workers will stay connected.'))return;
    try{
      await apiJson('/api/master-controller/chatgpt/pairings',{method:'DELETE'});
      $('af-cg-pair-key').value='';
      localStorage.removeItem('autoflowChatGPTPermanentPairKey');
      $('af-cg-pair-status').textContent='Not generated';
      $('af-cg-pair-status').className='af-pill neutral';
      toast('Pairing key deleted');
    }catch(e){toast(`Delete key failed: ${e.message}`);}
  }
  async function saveChatGPTGlobalLimit(){try{const max_parallel=Number($('af-cg-global-limit').value||8);await apiJson('/api/master-controller/chatgpt/settings',{method:'POST',body:JSON.stringify({max_parallel})});toast('Global ChatGPT safety limit saved');await refreshChatGPTBridgeUI();}catch(e){toast(`Save failed: ${e.message}`);}}
  async function loadChatGPTProjectSettings(){const id=Number($('af-cg-project')?.value||0);if(!id)return;try{const r=await apiJson(`/api/master-controller/chatgpt/projects/${id}/settings`);$('af-cg-project-limit').value=r.settings?.max_parallel||3;$('af-cg-project-state').textContent=r.settings?.enabled===false?'Disabled':'Enabled';}catch(e){$('af-cg-project-state').textContent='Unavailable';}}
  async function saveChatGPTProjectSettings(){const id=Number($('af-cg-project')?.value||0);if(!id)return toast('Select a project');try{await apiJson(`/api/master-controller/chatgpt/projects/${id}/settings`,{method:'POST',body:JSON.stringify({enabled:true,max_parallel:Number($('af-cg-project-limit').value||3)})});toast('Project ChatGPT parallel limit saved');await loadChatGPTProjectSettings();}catch(e){toast(`Save failed: ${e.message}`);}}
  async function sendChatGPTTestJob(){const id=Number($('af-cg-project')?.value||backend.state?.projects?.[0]?.id||projects[0]?.id||0);if(!id)return toast('No project is available for test');try{const r=await apiJson('/api/master-controller/chatgpt/jobs',{method:'POST',body:JSON.stringify({project_id:id,permanent_id:`TEST-${Date.now()}`,task_type:'CONNECTION_TEST',mode:'NEW_CHAT',test_only:true,prompt_text:'Auto Flow extension connection test. Reply with exactly: AUTO FLOW EXTENSION OK'})});toast(`Test job queued · ${String(r.job?.id||'').slice(0,8)}`);await refreshChatGPTBridgeUI();}catch(e){toast(`Test job failed: ${e.message}`);}}
  async function refreshChatGPTBridgeUI(){
    if(!$('af-chatgpt-bridge-card'))return;
    await refreshChatGPTPairingState();
    try{
      cgProjectOptions();
      const [workers,settings,jobs]=await Promise.all([apiJson('/api/master-controller/chatgpt/workers'),apiJson('/api/master-controller/chatgpt/settings'),apiJson('/api/master-controller/chatgpt/jobs?limit=8')]);
      $('af-cg-global-limit').value=settings.settings?.max_parallel||8;$('af-cg-global-state').textContent=`${settings.running||0} running · ${settings.queued||0} queued`;
      const wr=workers.workers||[];$('af-cg-summary').textContent=`${wr.filter(w=>w.online).length}/${wr.length} online`;
      $('af-cg-workers').innerHTML=wr.length?wr.map(w=>`<div><strong>${cgEsc(w.name)}</strong><span>${cgEsc(w.account_label||'Account not labeled')} · ${cgEsc(w.chrome_profile||'Chrome profile not labeled')}<br><small>${cgEsc(w.browser_state||'UNKNOWN')} · Active ${Number(w.active_count||0)}/${Number(w.max_parallel||1)} · ${w.online?'Online':'Offline'}</small>${w.last_error?`<br><small>${cgEsc(w.last_error)}</small>`:''}</span><b class="af-pill ${w.online&&w.browser_state!=='ATTENTION'?'success':'neutral'}">${w.browser_state==='ATTENTION'?'Needs Attention':w.online?'Connected':'Offline'}</b></div>`).join(''):'<div><strong>No extension paired</strong><span>Download the extension, generate the permanent key, then pair a Chrome profile.</span><b class="af-pill neutral">Waiting</b></div>';
      const jr=jobs.jobs||[];$('af-cg-jobs').innerHTML=jr.length?jr.map(j=>`<div><strong>${cgEsc(j.permanent_id||j.task_type)}</strong><span>${cgEsc(j.task_type)} · ${cgEsc(j.mode)}${j.error_text?`<br><small>${cgEsc(j.error_text)}</small>`:''}</span><b class="af-pill ${j.status==='DONE'?'success':j.status==='ERROR'?'neutral':'neutral'}">${cgEsc(j.status)}</b></div>`).join(''):'<div><span>No ChatGPT extension jobs yet.</span></div>';
      await loadChatGPTProjectSettings();
    }catch(e){$('af-cg-summary').textContent='Bridge unavailable';}
  }
  async function refreshChatGPTAttention(){
    if(!backend.ready)return;
    try{const s=await apiJson('/api/master-controller/control/state');if(backend.state){backend.state.attention=s.attention||[];}else{backend.state=s;}if(backend.enabled)renderAttentionQueue();}catch(e){}
  }
  setInterval(refreshChatGPTAttention,12000);

  // AUTOFLOW_CHATGPT_PROMPT_LIBRARY_V2_UI
  const cgPromptState={projectId:0,slot:'MASTER',prompts:new Map(),routes:[]};
  function initChatGPTPromptLibraryUI(){
    const card=$('af-chatgpt-bridge-card'); if(!card||$('af-cg-prompt-library'))return;
    card.insertAdjacentHTML('beforeend',`<div id="af-cg-prompt-library" style="margin-top:18px;border-top:1px solid #e6e9ef;padding-top:16px">
      <div class="af-card-head"><div><small>PROJECT PROMPT LIBRARY</small><h3 style="margin:2px 0">Master · Script · Scene Plan · Thumbnail · Metadata</h3><p>Prompts are versioned per project. Auto Flow composes the correct slots for each task and routes validated JSON outputs to project assets/Sheet/Docs.</p></div><b class="af-pill neutral" id="af-cg-prompt-version">—</b></div>
      <div style="display:grid;grid-template-columns:minmax(150px,220px) 1fr;gap:12px;align-items:start">
        <div>
          <label style="display:block;font-size:12px;font-weight:700;margin-bottom:5px">Prompt Slot</label>
          <select id="af-cg-prompt-slot" style="width:100%;padding:9px;border:1px solid #d9dee7;border-radius:8px">
            <option value="MASTER">Master</option><option value="SCRIPT">Script</option><option value="SCENE_PLAN">Scene Plan</option><option value="THUMBNAIL">Thumbnail</option><option value="METADATA">Metadata</option>
          </select>
          <label style="display:flex;align-items:center;gap:7px;margin-top:10px"><input id="af-cg-prompt-enabled" type="checkbox" checked> Enabled for composition</label>
          <button class="af-btn primary" id="af-cg-prompt-save" style="margin-top:10px;width:100%">Save Prompt</button>
          <button class="af-btn ghost" id="af-cg-prompt-refresh" style="margin-top:7px;width:100%">Reload</button>
          <div style="margin-top:14px"><small style="font-weight:700">VERSION HISTORY</small><div id="af-cg-prompt-history" style="margin-top:6px;font-size:12px">No history yet.</div></div>
        </div>
        <div><textarea id="af-cg-prompt-text" rows="13" style="width:100%;min-height:260px;resize:vertical;padding:12px;border:1px solid #d9dee7;border-radius:10px;font:inherit;line-height:1.45" placeholder="Project prompt instructions"></textarea></div>
      </div>
      <div style="margin-top:16px;padding-top:14px;border-top:1px solid #e6e9ef">
        <div class="af-card-head"><div><small>AUTO COMPOSITION + ROUTING</small><h3 style="margin:2px 0">Preview or Queue a Library Task</h3></div><b class="af-pill success" id="af-cg-route-state">Routes ready</b></div>
        <div style="display:grid;grid-template-columns:minmax(220px,1fr) minmax(160px,.6fr) minmax(140px,.4fr);gap:10px;align-items:end">
          <label>Task<select id="af-cg-route-task" style="display:block;width:100%;padding:9px;border:1px solid #d9dee7;border-radius:8px;margin-top:4px"></select></label>
          <label>Permanent ID<input id="af-cg-route-pid" placeholder="e.g. WU-TR-001" style="display:block;width:100%;padding:9px;border:1px solid #d9dee7;border-radius:8px;margin-top:4px"></label>
          <label>Mode<select id="af-cg-route-mode" style="display:block;width:100%;padding:9px;border:1px solid #d9dee7;border-radius:8px;margin-top:4px"><option>AUTO</option><option>NEW_CHAT</option><option>FOLLOW_UP</option></select></label>
        </div>
        <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap"><button class="af-btn ghost" id="af-cg-compose-preview">Preview Composed Prompt</button><button class="af-btn primary" id="af-cg-run-library">Queue Library Task</button></div>
        <textarea id="af-cg-composed-preview" readonly rows="8" style="display:none;width:100%;margin-top:10px;padding:10px;border:1px solid #d9dee7;border-radius:9px;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace"></textarea>
      </div>
    </div>`);
    $('af-cg-prompt-slot').onchange=()=>{cgPromptState.slot=$('af-cg-prompt-slot').value;renderChatGPTPromptSlot();};
    $('af-cg-prompt-save').onclick=saveChatGPTPromptSlot;
    $('af-cg-prompt-refresh').onclick=loadChatGPTPromptLibrary;
    $('af-cg-compose-preview').onclick=previewChatGPTLibraryPrompt;
    $('af-cg-run-library').onclick=queueChatGPTLibraryTask;
    const projectSel=$('af-cg-project'); if(projectSel){const prior=projectSel.onchange; projectSel.onchange=async()=>{if(prior)await prior();await loadChatGPTPromptLibrary();};}
    loadChatGPTPromptLibrary();
  }
  function cgPromptProjectId(){return Number($('af-cg-project')?.value||backend.state?.projects?.[0]?.id||projects[0]?.id||0);}
  function cgTaskLabel(t){return String(t||'').replace(/^CHATGPT_GENERATE_/,'').replaceAll('_',' ').toLowerCase().replace(/\b\w/g,c=>c.toUpperCase());}
  async function loadChatGPTPromptLibrary(){
    const id=cgPromptProjectId(); if(!id||!$('af-cg-prompt-library'))return;
    try{
      const r=await apiJson(`/api/master-controller/chatgpt/projects/${id}/prompts`);cgPromptState.projectId=id;cgPromptState.prompts=new Map((r.prompts||[]).map(x=>[x.slot,x]));cgPromptState.routes=r.routes||[];
      const task=$('af-cg-route-task'),old=task.value;task.innerHTML=cgPromptState.routes.map(x=>`<option value="${cgEsc(x.task_type)}">${cgEsc(cgTaskLabel(x.task_type))}</option>`).join('');if(old&&[...task.options].some(o=>o.value===old))task.value=old;
      renderChatGPTPromptSlot();
    }catch(e){toast(`Prompt Library unavailable: ${e.message}`);}
  }
  async function renderChatGPTPromptSlot(){
    const slot=$('af-cg-prompt-slot')?.value||cgPromptState.slot;cgPromptState.slot=slot;const p=cgPromptState.prompts.get(slot);if(!p)return;
    $('af-cg-prompt-text').value=p.prompt_text||'';$('af-cg-prompt-enabled').checked=p.enabled!==false;$('af-cg-prompt-version').textContent=`${slot.replace('_',' ')} · V${p.version||1}`;
    try{const h=await apiJson(`/api/master-controller/chatgpt/projects/${cgPromptState.projectId}/prompts/${encodeURIComponent(slot)}/versions?limit=6`);const rows=h.versions||[];$('af-cg-prompt-history').innerHTML=rows.length?rows.map(v=>`<div style="display:flex;justify-content:space-between;gap:8px;margin:5px 0"><span>V${Number(v.version||0)} · ${cgEsc(String(v.created_at||'').slice(0,16).replace('T',' '))}</span><button class="af-btn ghost" data-cg-prompt-restore="${Number(v.version||0)}" style="padding:3px 7px">Restore</button></div>`).join(''):'No previous versions yet.';document.querySelectorAll('[data-cg-prompt-restore]').forEach(b=>b.onclick=()=>restoreChatGPTPromptVersion(Number(b.dataset.cgPromptRestore)));}catch(e){$('af-cg-prompt-history').textContent='History unavailable';}
  }
  async function saveChatGPTPromptSlot(){
    const id=cgPromptProjectId(),slot=$('af-cg-prompt-slot').value;if(!id)return toast('Select a project');
    try{const r=await apiJson(`/api/master-controller/chatgpt/projects/${id}/prompts/${encodeURIComponent(slot)}`,{method:'POST',body:JSON.stringify({prompt_text:$('af-cg-prompt-text').value,enabled:$('af-cg-prompt-enabled').checked})});toast(r.changed?`${slot.replace('_',' ')} prompt saved as V${r.prompt?.version}`:'No prompt changes');await loadChatGPTPromptLibrary();}catch(e){toast(`Prompt save failed: ${e.message}`);}
  }
  async function restoreChatGPTPromptVersion(version){
    const id=cgPromptProjectId(),slot=$('af-cg-prompt-slot').value;try{await apiJson(`/api/master-controller/chatgpt/projects/${id}/prompts/${encodeURIComponent(slot)}/restore/${version}`,{method:'POST',body:'{}'});toast(`${slot.replace('_',' ')} restored from V${version}`);await loadChatGPTPromptLibrary();}catch(e){toast(`Restore failed: ${e.message}`);}
  }
  async function previewChatGPTLibraryPrompt(){
    const id=cgPromptProjectId(),task=$('af-cg-route-task').value,pid=$('af-cg-route-pid').value.trim()||'PREVIEW-ID';if(!id||!task)return;
    try{const r=await apiJson(`/api/master-controller/chatgpt/projects/${id}/compose`,{method:'POST',body:JSON.stringify({task_type:task,permanent_id:pid,input:{source:'Auto Flow Prompt Library preview'}})});const box=$('af-cg-composed-preview');box.style.display='block';box.value=`${r.prompt_version}\n\n${r.prompt_text}`;toast('Composed prompt preview ready');}catch(e){toast(`Preview failed: ${e.message}`);}
  }
  async function queueChatGPTLibraryTask(){
    const id=cgPromptProjectId(),task=$('af-cg-route-task').value,pid=$('af-cg-route-pid').value.trim(),mode=$('af-cg-route-mode').value;if(!id)return toast('Select a project');if(!pid)return toast('Permanent ID is required');
    try{const r=await apiJson(`/api/master-controller/chatgpt/projects/${id}/run`,{method:'POST',body:JSON.stringify({task_type:task,permanent_id:pid,mode,input:{source:'Auto Flow Prompt Library manual queue'}})});toast(`Library task queued · ${String(r.job?.id||'').slice(0,8)}`);await refreshChatGPTBridgeUI();}catch(e){toast(`Queue failed: ${e.message}`);}
  }
  initChatGPTBridgeUI();
  initChatGPTPromptLibraryUI();
  /* AUTOFLOW_UI_REFRESH_STATE_V1 */
  function afRestoreUiState(){
    let top='dashboard',pid=0,sub='videos';
    try{top=sessionStorage.getItem('afMainTab')||'dashboard';pid=Number(sessionStorage.getItem('afProjectId')||0);sub=sessionStorage.getItem('afProjectSubtab')||'videos';}catch{}
    if(document.querySelector(`[data-tab="${top}"]`)) activateTopTab(top); else activateTopTab('dashboard');
    if(top==='projects'&&pid){try{openProject(pid);setSubtab(sub)}catch{}}
  }
  renderProjects();
  renderAttentionQueue();
  loadBackendState();
  setTimeout(afRestoreUiState,120);setTimeout(afRestoreUiState,850);
})();


// AUTOFLOW_RESOURCES_SUBTABS_V1
(() => {
  const RESOURCE_TABS = [
    ['sheet','Sheets'],
    ['prompts','Prompts'],
    ['chatgpt','ChatGPT'],
    ['flow','Flow'],
  ];
  const classifyResourceNode = (node) => {
    const t=String(node?.textContent||'').replace(/\s+/g,' ').toLowerCase();
    if(/project prompt library|auto composition|preview or queue a library task|prompt slot/.test(t)) return 'prompts';
    if(/chatgpt extension|browser workers|paired chatgpt|chatgpt jobs|connection test|parallel safety limit/.test(t)) return 'chatgpt';
    if(/flow profile|flow account|flow generation|flow worker/.test(t)) return 'flow';
    return 'sheet';
  };
  window.afSetResourceSubtab = (name) => {
    const root=document.getElementById('af-tab-resources'); if(!root) return;
    root.querySelectorAll('[data-af-resource-tab]').forEach(b=>b.classList.toggle('active',b.dataset.afResourceTab===name));
    root.querySelectorAll('[data-af-resource-panel]').forEach(p=>p.classList.toggle('active',p.dataset.afResourcePanel===name));
    try{sessionStorage.setItem('afResourceSubtab',name)}catch{}
  };
  window.afEnsureResourceSubtabs = () => {
    const root=document.getElementById('af-tab-resources');
    if(!root || root.dataset.resourceSubtabsReady==='1') return;
    root.dataset.resourceSubtabsReady='1';
    const original=Array.from(root.children);
    const nav=document.createElement('div'); nav.className='af-resource-subtabs'; nav.setAttribute('aria-label','Resources sections');
    const panels={};
    RESOURCE_TABS.forEach(([key,label])=>{
      const b=document.createElement('button'); b.type='button'; b.dataset.afResourceTab=key; b.textContent=label; b.onclick=()=>window.afSetResourceSubtab(key); nav.appendChild(b);
      const panel=document.createElement('div'); panel.className='af-resource-subpanel'; panel.id=`af-res-panel-${key}`; panel.dataset.afResourcePanel=key; panels[key]=panel;
    });
    const promptLibrary=document.getElementById('af-cg-prompt-library');
    if(promptLibrary) promptLibrary.remove();
    root.prepend(nav);
    RESOURCE_TABS.forEach(([key])=>root.appendChild(panels[key]));
    original.forEach(node=>{
      if(node===nav || node.dataset?.afResourcePanel) return;
      panels[classifyResourceNode(node)].appendChild(node);
    });
    if(promptLibrary && panels.prompts){
      promptLibrary.style.marginTop='0'; promptLibrary.style.borderTop='0'; promptLibrary.style.paddingTop='0';
      panels.prompts.appendChild(promptLibrary);
      const slot=promptLibrary.querySelector('#af-cg-prompt-slot');
      const source=document.getElementById('af-cg-project');
      if(slot && source && !promptLibrary.querySelector('#af-cg-prompt-project')){
        const label=document.createElement('label'); label.textContent='Channel / Project'; label.style.cssText='display:block;font-size:12px;font-weight:700;margin-bottom:5px';
        const select=document.createElement('select'); select.id='af-cg-prompt-project'; select.style.cssText='width:100%;padding:9px;border:1px solid #d9dee7;border-radius:8px;margin-bottom:10px';
        const slotLabel=slot.previousElementSibling;
        const parent=slot.parentElement; parent.insertBefore(label,slotLabel); parent.insertBefore(select,slotLabel);
        const sync=()=>{const keep=select.value||source.value;select.innerHTML=source.innerHTML;if(keep&&[...select.options].some(o=>o.value===keep))select.value=keep;else select.value=source.value;if(select.value)source.value=select.value;};
        select.onchange=()=>{source.value=select.value;try{sessionStorage.setItem('afPromptProject',select.value)}catch{};source.dispatchEvent(new Event('change',{bubbles:true}));};
        source.addEventListener('change',()=>{sync();});
        sync(); const savedProject=sessionStorage.getItem('afPromptProject'); if(savedProject&&[...select.options].some(o=>o.value===savedProject)){select.value=savedProject;source.value=savedProject;source.dispatchEvent(new Event('change',{bubbles:true}));}
        setTimeout(sync,250); setTimeout(sync,900);
      }
    }
    if(!panels.flow.children.length){
      const card=document.createElement('div'); card.id='af-flow-resource-card'; card.className='af-card af-flow-resource-card';
      card.innerHTML=`<span id="af-flow-worker-pair-card" style="display:none"></span><div class="af-card-head"><div><small>GOOGLE FLOW WORKER</small><h2>Auto Flow ↔ Thumbnail Forge</h2><p>Pair the Chrome profile where Google Flow is already logged in. Auto Flow sends thumbnail prompts and receives the generated images back automatically.</p></div><div><a class="af-btn ghost" href="/static/downloads/autoflow-flow-worker-v1.2.0.zip" download>Download Flow Worker</a> <button class="af-btn ghost" id="af-vfw-refresh">Refresh</button></div></div><div class="af-resource-list"><div><strong>Permanent Pairing Key</strong><span><input id="af-fw-pair-key" readonly placeholder="Generate permanent pairing key" style="min-width:300px;max-width:100%;padding:8px;border:1px solid #d9dee7;border-radius:8px"> <button class="af-btn primary" id="af-fw-generate">Generate Key</button> <button class="af-btn ghost" id="af-fw-copy">Copy</button> <button class="af-btn ghost" id="af-fw-delete">Delete Key</button></span><b class="af-pill neutral" id="af-fw-pair-state">Not generated</b></div><div><strong>Connection Test</strong><span>Queues one 16:9 thumbnail job to the paired Flow account.</span><button class="af-btn primary" id="af-fw-test">Send Test Thumbnail</button></div></div><div style="margin-top:14px"><div class="af-card-head"><div><small>LIVE WORKERS</small><h3 style="margin:2px 0">Paired Flow Accounts</h3></div><span id="af-fw-summary">Loading…</span></div><div id="af-fw-workers" class="af-resource-list"><div><span>Loading workers…</span></div></div></div><div style="margin-top:14px"><div class="af-card-head"><div><small>RECENT</small><h3 style="margin:2px 0">Flow Thumbnail Jobs</h3></div></div><div id="af-fw-jobs" class="af-resource-list"><div><span>No Flow jobs yet.</span></div></div></div>`;
      // AUTOFLOW_FLOW_VISIBLE_PAIRING_CARD_V2
      (()=>{
        const q=id=>card.querySelector('#'+id);
        const esc2=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
        const api2=async(url,opt={})=>{const r=await fetch(url,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt});let j={};try{j=await r.json();}catch{}if(!r.ok)throw new Error(j.detail||j.error||('HTTP '+r.status));return j;};
        const notify=t=>{try{if(typeof toast==='function')toast(t);else console.log(t);}catch{console.log(t);}};
        async function refreshVisibleFlow(){
          try{
            const ps=await api2('/api/master-controller/flow/pairings');
            const st=q('af-fw-pair-state');
            if(ps.active){st.textContent='Permanent';st.className='af-pill success';}
            else{st.textContent='Not generated';st.className='af-pill neutral';q('af-fw-pair-key').value='';localStorage.removeItem('autoflowFlowPermanentPairKey');}
          }catch(e){q('af-fw-pair-state').textContent='Bridge unavailable';}
          try{
            const [wr,jr]=await Promise.all([api2('/api/master-controller/flow/workers'),api2('/api/master-controller/flow/jobs?limit=10')]);
            const workers=wr.workers||[]; q('af-fw-summary').textContent=`${workers.filter(w=>w.online).length}/${workers.length} online`;
            q('af-fw-workers').innerHTML=workers.length?workers.map(w=>`<div><strong>${esc2(w.name)}</strong><span>${esc2(w.account_label||'Account not labeled')} · ${esc2(w.chrome_profile||'Chrome profile not labeled')}<br><small>${esc2(w.browser_state||'UNKNOWN')} · Active ${Number(w.active_count||0)}/1${w.last_error?` · ${esc2(w.last_error)}`:''}</small></span><b class="af-pill ${w.online&&w.browser_state!=='ATTENTION'?'success':'neutral'}">${w.browser_state==='ATTENTION'?'Needs Attention':w.online?'Connected':'Offline'}</b></div>`).join(''):'<div><strong>No Flow worker paired</strong><span>Download Flow Worker, generate a key, then pair the Chrome profile containing your logged-in Thumbnail Forge tab.</span><b class="af-pill neutral">Waiting</b></div>';
            const jobs=jr.jobs||[]; q('af-fw-jobs').innerHTML=jobs.length?jobs.map(j=>`<div><strong>${esc2(j.permanent_id||'Flow job')}</strong><span>${esc2(j.title||'Thumbnail')} · ${Number(j.result_count||0)} image(s)${j.error_text?`<br><small>${esc2(j.error_text)}</small>`:''}${j.status==='DONE'&&Number(j.result_count||0)>0?`<br><a href="/api/master-controller/flow/jobs/${encodeURIComponent(j.id)}/images/1" target="_blank">Open returned image</a>`:''}</span><b class="af-pill ${j.status==='DONE'?'success':'neutral'}">${esc2(j.status||'')}</b></div>`).join(''):'<div><span>No Flow jobs yet.</span></div>';
          }catch(e){q('af-fw-summary').textContent='Bridge unavailable';}
        }
        q('af-fw-generate').onclick=async()=>{try{const r=await api2('/api/master-controller/flow/pairings',{method:'POST',body:'{}'});const key=`${location.origin}|${r.code}`;q('af-fw-pair-key').value=key;localStorage.setItem('autoflowFlowPermanentPairKey',key);q('af-fw-pair-state').textContent='Permanent';q('af-fw-pair-state').className='af-pill success';notify('Flow pairing key generated');}catch(e){notify('Pairing key failed: '+e.message);}};
        q('af-fw-copy').onclick=async()=>{const v=q('af-fw-pair-key').value;if(!v)return notify('Generate a pairing key first');try{await navigator.clipboard.writeText(v);notify('Flow pairing key copied');}catch{notify('Could not copy pairing key');}};
        q('af-fw-delete').onclick=async()=>{if(!confirm('Delete the permanent Flow pairing key? Already paired workers stay connected.'))return;try{await api2('/api/master-controller/flow/pairings',{method:'DELETE'});q('af-fw-pair-key').value='';localStorage.removeItem('autoflowFlowPermanentPairKey');await refreshVisibleFlow();notify('Flow pairing key deleted');}catch(e){notify('Delete failed: '+e.message);}};
        q('af-fw-test').onclick=async()=>{try{const r=await api2('/api/master-controller/flow/test-jobs',{method:'POST',body:'{}'});notify('Flow test queued · '+String(r.job?.permanent_id||''));await refreshVisibleFlow();}catch(e){notify('Test failed: '+e.message);}};
        q('af-vfw-refresh').onclick=refreshVisibleFlow;
        const saved=localStorage.getItem('autoflowFlowPermanentPairKey')||'';if(saved)q('af-fw-pair-key').value=saved;
        refreshVisibleFlow();
      })();
      panels.flow.appendChild(card);
    }
    const observer=new MutationObserver(muts=>{
      muts.forEach(m=>Array.from(m.addedNodes||[]).forEach(node=>{
        if(node.nodeType!==1 || node===nav || node.dataset?.afResourcePanel || node.classList?.contains('af-resource-subtabs')) return;
        const bucket=classifyResourceNode(node); if(panels[bucket] && node.parentElement===root) panels[bucket].appendChild(node);
      }));
    });
    observer.observe(root,{childList:true});
    let saved='sheet'; try{saved=sessionStorage.getItem('afResourceSubtab')||'sheet'}catch{}
    if(!panels[saved]) saved='sheet'; window.afSetResourceSubtab(saved);
  };
  const boot=()=>{ window.afEnsureResourceSubtabs(); const rb=document.querySelector('[data-tab="resources"]'); if(rb&&!rb.dataset.afResourceHook){rb.dataset.afResourceHook='1';rb.addEventListener('click',()=>setTimeout(window.afEnsureResourceSubtabs,0));} };
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',boot); else boot();
  setTimeout(boot,350);
})();

/* AUTOFLOW_FLOW_PHASE2A_UI_V1 */
(()=>{
  const $=id=>document.getElementById(id);
  async function req(path,opts={}){
    const r=await fetch(path,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opts.headers||{})},...opts});
    const text=await r.text(); let data={}; try{data=text?JSON.parse(text):{}}catch{data={detail:text||`HTTP ${r.status}`}}
    if(!r.ok) throw new Error(data.detail||data.error||`HTTP ${r.status}`); return data;
  }
  function esc(v){return String(v??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
  function projectOptions(){
    const ps=(window.__afFlowProjects||[]); return ps.map(p=>`<option value="${p.id}">${esc(p.name||('Project '+p.id))}</option>`).join('');
  }
  function ensureCard(){
    const host=$('af-res-panel-flow'); if(!host)return null;
    let card=$('af-flow-phase2a-card');
    if(!card){
      const old=$('af-flow-resource-card'); if(old) old.remove();
      card=document.createElement('div'); card.id='af-flow-phase2a-card'; card.className='af-card';
      host.appendChild(card);
    }
    return card;
  }
  function projectsFromPage(){
    const selects=[...document.querySelectorAll('select')]; const psel=selects.find(s=>[...s.options].some(o=>/test 1|project/i.test(o.textContent||'')));
    if(!psel)return [];
    return [...psel.options].filter(o=>o.value).map(o=>({id:Number(o.value)||o.value,name:o.textContent.trim()}));
  }
  async function load(){
    const card=ensureCard(); if(!card)return;
    try{
      let state={projects:[]};
      try{state=await req('/api/master-controller/control/state')}catch{}
      window.__afFlowProjects=(state.projects||[]).map(p=>({id:p.id,name:p.name}));
      if(!window.__afFlowProjects.length) window.__afFlowProjects=projectsFromPage();
      const projectId=Number(sessionStorage.getItem('af-flow-project')||window.__afFlowProjects[0]?.id||0)||0;
      const data=await req('/api/master-controller/flow/profiles'+(projectId?`?project_id=${projectId}`:''));
      render(card,data,projectId);
    }catch(e){card.innerHTML=`<div class="af-card-head"><div><small>FLOW</small><h2>Flow Profiles</h2></div><span class="af-pill error">Unavailable</span></div><p class="af-flow-error">${esc(e.message)}</p>`;}
  }
  function badge(p){
    if(!p.enabled)return '<span class="af-pill neutral">Disabled</span>';
    if(p.in_cooldown)return '<span class="af-pill approval">Cooldown</span>';
    if(p.eligible)return '<span class="af-pill success">Eligible</span>';
    return '<span class="af-pill neutral">Registered</span>';
  }
  function render(card,data,projectId){
    const profiles=data.profiles||[], bind=data.binding||{lock_mode:'AUTO',flow_profile_id:null}, sel=data.selected||null;
    const rows=profiles.length?profiles.map(p=>`<div class="af-flow-row" data-flow-id="${p.id}">
      <div><strong>${esc(p.name)}${p.is_default?' <span class="af-flow-default">DEFAULT</span>':''}</strong><small>${esc(p.login_state||'UNKNOWN')} · ${esc(p.status||'REGISTERED')}</small></div>
      <div>${badge(p)}<small>Health ${p.health}%</small></div>
      <div><b>${p.used_today}</b><small>used today</small></div>
      <div><b>${p.effective_daily_limit||'—'}</b><small>effective limit</small></div>
      <div><small>Port ${p.debug_port||'—'} · Display :${p.display_num||'—'}</small><small>${esc(p.in_cooldown?'Cooldown active':'Cooldown clear')}</small></div>
      <div class="af-flow-actions"><button data-flow-action="default" data-id="${p.id}">Default</button><button data-flow-action="toggle" data-id="${p.id}" data-enabled="${p.enabled?1:0}">${p.enabled?'Disable':'Enable'}</button><button data-flow-action="limit" data-id="${p.id}" data-limit="${p.manual_daily_limit||0}">Limit</button></div>
    </div>`).join(''):'<div class="af-flow-empty">No Flow profile registered yet. Add the first account/profile below.</div>';
    const pOpts=window.__afFlowProjects.map(p=>`<option value="${p.id}" ${Number(p.id)===Number(projectId)?'selected':''}>${esc(p.name)}</option>`).join('');
    const profileOpts='<option value="">Smart pool</option>'+profiles.map(p=>`<option value="${p.id}" ${Number(bind.flow_profile_id)===Number(p.id)?'selected':''}>${esc(p.name)}</option>`).join('');
    card.innerHTML=`
      <div class="af-card-head"><div><small>FLOW · THUMBNAIL WORKER</small><h2>Flow Profiles & Smart Rotation</h2></div><span class="af-pill success">Phase 2A ready</span></div>
      <p class="af-flow-note">Separate browser profiles for Flow. Smart selection uses availability, cooldown, effective limits, usage and health. Cooldowns/usage persist across restarts.</p>
      <div class="af-flow-summary"><div><b>${profiles.length}</b><span>Profiles</span></div><div><b>${profiles.filter(p=>p.eligible).length}</b><span>Eligible</span></div><div><b>${sel?esc(sel.name):'None'}</b><span>Selected now</span></div><div><b>${esc(data.rotation||'SMART')}</b><span>Rotation</span></div></div>
      <div class="af-flow-list">${rows}</div>
      <div class="af-flow-create"><input id="af-flow-new-name" placeholder="Flow profile name"><input id="af-flow-new-limit" type="number" min="0" placeholder="Daily limit (0 = auto)"><button class="af-btn primary" id="af-flow-add">Add Flow Profile</button><button class="af-btn ghost" id="af-flow-refresh">Refresh</button></div>
      <div class="af-flow-binding"><div><small>PROJECT PROFILE POLICY</small><strong>Channel / project lock</strong></div><label>Project<select id="af-flow-project-select">${pOpts}</select></label><label>Mode<select id="af-flow-lock-mode"><option value="AUTO" ${bind.lock_mode==='AUTO'?'selected':''}>AUTO · smart pool</option><option value="PREFER" ${bind.lock_mode==='PREFER'?'selected':''}>PREFER · profile then fallback</option><option value="STRICT" ${bind.lock_mode==='STRICT'?'selected':''}>STRICT · wait for profile</option></select></label><label>Profile<select id="af-flow-bind-profile">${profileOpts}</select></label><button class="af-btn primary" id="af-flow-save-binding">Save</button></div>
      <div class="af-flow-foot">Browser/login controls are intentionally not started yet. Phase 2B will create the isolated Flow Chrome session and wire real thumbnail generation into the existing queue.</div>`;
    wire();
  }
  function wire(){
    const add=$('af-flow-add'); if(add)add.onclick=async()=>{const name=$('af-flow-new-name').value.trim(); if(!name)return alert('Enter a Flow profile name'); const manual=Number($('af-flow-new-limit').value||0); try{await req('/api/master-controller/flow/profiles',{method:'POST',body:JSON.stringify({name,manual_daily_limit:manual})}); await load();}catch(e){alert(e.message)}};
    const ref=$('af-flow-refresh'); if(ref)ref.onclick=load;
    const ps=$('af-flow-project-select'); if(ps)ps.onchange=()=>{sessionStorage.setItem('af-flow-project',ps.value);load();};
    const save=$('af-flow-save-binding'); if(save)save.onclick=async()=>{const pid=Number($('af-flow-project-select').value||0); if(!pid)return; const mode=$('af-flow-lock-mode').value, profile=$('af-flow-bind-profile').value; try{await req(`/api/master-controller/projects/${pid}/flow/binding`,{method:'POST',body:JSON.stringify({lock_mode:mode,flow_profile_id:profile||null})}); await load();}catch(e){alert(e.message)}};
    document.querySelectorAll('[data-flow-action]').forEach(b=>b.onclick=async()=>{const id=Number(b.dataset.id), a=b.dataset.flowAction; try{if(a==='default') await req(`/api/master-controller/flow/profiles/${id}`,{method:'POST',body:JSON.stringify({is_default:true})}); else if(a==='toggle') await req(`/api/master-controller/flow/profiles/${id}/action`,{method:'POST',body:JSON.stringify({action:b.dataset.enabled==='1'?'DISABLE':'ENABLE'})}); else if(a==='limit'){const cur=b.dataset.limit||'0', val=prompt('Manual daily limit. Use 0 for automatic/unknown.',cur); if(val===null)return; await req(`/api/master-controller/flow/profiles/${id}`,{method:'POST',body:JSON.stringify({manual_daily_limit:Number(val||0)})});} await load();}catch(e){alert(e.message)}});
  }
  function start(){
    const top=$('af-tab-resources'); if(!top){setTimeout(start,400);return;}
    const mo=new MutationObserver(()=>{if($('af-res-panel-flow')&&!$('af-flow-phase2a-card'))load();}); mo.observe(top,{childList:true,subtree:true});
    if($('af-res-panel-flow'))load(); else setTimeout(start,400);
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
// AUTOFLOW_FLOW_PHASE2B_BROWSER_UI_V1
(()=>{
  const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  async function req(url,opt={}){const r=await fetch(url,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt});const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.detail||j.error||`${r.status}`);return j;}
  let timer=null;
  async function render(){
    const host=$('af-res-panel-flow'); if(!host)return;
    let card=$('af-flow-phase2b-browser');
    if(!card){card=document.createElement('div');card.id='af-flow-phase2b-browser';card.className='af-card';host.appendChild(card);}
    try{
      const list=await req('/api/master-controller/flow/profiles'); const profiles=list.profiles||[];
      if(!profiles.length){card.innerHTML='<div class="af-card-head"><div><small>FLOW BROWSER BRIDGE</small><h2>Google Flow Communication</h2></div></div><p>Create a Flow profile above first.</p>';return;}
      let pid=Number(sessionStorage.getItem('af-flow-browser-profile')||profiles[0].id); if(!profiles.some(p=>Number(p.id)===pid))pid=Number(profiles[0].id);
      const d=await req(`/api/master-controller/flow/profiles/${pid}/browser`); const p=d.profile||{}, b=d.browser||{};
      const opts=profiles.map(x=>`<option value="${x.id}" ${Number(x.id)===pid?'selected':''}>${esc(x.name)}</option>`).join('');
      const state=esc(p.browser_state||'STOPPED'), login=esc(p.login_state||b.login_state||'UNKNOWN');
      card.innerHTML=`<div class="af-card-head"><div><small>FLOW BROWSER BRIDGE</small><h2>Auto Flow ↔ Google Flow</h2></div><span class="af-pill ${state==='RUNNING'?'success':'neutral'}">${state}</span></div>
        <p class="af-flow-note">Dedicated persistent Chrome profile on VPS1. Login once in Viewer; Auto Flow then communicates with Thumbnail Forge through the local browser bridge.</p>
        <div class="af-flowb-summary"><div><span>Profile</span><b>${esc(p.name)}</b></div><div><span>Login</span><b>${login}</b></div><div><span>CDP</span><b>${b.reachable?'Connected':'Offline'}</b></div><div><span>Page</span><b>${esc((b.title||p.last_browser_title||'—').slice(0,32))}</b></div></div>
        <div class="af-flowb-controls"><label>Flow Profile<select id="af-flowb-profile">${opts}</select></label><button class="af-btn primary" data-flowb="START">Start Browser</button><button class="af-btn ghost" data-flowb="RESTART">Restart</button><button class="af-btn ghost" data-flowb="OPEN_TOOL">Open Tool</button><button class="af-btn ghost" id="af-flowb-test">Test Connection</button><a class="af-btn ghost af-flowb-view" href="${esc(d.viewer_url||'#')}" target="_blank" rel="noopener">Open Viewer</a><button class="af-btn danger" data-flowb="STOP">Stop</button></div>
        <div class="af-flowb-config"><input id="af-flowb-url" value="${esc(p.tool_url||'')}" placeholder="https://flow.google.com/project/.../tool/..."><button class="af-btn ghost" id="af-flowb-save">Save Tool URL</button></div>
        <div id="af-flowb-result" class="af-flowb-result">${b.tool_detected?'Thumbnail Forge detected and ready.':(b.reachable?'Browser connected. Open Viewer if Google login is required.':'Start the browser to connect.')}</div>`;
      $('af-flowb-profile').onchange=e=>{sessionStorage.setItem('af-flow-browser-profile',e.target.value);render();};
      card.querySelectorAll('[data-flowb]').forEach(btn=>btn.onclick=async()=>{btn.disabled=true;try{const x=await req(`/api/master-controller/flow/profiles/${pid}/browser/action`,{method:'POST',body:JSON.stringify({action:btn.dataset.flowb})});if(btn.dataset.flowb==='OPEN_TOOL'&&x.browser?.tool_detected)$('af-flowb-result').textContent='Thumbnail Forge detected and ready.';setTimeout(render,900);}catch(e){$('af-flowb-result').textContent=e.message;}finally{btn.disabled=false;}});
      $('af-flowb-test').onclick=async()=>{const out=$('af-flowb-result');out.textContent='Testing Flow DOM…';try{const x=await req(`/api/master-controller/flow/profiles/${pid}/browser/action`,{method:'POST',body:JSON.stringify({action:'TEST'})});const b=x.browser||{};out.textContent=b.tool_detected?'PASS · Thumbnail Forge inputs and Generate button detected.':`Connected · ${b.login_state||'UNKNOWN'}${b.probe_error?' · '+b.probe_error:''}`;}catch(e){out.textContent=e.message;}};
      $('af-flowb-save').onclick=async()=>{try{await req(`/api/master-controller/flow/profiles/${pid}/browser/config`,{method:'POST',body:JSON.stringify({tool_url:$('af-flowb-url').value.trim()})});$('af-flowb-result').textContent='Tool URL saved.';}catch(e){$('af-flowb-result').textContent=e.message;}};
    }catch(e){card.innerHTML=`<div class="af-card-head"><div><small>FLOW BROWSER BRIDGE</small><h2>Google Flow Communication</h2></div><span class="af-pill error">Unavailable</span></div><p class="af-flow-error">${esc(e.message)}</p>`;}
  }
  function start(){const host=$('af-tab-resources');if(!host){setTimeout(start,500);return;}const mo=new MutationObserver(()=>{if($('af-res-panel-flow')&&!$('af-flow-phase2b-browser'))render();});mo.observe(host,{childList:true,subtree:true});render();clearInterval(timer);timer=setInterval(()=>{if($('af-res-panel-flow')&&$('af-flow-phase2b-browser'))render();},15000);}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
// AUTOFLOW_FLOW_WORKER_PAIRING_UI_V1
(()=>{
  const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const req=async(url,opt={})=>{const r=await fetch(url,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt});const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.detail||j.error||`HTTP ${r.status}`);return j;};
  let timer=null;
  function init(){const host=$('af-res-panel-flow');if(!host){setTimeout(init,500);return;}if($('af-flow-worker-pair-card'))return refresh();const card=document.createElement('div');card.id='af-flow-worker-pair-card';card.className='af-card';card.innerHTML=`<div class="af-card-head"><div><small>GOOGLE FLOW WORKER</small><h2>Auto Flow ↔ Thumbnail Forge</h2><p>Pair the Chrome profile where Google Flow is already logged in. Auto Flow sends prompts; the worker returns generated thumbnail images.</p></div><div><a class="af-btn ghost" href="/static/downloads/autoflow-flow-worker-v1.2.0.zip" download>Download Flow Worker</a> <button class="af-btn ghost" id="af-fw-refresh">Refresh</button></div></div>
    <div class="af-resource-list">
      <div><strong>Permanent Pairing Key</strong><span><input id="af-fw-pair-key" readonly placeholder="Generate pairing key" style="min-width:300px;max-width:100%;padding:8px;border:1px solid #d9dee7;border-radius:8px"> <button class="af-btn primary" id="af-fw-generate">Generate Key</button> <button class="af-btn ghost" id="af-fw-copy">Copy</button> <button class="af-btn ghost" id="af-fw-delete">Delete Key</button></span><b class="af-pill neutral" id="af-fw-pair-state">Not generated</b></div>
      <div><strong>Connection Test</strong><span>Queues one 16:9 test thumbnail through your paired Flow account.</span><button class="af-btn primary" id="af-fw-test">Send Test Thumbnail</button></div>
    </div>
    <div style="margin-top:14px"><div class="af-card-head"><div><small>LIVE WORKERS</small><h3 style="margin:2px 0">Paired Flow Accounts</h3></div><span id="af-fw-summary">Loading…</span></div><div id="af-fw-workers" class="af-resource-list"><div><span>Loading workers…</span></div></div></div>
    <div style="margin-top:14px"><div class="af-card-head"><div><small>RECENT</small><h3 style="margin:2px 0">Flow Thumbnail Jobs</h3></div></div><div id="af-fw-jobs" class="af-resource-list"><div><span>No jobs yet.</span></div></div></div>`;host.prepend(card);
    $('af-fw-refresh').onclick=refresh;$('af-fw-generate').onclick=generate;$('af-fw-copy').onclick=copyKey;$('af-fw-delete').onclick=deleteKey;$('af-fw-test').onclick=testJob;
    const saved=localStorage.getItem('autoflowFlowPermanentPairKey')||'';if(saved)$('af-fw-pair-key').value=saved;
    refresh();clearInterval(timer);timer=setInterval(refresh,10000);
  }
  async function pairState(){try{const r=await req('/api/master-controller/flow/pairings');const st=$('af-fw-pair-state');if(r.active){st.textContent='Permanent';st.className='af-pill success';}else{st.textContent='Not generated';st.className='af-pill neutral';$('af-fw-pair-key').value='';localStorage.removeItem('autoflowFlowPermanentPairKey');}}catch{}}
  async function generate(){try{const r=await req('/api/master-controller/flow/pairings',{method:'POST',body:'{}'});const key=`${location.origin}|${r.code}`;$('af-fw-pair-key').value=key;localStorage.setItem('autoflowFlowPermanentPairKey',key);$('af-fw-pair-state').textContent='Permanent';$('af-fw-pair-state').className='af-pill success';if(window.toast)toast('Flow pairing key generated');}catch(e){if(window.toast)toast(`Pairing key failed: ${e.message}`);}}
  async function copyKey(){const v=$('af-fw-pair-key').value;if(!v)return window.toast&&toast('Generate a pairing key first');try{await navigator.clipboard.writeText(v);window.toast&&toast('Flow pairing key copied');}catch{window.toast&&toast('Could not copy key');}}
  async function deleteKey(){if(!confirm('Delete the Flow pairing key? Already paired workers stay connected.'))return;try{await req('/api/master-controller/flow/pairings',{method:'DELETE'});$('af-fw-pair-key').value='';localStorage.removeItem('autoflowFlowPermanentPairKey');await pairState();window.toast&&toast('Flow pairing key deleted');}catch(e){window.toast&&toast(`Delete failed: ${e.message}`);}}
  async function testJob(){try{const r=await req('/api/master-controller/flow/test-jobs',{method:'POST',body:'{}'});window.toast&&toast(`Flow test queued · ${String(r.job?.permanent_id||'')}`);await refresh();}catch(e){window.toast&&toast(`Test failed: ${e.message}`);}}
  async function refresh(){if(!$('af-flow-worker-pair-card'))return init();await pairState();try{const [wr,jr]=await Promise.all([req('/api/master-controller/flow/workers'),req('/api/master-controller/flow/jobs?limit=10')]);const workers=wr.workers||[];$('af-fw-summary').textContent=`${workers.filter(w=>w.online).length}/${workers.length} online`;$('af-fw-workers').innerHTML=workers.length?workers.map(w=>`<div><strong>${esc(w.name)}</strong><span>${esc(w.account_label||'Account not labeled')} · ${esc(w.chrome_profile||'Chrome profile not labeled')}<br><small>${esc(w.browser_state||'UNKNOWN')} · Active ${Number(w.active_count||0)}/1${w.last_error?` · ${esc(w.last_error)}`:''}</small></span><b class="af-pill ${w.online&&w.browser_state!=='ATTENTION'?'success':'neutral'}">${w.browser_state==='ATTENTION'?'Needs Attention':w.online?'Connected':'Offline'}</b></div>`).join(''):'<div><strong>No Flow worker paired</strong><span>Download Flow Worker, generate a key, then pair the Chrome profile containing your logged-in Thumbnail Forge tab.</span><b class="af-pill neutral">Waiting</b></div>';
      const jobs=jr.jobs||[];$('af-fw-jobs').innerHTML=jobs.length?jobs.map(j=>`<div><strong>${esc(j.permanent_id||'Flow job')}</strong><span>${esc(j.title||'Thumbnail')} · ${Number(j.result_count||0)} image(s)${j.error_text?`<br><small>${esc(j.error_text)}</small>`:''}${j.status==='DONE'&&j.result_count?`<br><a href="/api/master-controller/flow/jobs/${encodeURIComponent(j.id)}/images/1" target="_blank">Open returned image</a>`:''}</span><b class="af-pill ${j.status==='DONE'?'success':'neutral'}">${esc(j.status)}</b></div>`).join(''):'<div><span>No Flow jobs yet.</span></div>';
    }catch(e){$('af-fw-summary').textContent='Bridge unavailable';}}
  const start=()=>{init();const root=$('af-tab-resources');if(root)new MutationObserver(()=>{if($('af-res-panel-flow')&&!$('af-flow-worker-pair-card'))init();}).observe(root,{childList:true,subtree:true});};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();

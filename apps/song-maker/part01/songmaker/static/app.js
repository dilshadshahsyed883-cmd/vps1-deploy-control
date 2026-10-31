'use strict';
const $=id=>document.getElementById(id);let state={},busy=false;const selected=new Set();
const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
const notice=text=>$('notice').textContent=text;
async function api(path,options={}){const r=await fetch('/song-maker/api/'+path,{credentials:'same-origin',...options,headers:{'X-Song-Maker':'1',...options.headers}});let data=await r.json();if(!r.ok)throw Error(data.detail||'Request failed');return data;}
let pcFoldersReady=false,pcFolderEdited=false,pcFolderVersion=0,pcFolderWrites=Promise.resolve();
function setPCFolder(value){
 const select=$('pcSongFolder');
 if(value&&![...select.options].some(o=>o.value===value))select.add(new Option('Saved folder unavailable',value));
 select.value=value;
}
window.songMakerFolderSelection=()=>{
 if(!pcFoldersReady)throw Error('Wait for the VPS folder setting to load, then retry.');
 return $('pcSongFolder').value;
};
async function loadPCFolders(restoreSaved=false){
 const version=pcFolderVersion,previous=$('pcSongFolder').value;
 try{
  const folders=await api('folders');
  const saved=restoreSaved?await api('manual-folder'):null;
  const select=$('pcSongFolder'),value=(restoreSaved&&!pcFolderEdited&&version===pcFolderVersion)?String(saved.song_folder_id||''):select.value||previous;
  select.replaceChildren(new Option('Use saved delivery setting',''));
  for(const folder of folders.folders||[])select.add(new Option(folder.name,folder.id));
  setPCFolder(value);select.disabled=false;pcFoldersReady=true;
  $('pcFolderStatus').textContent=value?'Selected VPS folder retained for your account.':'Uses your existing export delivery setting.';
 }catch(e){$('pcFolderStatus').textContent='Folder settings unavailable: '+e.message;}
}
function savePCFolder(){
 pcFolderEdited=true;const version=++pcFolderVersion,value=$('pcSongFolder').value;
 $('pcFolderStatus').textContent='Saving folder selection…';
 pcFolderWrites=pcFolderWrites.catch(()=>{}).then(()=>api('manual-folder',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({song_folder_id:value})})).then(()=>{
  if(version===pcFolderVersion)$('pcFolderStatus').textContent='Folder selection saved for your account.';
 }).catch(e=>{if(version===pcFolderVersion)$('pcFolderStatus').textContent='Selection not saved: '+e.message;});
 return pcFolderWrites;
}
$('pcSongFolder').onchange=savePCFolder;
$('pcRefreshFolders').onclick=()=>loadPCFolders(!pcFoldersReady);
$('pcCreateFolder').onclick=async()=>{
 const name=prompt('VPS folder name');if(!name)return;
 const button=$('pcCreateFolder');button.disabled=true;
 try{
  const folder=await api('folders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  await loadPCFolders();if(!pcFoldersReady)throw Error('Refresh folders before selecting the new folder.');
  setPCFolder(folder.id);await savePCFolder();
 }catch(e){notice(e.message);}finally{button.disabled=false;}
};
loadPCFolders(true);
function tab(name){if(!['input','pairs','queue','preferences'].includes(name))name='queue';for(const section of ['input','pairs','queue','preferences'])$(section).hidden=section!==name;document.querySelectorAll('[data-tab]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===name)));sessionStorage.setItem('song-maker-tab',name);}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));tab(sessionStorage.getItem('song-maker-tab')||'input');
function settingsTab(name){if(!['export','video'].includes(name))name='export';$('settingsExportPanel').hidden=name!=='export';$('settingsVideoPanel').hidden=name!=='video';document.querySelectorAll('[data-settings-tab]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.settingsTab===name)));sessionStorage.setItem('song-maker-settings-tab',name);}
document.querySelectorAll('[data-settings-tab]').forEach(b=>b.onclick=()=>settingsTab(b.dataset.settingsTab));settingsTab(sessionStorage.getItem('song-maker-settings-tab')||'export');
function action(label,handler){const b=el('button',label);b.type='button';b.onclick=async()=>{b.disabled=true;try{await handler();await refresh();}catch(e){notice(e.message);}finally{b.disabled=false;}};return b;}
function shortTime(seconds){if(!Number.isFinite(seconds))return '—';const s=Math.max(0,Math.ceil(seconds));return (s>=3600?Math.floor(s/3600)+'h ':'')+Math.floor(s%3600/60)+'m '+(s%60)+'s';}
function showJobs(){const rows=$('jobs');rows.replaceChildren();const outputs=$('outputs');const signature=JSON.stringify((state.jobs||[]).filter(j=>j.valid).map(j=>[j.id,j.stage,j.remote_name,j.drive_status,j.drive_link]));const renderOutputs=outputs.dataset.signature!==signature;if(renderOutputs){outputs.dataset.signature=signature;outputs.replaceChildren();}for(const job of state.jobs||[]){const tr=el('tr'),pick=el('input'),cell=el('td');pick.type='checkbox';pick.checked=selected.has(job.id);pick.setAttribute('aria-label','Select '+job.project);pick.onchange=()=>{if(pick.checked)selected.add(job.id);else selected.delete(job.id);};cell.append(pick);tr.append(cell);for(const value of [job.project,job.image_name,job.audio_name,job.duration?job.duration.toFixed(2)+' s':'—',job.stage])tr.append(el('td',value));const p=el('td'),bar=el('progress');bar.max=100;bar.value=job.progress;p.append(bar,el('small',' '+job.progress+'%'));const eta=job.stage==='Rendering'&&Number.isFinite(job.duration)&&Number.isFinite(job.render_seconds)&&job.render_speed>0?Math.max(0,(job.duration-job.render_seconds)/job.render_speed):null;const perf=[Number.isFinite(job.render_fps)?job.render_fps.toFixed(1)+' FPS':'',Number.isFinite(job.render_speed)?job.render_speed.toFixed(2)+'×':'',Number.isFinite(job.render_elapsed)?shortTime(job.render_elapsed)+' elapsed':'',job.stage==='Rendering'?(eta!==null?shortTime(eta)+' remaining':'Estimating remaining time'):''].filter(Boolean).join(' · ');tr.append(p,el('td',perf||'—'),el('td',job.remote_name||job.local_name),el('td',(job.send_to_song_uploader?(job.uploader_status||'Preparing local handoff'):(job.drive_status||'Pending'))+(job.next_retry_at?' · '+(job.next_retry_at>Date.now()/1000?'Retry in '+shortTime(job.next_retry_at-Date.now()/1000):'Retry pending'):'')));const a=el('td');a.append(action('View Details',()=>{$('detailText').textContent=JSON.stringify(job,null,2);$('details').showModal();}));for(const [label,command] of [['Retry','retry'],['Cancel','cancel'],['Delete','delete']]){if(label==='Retry'&&!['Failed','Cancelled','Waiting for Drive','Needs Attention'].includes(job.stage))continue;if(label==='Cancel'&&['Completed','Cancelled'].includes(job.stage))continue;if(label==='Delete'&&(!['Completed','Failed','Cancelled','Waiting for Drive','Needs Attention'].includes(job.stage)||job.lane))continue;a.append(action(label,async()=>{const result=await api('jobs/'+job.id+'/'+command,{method:'POST'});if(command==='delete')notice(result.media_retained?'Project removed. Reserved media will clear after Song Uploader finishes.':'Project deleted.');}));}tr.append(a);rows.append(tr);
if(job.valid&&renderOutputs){const card=el('article');card.className='card';card.append(el('h2',job.project),el('p',(job.remote_name||job.local_name)+' · '+(job.duration||0).toFixed(2)+' s · '+((job.size||0)/1048576).toFixed(1)+' MB'),el('p',job.stage+' · '+(job.send_to_song_uploader?'Song Uploader: '+(job.uploader_status||'Pending'):'Drive: '+job.drive_status)));const video=el('video');video.controls=true;video.preload='none';video.src='/song-maker/api/jobs/'+job.id+'/video';card.append(video);const links=el('div');links.className='actions';const download=el('a','Download MP4');download.href=video.src+'?download=true';links.append(download);if(job.drive_link){const drive=el('a','Open in Google Drive');drive.href=job.drive_link;drive.target='_blank';drive.rel='noopener';links.append(drive,action('Copy Drive Link',async()=>{await navigator.clipboard.writeText(job.drive_link);notice('Drive link copied');}));}card.append(links);outputs.append(card);}}
if(!rows.children.length){const tr=el('tr'),td=el('td','Your queued projects will appear here.');td.colSpan=11;tr.append(td);rows.append(tr);}if(!outputs.children.length)outputs.append(el('p','Rendered videos will appear here.'));}
function showPairs(){const sync=state.sync||{};const metrics=$('metrics');metrics.replaceChildren();const pairs=sync.pairs||[];for(const [label,value] of [['Images',sync.images||0],['Audio',sync.audio||0],['Matched',sync.matched||0],['Waiting for Audio',pairs.filter(p=>p.stage==='Waiting for Audio').length],['Waiting for Image',pairs.filter(p=>p.stage==='Waiting for Image').length],['Conflicts',pairs.filter(p=>p.stage.startsWith('Conflict')).length],['Auto-created projects',pairs.filter(p=>p.job_id).length]])metrics.append(el('span',label+': '+value));const list=$('pairList');list.replaceChildren();for(const pair of pairs){const line=el('div');line.append(el('p',pair.key+' · '+pair.stage+(pair.job_stage?' · '+pair.job_stage:'')),el('p','Image: '+pair.images.map(x=>x.Name).join(', ')+' | Audio: '+pair.audio.map(x=>x.Name).join(', ')));if(pair.stage.startsWith('Conflict')&&pair.images.length&&pair.audio.length){const image=el('select'),audio=el('select');image.setAttribute('aria-label','Choose image for '+pair.key);audio.setAttribute('aria-label','Choose audio for '+pair.key);for(const [select,items]of [[image,pair.images],[audio,pair.audio]])for(const item of items){const option=el('option',item.Name);option.value=item.ID;select.append(option);}line.append(image,audio,action('Queue selected pair',()=>api('pairs/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:pair.key,image_id:image.value,audio_id:audio.value})})));}list.append(line);}if(!pairs.length)list.append(el('p','Run a Drive sync to detect matching filenames.'));}
async function refresh(){if(busy||document.hidden)return;try{state=await api('state');showJobs();showPairs();const sync=state.sync||{};$('syncStatus').textContent=(state.settings.auto_sync?'Automatic sync on':'Automatic sync off')+' · Last scan: '+(sync.last_sync?new Date(sync.last_sync*1000).toLocaleString():'Not yet scanned')+(sync.error?' · '+sync.error:'');$('folderInfo').textContent=state.settings.output_folder?'Drive output folder: '+state.settings.output_folder:'Administrator: select Drive folders to enable delivery.';$('syncNow').disabled=!state.sync_access;$('syncNow').title='Scan the approved Song Studio input folders';$('adminSettings').hidden=false;$('adminDriveProfile').hidden=!state.admin;$('settingsAccess').textContent=state.admin?'Shared Drive profile and your own folder/scan settings.':'You control your folders and Auto Sync; the administrator manages shared rclone credentials.';$('pauseQueue').textContent=state.queue_paused?'Resume queue':'Pause queue';$('queueStatus').textContent=state.queue_paused?'Queue paused. Active jobs finish; new work waits.':'Queue running';if(state.drive_retry_after>Date.now()/1000)$('queueStatus').textContent+=' · Drive rate limited; automatic retry in '+shortTime(state.drive_retry_after-Date.now()/1000);}catch(e){notice(e.message);}}
$('manual').onsubmit=async e=>{
 e.preventDefault();busy=true;
 const form=e.target,b=form.querySelector('button'),image=form.elements.image,audio=form.elements.audio;
 const area=$('manualUploadArea'),bar=$('manualUploadProgress'),status=$('manualUploadText');
 b.disabled=true;image.disabled=true;audio.disabled=true;area.hidden=false;bar.value=0;
 status.textContent='Starting upload…';
 try{
  const job=await window.songMakerUploadPair(image.files[0],audio.files[0],percent=>{
   bar.value=percent;
   status.textContent=percent===100?'Upload accepted · queued':percent>=99?'Uploaded · validating media…':'Uploading '+percent+'%';
  });
  notice('Queued: '+job.local_name);tab('queue');
 }catch(e){status.textContent='Upload failed: '+e.message;notice(e.message);}
 finally{busy=false;b.disabled=false;image.disabled=false;audio.disabled=false;await refresh();}
};
let imageURL,audioURL;const imageInput=$('manual').elements.image,audioInput=$('manual').elements.audio;imageInput.onchange=()=>{if(imageURL)URL.revokeObjectURL(imageURL);const file=imageInput.files[0];$('preview').hidden=!file;if(file)$('preview').src=imageURL=URL.createObjectURL(file);};audioInput.onchange=()=>{if(audioURL)URL.revokeObjectURL(audioURL);const file=audioInput.files[0];if(!file)return;const audio=new Audio(audioURL=URL.createObjectURL(file));$('audioInfo').textContent=file.name;audio.onloadedmetadata=()=>{$('audioInfo').textContent=file.name+' · '+audio.duration.toFixed(2)+' seconds';};};
$('settings').onsubmit=async e=>{e.preventDefault();try{const data=Object.fromEntries(new FormData(e.target));data.auto_sync=e.target.elements.auto_sync.checked;await api('settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});notice('Drive settings saved');await refresh();}catch(e){notice(e.message);}};
$('exportSettings').onsubmit=async e=>{e.preventDefault();try{const data=Object.fromEntries(new FormData(e.target));data.auto_input_from_audio_downloader=e.target.elements.auto_input_from_audio_downloader.checked;data.send_to_song_uploader=e.target.elements.send_to_song_uploader.checked;data.overlay_enabled=e.target.elements.overlay_enabled.checked;data.overlay_preset=e.target.elements.overlay_preset.value;data.overlay_strength=Number(e.target.elements.overlay_strength.value);data.overlay_speed=e.target.elements.overlay_speed.value;await api('export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});notice(e.submitter?.dataset.saveKind==='video'?'Video settings saved for your new projects.':'Export settings saved for your new projects.');await refresh();}catch(e){notice(e.message);}};
$('syncNow').onclick=async()=>{$('syncNow').disabled=true;try{await api('sync',{method:'POST'});notice('Drive scan requested. It continues with the browser closed.');}catch(e){notice(e.message);}finally{await refresh();}};$('closeDetails').onclick=()=>$('details').close();
function updateOverlayControls(){const form=$('exportSettings'),enabled=form.elements.overlay_enabled.checked;for(const key of ['overlay_preset','overlay_strength'])form.elements[key].disabled=!enabled;form.elements.overlay_speed.disabled=!enabled||form.elements.overlay_preset.value!=='rhythm_pulse';}
$('exportSettings').elements.overlay_enabled.addEventListener('change',updateOverlayControls);
$('exportSettings').elements.overlay_preset.addEventListener('change',updateOverlayControls);
async function loadDriveChoices(){const data=await api('drive/folders');const options=$('driveChoices');options.replaceChildren();for(const folder of data.folders||[]){const item=el('option');item.value=folder;options.append(item);}$('driveFolderStatus').textContent='Approved root: '+data.owner_root+' · '+(data.folders||[]).length+' available folders';}
$('driveRefreshFolders').onclick=async()=>{try{await loadDriveChoices();}catch(e){notice(e.message);}};
$('driveCreateFolder').onclick=async()=>{const name=prompt('New Drive folder name (inside your own approved root)');if(!name)return;const button=$('driveCreateFolder');button.disabled=true;try{const result=await api('drive/folders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});await loadDriveChoices();notice('Drive folder ready: '+result.path);}catch(e){notice(e.message);}finally{button.disabled=false;}};
async function start(){await refresh();for(const key of ['resolution','fit','quality','overlay_preset','overlay_strength','overlay_speed'])if(state.export?.[key]!==undefined)$('exportSettings').elements[key].value=state.export[key];$('exportSettings').elements.overlay_enabled.checked=state.export?.overlay_enabled===true;$('exportSettings').elements.auto_input_from_audio_downloader.checked=state.export?.auto_input_from_audio_downloader===true;$('exportSettings').elements.send_to_song_uploader.checked=state.export?.send_to_song_uploader===true;updateOverlayControls();const form=$('settings');try{if(state.admin){const profiles=await api('profiles');for(const name of profiles){const option=el('option',name);option.value=name;form.elements.remote.append(option);}}else{const option=el('option',state.settings.remote||'gdrive');option.value=state.settings.remote||'gdrive';form.elements.remote.append(option);}for(const [key,value] of Object.entries(state.settings||{})){if(form.elements[key]){if(key==='auto_sync')form.elements[key].checked=value;else form.elements[key].value=value;}}await loadDriveChoices();}catch(e){notice(e.message);}}start();setInterval(refresh,4000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});

$('selectAll').onchange=e=>{selected.clear();if(e.target.checked)for(const job of state.jobs||[])selected.add(job.id);showJobs();};
$('pauseQueue').onclick=async()=>{try{await api('queue/'+(state.queue_paused?'resume':'pause'),{method:'POST'});await refresh();}catch(e){notice(e.message);}};
for(const button of document.querySelectorAll('[data-batch]'))button.onclick=async()=>{
 if(!selected.size){notice('Select projects first');return;}
 const command=button.dataset.batch;
 if(command!=='delete'&&!confirm(command+' '+selected.size+' selected projects?'))return;
 busy=true;button.disabled=true;let count=0,retained=0;const failures=[];
 try{for(const id of [...selected]){try{const result=await api('jobs/'+id+'/'+command,{method:'POST'});if(result.media_retained)retained++;selected.delete(id);count++;}catch(e){failures.push(e.message);}}}
 finally{busy=false;button.disabled=false;await refresh();notice(count+' projects updated'+(retained?' · '+retained+' reserved media retained for Song Uploader':'')+(failures.length?' · '+failures.length+' skipped: '+[...new Set(failures)].join('; '):''));}
};

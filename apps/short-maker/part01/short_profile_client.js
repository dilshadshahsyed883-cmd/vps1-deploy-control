/* Short Maker Short Profiles PC media import + owner-profile management.
   Source files are cached for refresh-safe resumable uploads. Until the
   dedicated PKT scheduler and renderer are verified, Start stays disabled.
   No Auto Script or uploader publishing actions are invoked. */
(() => {
'use strict';
const $=id=>document.getElementById(id);
const source=$('shortProfileSource'),fallback=$('shortProfileSourceFallback');
const name=$('shortProfileName'),folder=$('shortProfileDestination');
const count=$('shortProfileDailyCount'),schedule=$('shortProfileSchedule');
const save=$('shortProfileSave'),importBtn=$('shortProfileSync');
const pause=$('shortProfilePauseImport'),resume=$('shortProfileResumeImport');
const retry=$('shortProfileRetryImport'),cancel=$('shortProfileCancelImport');
const status=$('shortProfileImportStatus'),progress=$('shortProfileImportProgress');
const rows=$('shortProfilesRows');
if(!source||!fallback||!name||!folder||!count||!schedule||!save||
   !importBtn||!pause||!resume||!retry||!cancel||!status||!progress||!rows)return;
const BASE='/short-maker/api/short-profile-media/sessions';
const PROFILES='/short-maker/api/short-profiles';
const DB='short-maker-short-profile-media-v1',CHUNK=4*1024*1024,WORKERS=2;
const accepted=/\.(mp3|wav|m4a|aac|flac|ogg|opus|jpg|jpeg|png|webp|jfif|heic|heif)$/i;
let session=null,busy=false,paused=false,stop=false,verifiedReceipt='',editing=null;
let files=[],fileErrors=new Map();
function message(value){status.textContent=value;}
function selectFiles(){
 const selected=source.files&&source.files.length?[...source.files]:[...(fallback.files||[])];
 return selected.filter(f=>accepted.test(f.name));
}
function controls(){
 const selected=selectFiles();
 importBtn.disabled=busy||!!session||!selected.length||
      !selected.some(f=>/\.(mp3|wav|m4a|aac|flac|ogg|opus)$/i.test(f.name))||
      !selected.some(f=>/\.(jpe?g|png|webp|jfif|heic|heif)$/i.test(f.name));
 save.disabled=busy||!verifiedReceipt;
 pause.disabled=!busy||paused;
 resume.disabled=busy||!session;
 retry.disabled=busy||!session;
 cancel.disabled=!session;
}
function openDb(){return new Promise((resolve,reject)=>{
 const req=indexedDB.open(DB,1);
 req.onupgradeneeded=()=>{for(const store of ['meta','files'])
  if(!req.result.objectStoreNames.contains(store))req.result.createObjectStore(store);};
 req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);
});}
async function idb(store,action,key,value){
 const db=await openDb();
 try{return await new Promise((resolve,reject)=>{
  const tx=db.transaction(store,action==='get'?'readonly':'readwrite'),s=tx.objectStore(store);
  let out;
  if(action==='get'){const req=s.get(key);req.onsuccess=()=>{out=req.result};req.onerror=()=>reject(req.error);}
  if(action==='put')s.put(value,key);
  if(action==='del')s.delete(key);
  tx.oncomplete=()=>resolve(out);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
 });}finally{db.close();}
}
async function api(url,options){
 const response=await fetch(url,{cache:'no-store',...options});
 let json;try{json=await response.json();}catch{throw Error('Invalid server response');}
 if(!response.ok)throw Error(json.detail||('HTTP '+response.status));
 return json;
}
function key(id,i){return id+':'+i;}
function renderProgress(snapshot){
 progress.replaceChildren();
 const total=snapshot.files.length;
 const complete=snapshot.files.filter(f=>f.uploaded>=f.size).length;
 const failures=snapshot.files.filter(f=>fileErrors.has(f.index)).length;
 const summary=document.createElement('p');summary.className='hint';
 summary.textContent=snapshot.status+' · '+snapshot.uploaded_bytes+'/'+snapshot.total_bytes+
     ' bytes · '+complete+' transferred · '+failures+' failed · '+Math.max(0,total-complete-failures)+' waiting';
 progress.appendChild(summary);
 for(const f of snapshot.files){
  const line=document.createElement('div');line.className='song-item';
  const value=document.createElement('span');
  const percent=f.size?Math.floor(100*f.uploaded/f.size):0;
  value.textContent=f.name+' · '+f.kind+' · '+percent+'% ('+f.uploaded+'/'+f.size+
      ')'+(fileErrors.has(f.index)?' · '+fileErrors.get(f.index):'');
  const bar=document.createElement('div');bar.className='progress';bar.style.flex='0 0 100px';
  const fill=document.createElement('span');fill.style.width=percent+'%';bar.appendChild(fill);
  line.append(value,bar);progress.appendChild(line);
 }
}
async function snapshot(){
 const data=await api(BASE+'/'+session.id);
 renderProgress(data);
 return data;
}
async function cleanLocal(id,number){
 for(let i=0;i<number;i++)await idb('files','del',key(id,i));
 await idb('meta','del','current');session=null;files=[];controls();
}
async function transfer(i){
 const part=files[i];
 if(!part?.blob)throw Error('Recovery file missing; choose the original folder again');
 let state=await snapshot(),offset=state.files[i]?.uploaded;
 if(!Number.isSafeInteger(offset)||state.files[i].size!==part.blob.size)
  throw Error('Source file and persisted session do not match');
 while(offset<part.blob.size&&!paused&&!stop){
  const bytes=part.blob.slice(offset,Math.min(part.blob.size,offset+CHUNK));
  try{
   const resp=await api(BASE+'/'+session.id+'/files/'+i,{
    method:'PUT',headers:{'x-upload-offset':String(offset)},body:bytes});
   if(resp.uploaded<=offset)throw Error('Source upload did not advance');
   offset=resp.uploaded;
  }catch(error){
   const latest=await snapshot(),recorded=latest.files[i]?.uploaded||0;
   if(recorded>offset){offset=recorded;continue;}
   throw error;
  }
  state.files[i].uploaded=offset;
  state.uploaded_bytes=state.files.reduce((sum,f)=>sum+f.uploaded,0);
  renderProgress(state);
 }
}
async function run(){
 if(!session||busy)return;
 busy=true;stop=false;paused=false;controls();fileErrors.clear();
 try{
  const current=await snapshot();
  if(current.status==='completed'){verifiedReceipt=session.id;
   await idb('meta','put','verified',verifiedReceipt);
   await cleanLocal(session.id,session.fileCount);message('Existing verified import restored.');return;}
  if(current.status==='cancelled')throw Error('Cancelled source import cannot resume');
  if(current.status==='paused')await api(BASE+'/'+session.id+'/resume',{method:'POST'});
  files=[];
  for(let i=0;i<current.files.length;i++){
   const item=await idb('files','get',key(session.id,i));
   if(!item?.blob)throw Error('Recovery files missing. Re-select original PC folder; no VPS data deleted.');
   files.push(item);
  }
  let index=0;
  async function worker(){
   while(index<files.length&&!stop&&!paused){
    const i=index++;
    try{await transfer(i);}
    catch(e){fileErrors.set(i,String(e.message||e));throw e;}
   }
  }
  const outcome=await Promise.allSettled(Array.from({length:Math.min(WORKERS,files.length)},worker));
  if(stop){
   await api(BASE+'/'+session.id,{method:'DELETE'});
   const old=session;await cleanLocal(old.id,old.fileCount);
   message('Cancelled. Unverified staging media cleaned.');return;
  }
  if(paused){
   const state=await api(BASE+'/'+session.id+'/pause',{method:'POST'});
   renderProgress(state);
   await idb('meta','put','current',{...session,paused:true});
   message('Paused after in-flight chunks.');return;
  }
  if(outcome.some(x=>x.status==='rejected')){
   await snapshot();throw Error('Some uploads failed. Retry resumes from verified offsets');
  }
  message('Checking audio and pictures on VPS…');
  const finished=await api(BASE+'/'+session.id+'/finalize',{method:'POST'});
  renderProgress(finished);
  verifiedReceipt=session.id;
  await idb('meta','put','verified',verifiedReceipt);
  const old=session;await cleanLocal(old.id,old.fileCount);
  message('Verified on VPS: '+finished.verified_audio_count+' voice and '+
          finished.verified_picture_count+' pictures. Save your profile.');
 }catch(e){
  message('Import not completed: '+e.message+'. Retry or resume safely.');
  if(session)await idb('meta','put','current',{...session,paused:true});
 }finally{busy=false;controls();}
}
async function beginImport(){
 const selected=selectFiles();
 if(!selected.length||selected.length!==([...source.files||[],...fallback.files||[]].length))
  throw Error('Select only supported voice and picture files');
 if(selected.some(f=>f.size<128))throw Error('A source file is too small to verify');
 if(!selected.some(f=>/\.(mp3|wav|m4a|aac|flac|ogg|opus)$/i.test(f.name))||
    !selected.some(f=>/\.(jpg|jpeg|png|webp|jfif|heic|heif)$/i.test(f.name)))
  throw Error('Folder must contain both voice audio and pictures');
 const requestId=crypto.randomUUID();
 const state=await api(BASE,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({client_request_id:requestId,files:selected.map(f=>({name:f.name,size:f.size}))})});
 session={id:state.session_id,fileCount:selected.length,paused:false};
 verifiedReceipt='';await idb('meta','del','verified');
 await idb('meta','put','current',session);
 for(let i=0;i<selected.length;i++){
  await idb('files','put',key(session.id,i),{blob:selected[i],name:selected[i].name});
  message('Caching source media '+(i+1)+'/'+selected.length+' for refresh recovery…');
 }
 renderProgress(state);controls();
}
function inputChanged(){
 verifiedReceipt='';idb('meta','del','verified').catch(()=>{});
 const selected=selectFiles(),voice=selected.filter(f=>/\.(mp3|wav|m4a|aac|flac|ogg|opus)$/i.test(f.name)).length;
 message(voice+' voice + '+(selected.length-voice)+' picture files selected locally. Import to VPS before saving.');
 controls();
}
source.addEventListener('change',()=>{
 if(source.files?.length)fallback.value='';
 inputChanged();
});
fallback.addEventListener('change',()=>{
 if(fallback.files?.length)source.value='';
 inputChanged();
});
if('webkitdirectory' in document.createElement('input'))fallback.hidden=true;
else{source.hidden=true;fallback.hidden=false;}
importBtn.addEventListener('click',async()=>{
 if(busy||session)return;
 try{importBtn.disabled=true;await beginImport();await run();}
 catch(e){message('Import cannot begin: '+e.message);controls();}
});
pause.addEventListener('click',()=>{paused=true;pause.disabled=true;message('Pausing after in-flight chunks…');});
resume.addEventListener('click',run);
retry.addEventListener('click',run);
cancel.addEventListener('click',async()=>{
 if(!session||!confirm('Cancel unfinished import and remove its VPS staging media?'))return;
 if(busy){stop=true;message('Cancelling after active chunks finish…');return;}
 try{await api(BASE+'/'+session.id,{method:'DELETE'});
  const old=session;await cleanLocal(old.id,old.fileCount);
  message('Unfinished import cancelled.');}
 catch(e){message(e.message);}
});
async function listProfiles(){
 try{
  const data=await api(PROFILES);
  rows.replaceChildren();
  if(!data.profiles?.length){rows.textContent='No Short Profiles saved yet.';return;}
  for(const profile of data.profiles){
   const item=document.createElement('div');item.className='song-item';
   const label=document.createElement('span');
   label.textContent=profile.name+' · '+profile.output_folder+' · '+profile.videos_per_day+
     '/day · '+(profile.enabled?'Enabled':'Stopped')+' · Daily generation not activated';
   item.appendChild(label);
   const actions=document.createElement('div');actions.className='queue-actions';
   function action(title,handler,disabled=false){
    const btn=document.createElement('button');btn.type='button';btn.className='secondary';
    btn.textContent=title;btn.disabled=disabled;
    btn.addEventListener('click',async()=>{btn.disabled=true;try{await handler();}catch(e){message(e.message);}finally{btn.disabled=disabled;}});
    actions.appendChild(btn);return btn;
   }
   action('Start (pending)',async()=>{},true);
   action('Stop',async()=>{await api(PROFILES+'/'+profile.id+'/stop',{method:'POST'});await listProfiles();});
   action('Edit',async()=>{
    editing=profile.id;name.value=profile.name;folder.value=profile.output_folder;
    count.value=profile.videos_per_day;schedule.value=profile.schedule_hhmm;
    verifiedReceipt=profile.source_import_id;
    source.value='';fallback.value='';
    message('Editing '+profile.name+'; change source folder only if you import a new verified copy.');
    controls();name.focus();
   });
   action('Delete',async()=>{
    if(!confirm('Delete Short Profile "'+profile.name+'"? Source media, MP4s and history are preserved.'))return;
    const confirmName=prompt('Type the exact profile name to delete:');
    if(confirmName!==profile.name)return;
    await api(PROFILES+'/'+profile.id,{method:'DELETE',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({confirm_name:confirmName})});
    if(editing===profile.id)resetForm();
    await listProfiles();
   });
   action('History',async()=>{
    const result=await api(PROFILES+'/'+profile.id+'/history');
    const detail=item.querySelector('.short-profile-history')||document.createElement('div');
    detail.className='short-profile-history';
    detail.textContent=result.history.length?result.history.map(x=>x.pkt_date+' · '+x.status).join(' | '):
                       'No daily generation history yet.';
    item.appendChild(detail);
   });
   item.appendChild(actions);rows.appendChild(item);
  }
 }catch(e){rows.textContent='Could not load owned profiles: '+e.message;}
}
function resetForm(){
 editing=null;name.value='';folder.value='';count.value='';schedule.value='00:00';
 verifiedReceipt='';source.value='';fallback.value='';
 idb('meta','del','verified').catch(()=>{});
 controls();
}
save.addEventListener('click',async()=>{
 if(busy||!verifiedReceipt)return;
 const target={name:name.value,source_import_id:verifiedReceipt,output_folder:folder.value,
   videos_per_day:Number(count.value),schedule_hhmm:schedule.value};
 if(!Number.isSafeInteger(target.videos_per_day)||target.videos_per_day<1){
  message('Videos per day must be a positive whole number.');return;
 }
 const url=editing?PROFILES+'/'+editing:PROFILES;
 try{save.disabled=true;const result=await api(url,{method:editing?'PATCH':'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(target)});
  message('Profile '+result.name+' saved with verified VPS media. Daily scheduler is not yet activated.');
  resetForm();await listProfiles();
 }catch(e){message('Profile save blocked: '+e.message);}
 finally{controls();}
});
$('shortProfileNew')?.addEventListener('click',()=>{resetForm();message('Create a profile using your PC folder and verified VPS media.');});
(async()=>{
 await listProfiles();
 try{
  const stored=await idb('meta','get','current');
  if(stored?.id){
   session=stored;
   const snap=await snapshot();
   if(snap.status==='completed'){
    verifiedReceipt=stored.id;await idb('meta','put','verified',stored.id);
    await cleanLocal(stored.id,stored.fileCount);
   }else if(!stored.paused&&snap.status!=='cancelled')await run();
   else message('Paused import recovered. Resume or cancel before importing another folder.');
  }else{
   const receipt=await idb('meta','get','verified');
   if(receipt){
    const snap=await api(BASE+'/'+receipt);
    if(snap.status==='completed'){verifiedReceipt=receipt;
     message('Previously verified import recovered. Ready to save a Short Profile.');}
   }
  }
 }catch(e){message('Recovery unavailable: '+e.message+'. Re-select the PC folder if necessary.');}
 finally{controls();}
})();
})();

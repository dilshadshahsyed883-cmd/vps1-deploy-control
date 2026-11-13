/* SHORT_MAKER_REFRESH_SAFE_UPLOAD_V1
   Persist selected files in IndexedDB before creating the VPS job.
   Resume server-confirmed byte ranges after reload, tab navigation, and errors.
   An already finalized render runs entirely on the VPS. */
const SHORT_UPLOAD_DB='short-maker-resume-v1',SHORT_CHUNK_SIZE=4*1024*1024;
const progressInfo=document.getElementById('progressDetail');
const progressTrack=document.getElementById('progressTrack');
const renderProgressTrack=document.getElementById('renderProgressTrack');
const renderProgressBar=document.getElementById('renderProgressBar');
const uploadPctLabel=document.getElementById('uploadPctLabel');
const renderPctLabel=document.getElementById('renderPctLabel');
const discardUpload=document.getElementById('discardUpload');
const resumeUpload=document.getElementById('resumeUpload');
const pendingUploadStatus=document.getElementById('pendingUploadStatus');
let trackedShortJob=null, activeShortUpload=null,shortSubmitBusy=false;

function showShortProgress(uploadPercent,renderPercent,stage){
 const up=Math.min(100,Math.max(0,Number(uploadPercent)||0));
 const rp=Math.min(100,Math.max(0,Number(renderPercent)||0));
 bar.style.width=up+'%';renderProgressBar.style.width=rp+'%';
 progressTrack.setAttribute('aria-valuenow',String(Math.round(up)));
 renderProgressTrack.setAttribute('aria-valuenow',String(Math.round(rp)));
 uploadPctLabel.textContent=Math.round(up)+'%';renderPctLabel.textContent=Math.round(rp)+'%';
 progressInfo.textContent=stage==='upload'?'Uploading · '+Math.round(up)+'%':'VPS rendering · '+Math.round(rp)+'%';
}
function shortDB(){return new Promise((resolve,reject)=>{
 const req=indexedDB.open(SHORT_UPLOAD_DB,2);
 req.onupgradeneeded=()=>{
  for(const store of ['meta','files','pending']){
   if(!req.result.objectStoreNames.contains(store))req.result.createObjectStore(store);
  }
 };
 req.onsuccess=()=>resolve(req.result);
 req.onerror=()=>reject(req.error||Error('Browser storage unavailable.'));
});}
async function shortDBRead(store,key){
 const db=await shortDB();
 return new Promise((resolve,reject)=>{
  const tx=db.transaction(store,'readonly'),req=tx.objectStore(store).get(key);
  req.onsuccess=()=>resolve(req.result);
  req.onerror=()=>reject(req.error);
  tx.oncomplete=()=>db.close();tx.onerror=()=>db.close();
 });}
async function shortDBSave(meta,files){
 const db=await shortDB();
 return new Promise((resolve,reject)=>{
  const tx=db.transaction(['meta','files'],'readwrite');
  if(files){
   tx.objectStore('files').clear();
   for(const f of files)tx.objectStore('files').put(f.blob,f.kind+':'+f.index);
  }
  tx.objectStore('meta').put(meta,'active');
  tx.oncomplete=()=>{db.close();resolve();};
  tx.onerror=()=>{db.close();reject(tx.error||Error('Not enough browser storage to protect this upload.'));};
  tx.onabort=()=>{db.close();reject(tx.error||Error('Not enough browser storage to protect this upload.'));};
 });}
// Additional submissions are durably staged separately from the single active
// resumable transfer. They cannot overwrite its byte offsets or source Blobs.
async function shortDBStageUpload(meta,blobs){
 const db=await shortDB();
 return new Promise((resolve,reject)=>{
  const tx=db.transaction('pending','readwrite');
  const key=String(Date.now()).padStart(13,'0')+':'+meta.client_request_id;
  tx.objectStore('pending').put({meta,blobs},key);
  tx.oncomplete=()=>{db.close();resolve(key);};
  tx.onerror=()=>{db.close();reject(tx.error||Error('Browser upload queue could not be saved.'));};
  tx.onabort=()=>{db.close();reject(tx.error||Error('Browser upload queue storage is full.'));};
 });
}
async function shortDBPendingCount(){
 const db=await shortDB();
 return new Promise((resolve,reject)=>{
  const tx=db.transaction('pending','readonly'),req=tx.objectStore('pending').count();
  req.onsuccess=()=>resolve(req.result);
  req.onerror=()=>reject(req.error);
  tx.oncomplete=()=>db.close();tx.onerror=()=>db.close();
 });
}
async function shortDBNextPending(){
 const db=await shortDB();
 return new Promise((resolve,reject)=>{
  const tx=db.transaction('pending','readonly');
  const req=tx.objectStore('pending').openCursor();
  req.onsuccess=()=>resolve(req.result?{key:req.result.key,record:req.result.value}:null);
  req.onerror=()=>reject(req.error);
  tx.oncomplete=()=>db.close();tx.onerror=()=>db.close();
 });
}
async function shortDBActivatePending(next){
 const db=await shortDB();
 return new Promise((resolve,reject)=>{
  const tx=db.transaction(['meta','files','pending'],'readwrite');
  let activated=null;
  const metaStore=tx.objectStore('meta'),fileStore=tx.objectStore('files');
  const existing=metaStore.get('active');
  existing.onsuccess=()=>{
   if(existing.result){tx.abort();return;}
   const request=tx.objectStore('pending').get(next.key);
   request.onsuccess=()=>{
    const entry=request.result;
    if(!entry){tx.abort();return;}
    fileStore.clear();
    for(const f of entry.blobs)fileStore.put(f.blob,f.kind+':'+f.index);
    metaStore.put(entry.meta,'active');
    tx.objectStore('pending').delete(next.key);
    activated=entry.meta;
   };
  };
  tx.oncomplete=()=>{db.close();resolve(activated);};
  tx.onerror=()=>{db.close();reject(tx.error||Error('Cannot activate saved upload.'));};
  tx.onabort=()=>{db.close();reject(tx.error||Error('Active upload changed. Reload to resume.'));};
 });
}
async function shortRefreshUploadControls(){
 if(!shortSubmitBusy)render.disabled=false;
 render.textContent=activeShortUpload?'Add Another Job to Queue':'Start Rendering';
 try{
  const count=await shortDBPendingCount();
  pendingUploadStatus.textContent=count?
   count+' additional project(s) saved in browser upload queue · uploads run one at a time':
   (activeShortUpload?'Current upload in progress · you can add another project':'New projects join the VPS render queue automatically');
 }catch(error){pendingUploadStatus.textContent='Browser upload queue temporarily unavailable.';}
}
function shortUploadError(error){
 render.disabled=false;
 statusEl.textContent='Upload paused: '+error.message+' — use Resume paused upload to retry.';
 discardUpload.hidden=false;
 resumeUpload.hidden=false;
 shortRefreshUploadControls();
}
async function shortDrainQueuedUploads(){
 return withShortUploadLock(async()=>{
  while(true){
   let meta=await shortDBRead('meta','active');
   if(!meta){
    const next=await shortDBNextPending();
    if(!next)break;
    meta=await shortDBActivatePending(next);
   }
   await shortResumeInternal(meta);
  }
 });
}
function shortKickUploadDrain(){
 const previous=activeShortUpload;
 const run=()=>shortDrainQueuedUploads().catch(shortUploadError);
 if(previous)previous.then(run,()=>{});
 else run();
}
async function shortDBDelete(store,key){
 const db=await shortDB();
 return new Promise((resolve,reject)=>{
  const tx=db.transaction(store,'readwrite');
  tx.objectStore(store).delete(key);
  tx.oncomplete=()=>{db.close();resolve();};
  tx.onerror=()=>{db.close();reject(tx.error);};
 });}
async function shortDBClear(){
 const db=await shortDB();
 return new Promise((resolve,reject)=>{
  const tx=db.transaction(['meta','files'],'readwrite');
  tx.objectStore('meta').delete('active');
  tx.objectStore('files').clear();
  tx.oncomplete=()=>{db.close();resolve();};
  tx.onerror=()=>{db.close();reject(tx.error);};
 });}
async function shortJson(response,fallback){
 const data=await response.json().catch(()=>({}));
 if(!response.ok)throw Error(data.detail||fallback||'Request failed.');
 return data;
}
const SHORT_IMAGE_EXTENSION=/\.(?:png|jpg|jpeg|webp|gif|bmp|tif|tiff|jfif|heic|heif)$/i;
function shortSupportedImageName(name){return SHORT_IMAGE_EXTENSION.test(String(name||''));}
function shortSupportedImageSelection(files){
 const accepted=[],skipped=[];
 for(const file of Array.from(files||[])){
  (shortSupportedImageName(file.name)?accepted:skipped).push(file);
 }
 return {accepted,skipped};
}
images.addEventListener('change',()=>{
 const selected=shortSupportedImageSelection(images.files);
 imageCount.textContent=selected.accepted.length+' image(s) selected'+
  (selected.skipped.length?' · '+selected.skipped.length+' non-image file(s) skipped':'');
});
function shortSettings(){
 const cfg={};
 for(const name of ['video_duration','image_duration','output_count','order','fit','motion','fps','crf','base_name','input_mode','drive_output_path']){
  cfg[name]=form.elements[name].value;
 }
 const paths=drivePaths();
 cfg.drive_input_path=inputMode.value==='drive'?(paths[0]||''):'';
 cfg.drive_input_paths_json=inputMode.value==='drive'?JSON.stringify(paths):'';
 cfg.available_to_short_uploader=uploaderDestination.checked;
 cfg.upload_to_drive=driveDestination.checked;
 cfg.short_vps_folder=shortFolderSelect.value;
 return cfg;
}
function uploadRatio(uploaded,total){return total?Math.min(100,Math.floor(100*uploaded/total)):100;}
async function pollShortJob(id){
 if(trackedShortJob!==id)return;
 try{
  const state=await shortJson(await fetch('/short-maker/api/jobs/'+encodeURIComponent(id),{cache:'no-store'}),'Status check failed');
  const uploading=state.status==='uploading';
  showShortProgress(uploading?Number(state.upload_progress||state.progress||0):100,uploading?0:Number(state.progress||0),uploading?'upload':'render');
  statusEl.textContent=state.message||state.status||'Working...';
  if(state.status==='completed'){
   render.disabled=false;outputs.replaceChildren();
   for(const output of state.outputs||[]){
    if(!output.local_available||!output.url)continue;
    const link=document.createElement('a'),label=document.createElement('span'),action=document.createElement('span');
    link.href=output.url;label.textContent=output.name;action.textContent='Download';
    link.append(label,action);outputs.appendChild(link);
   }
   return;
  }
  if(state.status==='failed'){
   render.disabled=false;
   statusEl.textContent='Failed: '+(state.error||state.message||'Unknown error');
   return;
  }
  setTimeout(()=>pollShortJob(id),1500);
 }catch(err){
  statusEl.textContent='Status temporarily unavailable; retrying...';
  setTimeout(()=>pollShortJob(id),2500);
 }
}
// A rejected session can leave a mixed-folder snapshot in browser storage.
// Reindex it atomically before creating a VPS job; preserve audio and originals.
async function shortNormalizePendingImages(meta){
 if(meta.job_id||meta.config.input_mode!=='upload')return meta;
 const original=meta.files.images;
 const accepted=original.map((entry,index)=>({entry,index}))
  .filter(({entry})=>shortSupportedImageName(entry.name));
 if(accepted.length===original.length)return meta;
 if(!accepted.length)throw Error('Selected folder has no supported images. Discard paused upload and choose an image folder.');
 const persisted=[];
 for(const item of accepted){
  const blob=await shortDBRead('files','images:'+item.index);
  if(!blob||blob.size!==item.entry.size)throw Error('Saved PC image is missing. Discard paused upload and reselect images.');
  persisted.push({kind:'images',index:persisted.length,blob});
 }
 for(let index=0;index<meta.files.audio.length;index++){
  const info=meta.files.audio[index];
  const blob=await shortDBRead('files','audio:'+index);
  if(!blob||blob.size!==info.size)throw Error('Saved PC audio is missing. Discard paused upload and reselect audio.');
  persisted.push({kind:'audio',index,blob});
 }
 const next={...meta,client_request_id:crypto.randomUUID(),
  files:{images:accepted.map(({entry})=>entry),audio:meta.files.audio}};
 await shortDBSave(next,persisted);
 return next;
}
async function shortResumeInternal(meta){
 meta=await shortNormalizePendingImages(meta);
 discardUpload.hidden=true;resumeUpload.hidden=true;
 if(!meta.client_request_id&&!meta.job_id){
  meta.client_request_id=crypto.randomUUID();
  await shortDBSave(meta,null);
 }
 if(!meta.job_id){
  statusEl.textContent='Creating resumable upload job on VPS...';
  const begun=await shortJson(await fetch('/short-maker/api/upload-sessions',{
   method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({config:meta.config,files:meta.files,client_request_id:meta.client_request_id})
  }),'Could not create upload job');
  meta.job_id=begun.job_id;
  await shortDBSave(meta,null);
 }
 trackedShortJob=meta.job_id;
 sessionStorage.setItem('shortMakerLastJob',meta.job_id);
 const status=await shortJson(await fetch('/short-maker/api/upload-sessions/'+meta.job_id,{cache:'no-store'}),'Could not read upload status');
 if(status.status!=='uploading'){
  await shortDBClear();
  render.disabled=false;pollShortJob(meta.job_id);loadQueue();return;
 }
 let sent=Number(status.uploaded_bytes||0),total=Number(status.total_bytes||0);
 showShortProgress(uploadRatio(sent,total),0,'upload');
 for(const kind of ['images','audio']){
  for(let i=0;i<meta.files[kind].length;i++){
   const fileInfo=meta.files[kind][i],key=kind+':'+i;
   let offset=Number(status.files[kind][i].uploaded||0);
   if(offset===fileInfo.size){
    await shortDBDelete('files',key);continue;
   }
   const file=await shortDBRead('files',key);
   if(!file||file.size!==fileInfo.size){
    throw Error('Saved PC source is unavailable. Re-select original files before restarting.');
   }
   while(offset<fileInfo.size){
    const chunk=file.slice(offset,Math.min(offset+SHORT_CHUNK_SIZE,fileInfo.size));
    const response=await fetch('/short-maker/api/upload-sessions/'+meta.job_id+'/files/'+kind+'/'+i,{
     method:'PUT',headers:{'X-Upload-Offset':String(offset)},body:chunk
    });
    if(response.status===409){
     const fresh=await shortJson(await fetch('/short-maker/api/upload-sessions/'+meta.job_id,{cache:'no-store'}),'Could not reconcile upload');
     const newOffset=Number(fresh.files[kind][i].uploaded||0);
     if(newOffset===offset)throw Error('Upload conflict. Press Start Rendering to retry.');
     offset=newOffset;sent=Number(fresh.uploaded_bytes||0);
     continue;
    }
    const ack=await shortJson(response,'Chunk upload failed');
    const newOffset=Number(ack.uploaded);
    if(newOffset<=offset)throw Error('Upload did not advance.');
    sent+=newOffset-offset;offset=newOffset;
    const pct=uploadRatio(sent,total);
    showShortProgress(pct,0,'upload');
    statusEl.textContent='Uploading '+kind+' '+(i+1)+'/'+meta.files[kind].length+' · '+pct+'%';
   }
   await shortDBDelete('files',key);
  }
 }
 statusEl.textContent='All source files uploaded. Queuing VPS render...';
 await shortJson(await fetch('/short-maker/api/upload-sessions/'+meta.job_id+'/finalize',{method:'POST'}),'Could not finalize job');
 await shortDBClear();discardUpload.hidden=true;resumeUpload.hidden=true;
 render.disabled=false;outputs.replaceChildren();
 statusEl.textContent='Job '+meta.job_id.slice(0,8)+' queued; rendering continues on VPS.';
 showShortProgress(100,0,'render');
 trackedShortJob=meta.job_id;pollShortJob(meta.job_id);loadQueue();
}
function withShortUploadLock(run){
 if(activeShortUpload)return activeShortUpload;
 activeShortUpload=(navigator.locks&&navigator.locks.request
  ?navigator.locks.request('short-maker-one-upload',run)
  :run()).finally(()=>{activeShortUpload=null;shortRefreshUploadControls();});
 shortRefreshUploadControls();
 return activeShortUpload;
}
function shortResume(meta){
 return withShortUploadLock(async()=>{
  const pending=await shortDBRead('meta','active');
  // Another tab may have already completed the upload while we waited for lock.
  if(!pending){
   if(meta.job_id){trackedShortJob=meta.job_id;pollShortJob(meta.job_id);}
   return;
  }
  return shortResumeInternal(pending);
 });
}
async function shortSubmitOrResume(){
 const paths=drivePaths();
 const selectedImages=shortSupportedImageSelection(images.files);
 const noNewInputs=inputMode.value==='upload'&&!selectedImages.accepted.length&&!selectedAudioFiles.length;
 if(noNewInputs){
  const active=await shortDBRead('meta','active');
  const waiting=await shortDBPendingCount();
  if(active||waiting){shortKickUploadDrain();return;}
 }
 if(inputMode.value==='upload'&&!selectedImages.accepted.length)throw Error('Select a folder containing supported images (JPG, PNG, WebP, etc.).');
 if(inputMode.value==='drive'&&!paths.length)throw Error('Add at least one Google Drive folder.');
 if(inputMode.value==='drive'&&new Set(paths).size!==paths.length)throw Error('Remove duplicate Drive folder links.');
 const files={images:[],audio:[]},blobs=[];
 if(inputMode.value==='upload'){
  selectedImages.accepted.forEach((file,index)=>{
   files.images.push({name:file.webkitRelativePath||file.name,size:file.size});
   blobs.push({kind:'images',index,blob:file});
  });
 }
 selectedAudioFiles.forEach((file,index)=>{
  files.audio.push({name:file.name,size:file.size});
  blobs.push({kind:'audio',index,blob:file});
 });
 const meta={job_id:null,client_request_id:crypto.randomUUID(),files,config:shortSettings()};
 // Persist the entire second project before clearing its form selections.
 // The active upload, if any, remains untouched in separate IndexedDB stores.
 await shortDBStageUpload(meta,blobs);
 images.value='';audio.value='';selectedAudioFiles.length=0;
 imageCount.textContent='0 images selected';renderSelectedAudio();
 if(!activeShortUpload)statusEl.textContent='Project saved in upload queue; starting VPS transfer.';
 shortKickUploadDrain();
}
form.addEventListener('submit',async event=>{
 event.preventDefault();
 if(shortSubmitBusy)return;
 shortSubmitBusy=true;
 render.disabled=true;
 try{await shortSubmitOrResume();}
 catch(error){statusEl.textContent='Could not queue project: '+error.message;}
 finally{shortSubmitBusy=false;shortRefreshUploadControls();}
});
resumeUpload.addEventListener('click',()=>{
 resumeUpload.hidden=true;
 shortKickUploadDrain();
});
(async()=>{
 try{
  const pending=await shortDBRead('meta','active');
  if(pending){
   statusEl.textContent='Recovering interrupted file upload...';
   discardUpload.hidden=false;
  }else{
   const lastJob=sessionStorage.getItem('shortMakerLastJob');
   if(lastJob){trackedShortJob=lastJob;pollShortJob(lastJob);}
  }
  await shortRefreshUploadControls();
  shortKickUploadDrain();
 }catch(error){shortUploadError(error);}
})();

discardUpload.addEventListener('click',async()=>{
 if(activeShortUpload)return alert('Upload is active. Discard is available once it pauses.');
 if(!confirm('Discard this incomplete upload and delete its staged VPS files?'))return;
 try{
  const meta=await shortDBRead('meta','active');
  if(meta?.job_id){
   const r=await fetch('/short-maker/api/upload-sessions/'+meta.job_id,{method:'DELETE'});
   if(!r.ok&&r.status!==404)await shortJson(r,'Could not discard upload');
  }
  await shortDBClear();
  discardUpload.hidden=true;resumeUpload.hidden=true;render.disabled=false;trackedShortJob=null;
  sessionStorage.removeItem('shortMakerLastJob');
  showShortProgress(0,0,'upload');
  statusEl.textContent='Incomplete upload discarded. Saved next projects remain queued.';
  loadQueue();shortRefreshUploadControls();shortKickUploadDrain();
 }catch(error){statusEl.textContent='Discard failed: '+error.message;}
});

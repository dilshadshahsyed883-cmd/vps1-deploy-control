/* SHORT_MAKER_REFRESH_SAFE_UPLOAD_V1
   Persist selected files in IndexedDB before creating the VPS job.
   Resume server-confirmed byte ranges after reload, tab navigation, and errors.
   An already finalized render runs entirely on the VPS. */
const SHORT_UPLOAD_DB='short-maker-resume-v1',SHORT_CHUNK_SIZE=4*1024*1024;
const progressInfo=document.getElementById('progressDetail');
const progressTrack=document.getElementById('progressTrack');
let trackedShortJob=null, activeShortUpload=null;

function showShortProgress(uploadPercent,renderPercent,stage){
 const pct=Math.min(100,Math.max(0,stage==='upload'?uploadPercent:renderPercent));
 bar.style.width=pct+'%';
 progressTrack.setAttribute('aria-valuenow',String(Math.round(pct)));
 progressTrack.setAttribute('aria-label',stage==='upload'?'Uploading selected files':'Rendering on VPS');
 progressInfo.textContent='Upload: '+Math.round(uploadPercent)+'% · Rendering: '+Math.round(renderPercent)+'%';
}
function shortDB(){return new Promise((resolve,reject)=>{
 const req=indexedDB.open(SHORT_UPLOAD_DB,1);
 req.onupgradeneeded=()=>{
  req.result.createObjectStore('meta');
  req.result.createObjectStore('files');
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
  const state=await shortJson(await fetch('api/jobs/'+encodeURIComponent(id),{cache:'no-store'}),'Status check failed');
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
async function shortResumeInternal(meta){
 render.disabled=true;
 if(!meta.job_id){
  statusEl.textContent='Creating resumable upload job on VPS...';
  const begun=await shortJson(await fetch('api/upload-sessions',{
   method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({config:meta.config,files:meta.files})
  }),'Could not create upload job');
  meta.job_id=begun.job_id;
  await shortDBSave(meta,null);
 }
 trackedShortJob=meta.job_id;
 sessionStorage.setItem('shortMakerLastJob',meta.job_id);
 const status=await shortJson(await fetch('api/upload-sessions/'+meta.job_id,{cache:'no-store'}),'Could not read upload status');
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
    const response=await fetch('api/upload-sessions/'+meta.job_id+'/files/'+kind+'/'+i,{
     method:'PUT',headers:{'X-Upload-Offset':String(offset)},body:chunk
    });
    if(response.status===409){
     const fresh=await shortJson(await fetch('api/upload-sessions/'+meta.job_id,{cache:'no-store'}),'Could not reconcile upload');
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
 await shortJson(await fetch('api/upload-sessions/'+meta.job_id+'/finalize',{method:'POST'}),'Could not finalize job');
 await shortDBClear();
 images.value='';audio.value='';selectedAudioFiles.length=0;
 imageCount.textContent='0 images selected';renderSelectedAudio();
 render.disabled=false;outputs.replaceChildren();
 statusEl.textContent='Job '+meta.job_id.slice(0,8)+' queued; rendering continues on VPS.';
 showShortProgress(100,0,'render');
 trackedShortJob=meta.job_id;pollShortJob(meta.job_id);loadQueue();
}
function shortResume(meta){
 if(activeShortUpload)return activeShortUpload;
 const run=()=>shortResumeInternal(meta);
 // Cross-tab Web Locks are advisory. The backend also serializes chunks/finalize.
 activeShortUpload=(navigator.locks&&navigator.locks.request
  ?navigator.locks.request('short-maker-one-upload',run)
  :run()).finally(()=>{activeShortUpload=null;});
 return activeShortUpload;
}
async function shortSubmitOrResume(){
 const existing=await shortDBRead('meta','active');
 if(existing)return shortResume(existing);
 const paths=drivePaths();
 if(inputMode.value==='upload'&&!images.files.length)throw Error('Select an image folder first.');
 if(inputMode.value==='drive'&&!paths.length)throw Error('Add at least one Google Drive folder.');
 if(inputMode.value==='drive'&&new Set(paths).size!==paths.length)throw Error('Remove duplicate Drive folder links.');
 const files={images:[],audio:[]},blobs=[];
 if(inputMode.value==='upload'){
  Array.from(images.files).forEach((file,index)=>{
   files.images.push({name:file.webkitRelativePath||file.name,size:file.size});
   blobs.push({kind:'images',index,blob:file});
  });
 }
 selectedAudioFiles.forEach((file,index)=>{
  files.audio.push({name:file.name,size:file.size});
  blobs.push({kind:'audio',index,blob:file});
 });
 const meta={job_id:null,files,config:shortSettings()};
 statusEl.textContent='Saving selected files for safe resume after refresh...';
 showShortProgress(0,0,'upload');
 // Do not start network uploads if the browser cannot save source files durably.
 await shortDBSave(meta,blobs);
 return shortResume(meta);
}
form.addEventListener('submit',async event=>{
 event.preventDefault();
 if(activeShortUpload)return;
 render.disabled=true;outputs.replaceChildren();
 try{await shortSubmitOrResume();}
 catch(error){
  render.disabled=false;
  statusEl.textContent='Upload paused: '+error.message+' — click Start Rendering to retry.';
 }
});
(async()=>{
 try{
  const pending=await shortDBRead('meta','active');
  if(pending){
   statusEl.textContent='Recovering interrupted file upload...';
   await shortResume(pending);
  }else{
   const lastJob=sessionStorage.getItem('shortMakerLastJob');
   if(lastJob){trackedShortJob=lastJob;pollShortJob(lastJob);}
  }
 }catch(error){
  render.disabled=false;
  statusEl.textContent='Upload paused: '+error.message+' — click Start Rendering to retry.';
 }
})();

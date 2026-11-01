/* Short Maker ready-made MP4 browser client; isolated from existing Maker render.
   Persists file blobs in IndexedDB before transfer: no silent loss after refresh.
   Requests are chunked and bounded. No file-count cap is imposed. */
(() => {
'use strict';
const BASE='/short-maker/api/ready-videos/sessions';
const CHUNK=4*1024*1024, WORKERS=2;
const $=id=>document.getElementById(id);
const input=$('foldersVideoInput'),dest=$('foldersVpsDestination');
const start=$('foldersStartUpload'),progress=$('foldersUploadProgress');
const pause=$('foldersPauseUpload'),resume=$('foldersResumeUpload');
const cancel=$('foldersCancelUpload'),retry=$('foldersRetryUpload');
if(!input||!dest||!start||!progress||!pause||!resume||!cancel||!retry)return;
const nameDb='short-maker-ready-videos-v1';
let busy=false,stop=false,paused=false,session=null,uploadFiles=[];
const status=$('foldersSelectedInfo');
function write(text){status.textContent=text}
function controls(){
 start.disabled=busy||!input.files.length||!dest.value||!!session;
 pause.disabled=!busy||paused;
 resume.disabled=busy||!session;
 cancel.disabled=busy||!session;
 retry.disabled=busy||!session;
}
function openDb(){return new Promise((resolve,reject)=>{
 const req=indexedDB.open(nameDb,1);
 req.onupgradeneeded=()=>{const d=req.result; if(!d.objectStoreNames.contains('files'))d.createObjectStore('files');if(!d.objectStoreNames.contains('meta'))d.createObjectStore('meta');};
 req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);
});}
async function dbGet(store,key){
 const db=await openDb();try{return await new Promise((resolve,reject)=>{
 const tx=db.transaction(store,'readonly'),req=tx.objectStore(store).get(key);
 req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);
 });}finally{db.close()}}
async function dbPut(store,key,val){
 const db=await openDb();try{await new Promise((resolve,reject)=>{
 const tx=db.transaction(store,'readwrite');tx.objectStore(store).put(val,key);
 tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
 });}finally{db.close()}}
async function dbDelete(store,key){
 const db=await openDb();try{await new Promise((resolve,reject)=>{
 const tx=db.transaction(store,'readwrite');tx.objectStore(store).delete(key);
 tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);
 });}finally{db.close()}}
async function json(response){
 let payload;try{payload=await response.json();}catch{throw Error('Invalid server response');}
 if(!response.ok)throw Error(payload.detail||'Upload server error');
 return payload;
}
function fileKey(sessionId,index){return sessionId+':'+index}
function row(snapshot){
 progress.replaceChildren();
 const summary=document.createElement('div');summary.className='hint';
 summary.textContent=snapshot.status.toUpperCase()+' · '+snapshot.uploaded_bytes+' / '+snapshot.total_bytes+' bytes received by VPS';
 progress.appendChild(summary);
 for(const file of snapshot.files){
  const container=document.createElement('div');container.className='song-item';
  const label=document.createElement('span');
  const percentage=file.size?Math.floor(100*file.uploaded/file.size):100;
  label.textContent=file.name+' · '+percentage+'% · '+file.uploaded+'/'+file.size+' bytes';
  const bar=document.createElement('div');bar.className='progress';bar.style.flex='0 0 100px';bar.style.margin='0';
  const indicator=document.createElement('span');indicator.style.width=percentage+'%';bar.appendChild(indicator);
  container.append(label,bar);progress.appendChild(container);
 }
}
async function currentSnapshot(){
 const snap=await json(await fetch(BASE+'/'+session.id,{cache:'no-store'}));
 row(snap);return snap;
}
async function transferOne(index){
 const item=uploadFiles[index];
 if(!item)throw Error('A selected file is missing from browser recovery storage. Re-select the same batch.');
 const blob=item.blob;
 let snapshot=await currentSnapshot();
 let offset=snapshot.files[index]?.uploaded;
 if(typeof offset!=='number'||blob.size!==snapshot.files[index].size)throw Error('Stored video does not match its VPS manifest');
 while(offset<blob.size&&!paused&&!stop){
  const next=blob.slice(offset,Math.min(blob.size,offset+CHUNK));
  try{
   const response=await fetch(BASE+'/'+session.id+'/files/'+index,{
     method:'PUT',headers:{'x-upload-offset':String(offset)},body:next
   });
   if(!response.ok)throw Error((await response.json()).detail||'Chunk transfer failed');
   const ack=await response.json();
   if(ack.uploaded<=offset)throw Error('VPS did not advance the upload offset');
   offset=ack.uploaded;
  }catch(error){
   const latest=await currentSnapshot();
   const recorded=latest.files[index]?.uploaded||0;
   if(recorded>offset){offset=recorded;continue;}
   throw error;
  }
  snapshot.files[index].uploaded=offset;
  snapshot.uploaded_bytes=snapshot.files.reduce((sum,f)=>sum+f.uploaded,0);
  row(snapshot);
 }
}
async function runUploads(){
 if(!session||busy)return;
 busy=true;stop=false;paused=false;controls();
 try{
  const manifest=await currentSnapshot();
  if(manifest.status==='completed'){await cleanup();write('Verified MP4s are already available in the VPS folder.');return;}
  if(manifest.status==='paused')await json(await fetch(BASE+'/'+session.id+'/resume',{method:'POST'}));
  if(manifest.status==='cancelled')throw Error('Cancelled session cannot be restarted; create a new batch.');
  const restored=[];
  for(let i=0;i<manifest.files.length;i++){
   const entry=await dbGet('files',fileKey(session.id,i));
   if(!entry||!entry.blob)throw Error('Recovery media missing for '+manifest.files[i].name+'. Retain the VPS session and re-select files.');
   restored.push(entry);
  }
  uploadFiles=restored;
  let index=0;
  async function worker(){
   while(index<restored.length&&!paused&&!stop){const i=index++;await transferOne(i);}
  }
  await Promise.all(Array.from({length:Math.min(WORKERS,restored.length)},worker));
  if(stop){
   await json(await fetch(BASE+'/'+session.id,{method:'DELETE'}));
   await cleanup();write('Cancelled; staging files cleaned.');return;
  }
  if(paused){
   const snap=await json(await fetch(BASE+'/'+session.id+'/pause',{method:'POST'}));
   row(snap);await dbPut('meta','current',{...session,paused:true});
   write('Paused. Resume when ready.');return;
  }
  write('All bytes transferred. Verifying playable videos and publishing safely…');
  const finished=await json(await fetch(BASE+'/'+session.id+'/finalize',{method:'POST'}));
  row(finished);await cleanup();
  write('Upload verified. Videos available to Short Uploader; publishing has NOT started.');
  if(typeof loadOwnerFolderNames==='function')await loadOwnerFolderNames();
 }catch(error){
  write('Upload paused safely: '+error.message+' · Retry resumes from VPS offset.');
  if(session)await dbPut('meta','current',{...session,paused:true});
 }finally{busy=false;controls();}
}
async function cleanup(){
 if(!session)return;
 const old=session;session=null;
 for(let i=0;i<old.fileCount;i++)await dbDelete('files',fileKey(old.id,i));
 await dbDelete('meta','current');
 uploadFiles=[];input.value='';controls();
}
async function saveSelectedFiles(){
 const files=[...input.files];
 if(!files.length||!dest.value)throw Error('Select your VPS folder and one or more MP4 files.');
 if(files.some(f=>!f.name.toLowerCase().endsWith('.mp4')||!f.size))throw Error('Only nonempty MP4 files are supported.');
 const total=files.reduce((sum,f)=>sum+f.size,0);
 if(navigator.storage?.estimate){
  const info=await navigator.storage.estimate();
  if(info.quota&&info.usage&&total>info.quota-info.usage)
   throw Error('Browser recovery storage is insufficient for this batch; files remain on PC.');
 }
 const requestId=crypto.randomUUID();
 const payload={folder:dest.value,client_request_id:requestId,
                files:files.map(f=>({name:f.name,size:f.size}))};
 const response=await json(await fetch(BASE,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}));
 session={id:response.session_id,fileCount:files.length,folder:dest.value,paused:false};
 await dbPut('meta','current',session);
 for(let i=0;i<files.length;i++){
  await dbPut('files',fileKey(session.id,i),{blob:files[i],name:files[i].name});
  write('Saving '+(i+1)+'/'+files.length+' files for refresh-safe recovery…');
 }
 row(response);
}
input.addEventListener('change',()=>{write(input.files.length+' selected PC videos · waiting for upload');controls()});
dest.addEventListener('change',controls);
start.addEventListener('click',async()=>{
 if(busy)return;
 try{start.disabled=true;await saveSelectedFiles();await runUploads();}
 catch(error){write('Cannot start upload: '+error.message);controls();}
});
pause.addEventListener('click',()=>{paused=true;pause.disabled=true;write('Pausing after in-flight chunks…')});
resume.addEventListener('click',()=>runUploads());
retry.addEventListener('click',()=>runUploads());
cancel.addEventListener('click',async()=>{
 if(busy){stop=true;write('Cancelling after in-flight chunks…');return;}
 if(!session||!confirm('Cancel this unfinished upload and delete its staged VPS chunks?'))return;
 try{await json(await fetch(BASE+'/'+session.id,{method:'DELETE'}));await cleanup();write('Cancelled safely.');}
 catch(error){write(error.message);}
});
(async()=>{
 try{
  const saved=await dbGet('meta','current');
  if(saved?.id){
   session=saved;
   const snap=await currentSnapshot();
   if(snap.status==='completed')await cleanup();
   else if(!saved.paused&&snap.status!=='cancelled')await runUploads();
   else write('Paused session recovered. Select Resume to continue.');
  }
 }catch(error){write('Recovery check: '+error.message);}
 finally{controls()}
})();
})();

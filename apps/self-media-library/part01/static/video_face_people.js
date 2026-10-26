// Clip face recognition: owner-initiated sampling and human-approved keywords.
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const encode = encodeURIComponent;
  const send = (url, body={}) => api(url, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body:JSON.stringify(body)
  });

  let reference = null, chosen = -1, visibleFaces = [], activePerson = '';
  let shown = [], selected = new Set(), timer = null, scanRequest = 0;

  const say = message => {el('videoFaceStatus').textContent=message;};
  const refSay = message => {el('videoReferenceStatus').textContent=message;};
  const controls = ['videoFaceApprove','videoFaceReject','videoFaceUndo'];
  function selectionState(){
    el('videoFaceSelectedCount').textContent=selected.size+' selected';
    controls.forEach(id=>el(id).disabled=!selected.size);
  }
  function clearTimer(){if(timer){clearInterval(timer);timer=null;}}

  function highlightFace(face){
    chosen = face.face_index;
    const video=el('videoFaceReferencePlayer');
    const seek=()=>{video.pause();video.currentTime=Math.min(
      Math.max(0,face.time_seconds),Math.max(0,(video.duration||face.time_seconds)-0.01)
    );};
    if(video.readyState>=1)seek();
    else video.addEventListener('loadedmetadata',seek,{once:true});
    const layer=el('videoFaceBoxes');
    layer.innerHTML='';
    visibleFaces.filter(item=>item.frame_index===face.frame_index).forEach(item=>{
      const button=document.createElement('button');
      button.type='button';button.className='face-box';
      button.dataset.videoFaceIndex=String(item.face_index);
      button.style.left=(100*item.bbox[0])+'%';
      button.style.top=(100*item.bbox[1])+'%';
      button.style.width=(100*item.bbox[2])+'%';
      button.style.height=(100*item.bbox[3])+'%';
      button.textContent=String(item.face_index+1);
      button.title='Reference face '+(item.face_index+1);
      button.classList.toggle('selected',item.face_index===chosen);
      button.onclick=()=>highlightFace(item);
      layer.appendChild(button);
    });
    for(const b of el('videoFaceChoices').querySelectorAll('[data-video-face-index]')){
      b.classList.toggle('selected',Number(b.dataset.videoFaceIndex)===chosen);
    }
    el('videoFaceCreate').disabled=false;
    refSay('Face '+(chosen+1)+' selected at '+face.time_seconds.toFixed(1)+
      's. Enter a name or choose an existing person below to scan.');
  }

  async function referenceClip(clip, retainName=false){
    reference=clip; chosen=-1;visibleFaces=[];
    el('videoFaceReference').classList.remove('hidden');
    el('videoFaceName').value=retainName?el('videoFaceName').value:'';
    el('videoFaceCreate').disabled=true;
    el('videoFaceChoices').innerHTML='';
    el('videoFaceBoxes').innerHTML='';
    const video=el('videoFaceReferencePlayer');
    video.src=mediaUrl(clip.clip_url);
    video.load();
    refSay('Detecting faces across video frames on VPS CPU…');
    el('videoFacePanel').scrollIntoView({behavior:'smooth',block:'start'});
    try{
      const result=await api('/api/v1/faces/clips/'+encode(clip.id));
      if(reference!==clip)return;
      visibleFaces=result.faces||[];
      if(!visibleFaces.length){refSay('No clear face detected. Try another clip.');return;}
      const choiceHost=el('videoFaceChoices');
      visibleFaces.forEach(face=>{
        const button=document.createElement('button');
        button.type='button';button.className='ghost';
        button.dataset.videoFaceIndex=String(face.face_index);
        button.textContent='Face '+(face.face_index+1)+' · '+face.time_seconds.toFixed(1)+'s';
        button.onclick=()=>highlightFace(face);
        choiceHost.appendChild(button);
      });
      highlightFace(visibleFaces[0]);
      refSay(visibleFaces.length+' face appearances detected. Select the correct face and name.');
    }catch(error){
      refSay('Video face detection failed: '+error.message+'. Retry detection.');
    }
  }

  async function createPerson(){
    if(!reference||chosen<0||!el('videoFaceName').value.trim()){
      refSay('Choose a face and enter a person name.');return;
    }
    const button=el('videoFaceCreate');
    button.disabled=true;
    try{
      const result=await send('/api/v1/people/video-reference',{
        name:el('videoFaceName').value.trim(),clip_id:reference.id,face_index:chosen
      });
      hideReference();
      await loadPeople(result.id);
      await startScan();
      toast('Person saved. Scanning video clips for suggestions.');
    }catch(error){refSay('Unable to save person: '+error.message);}
    finally{button.disabled=false;}
  }

  function hideReference(){
    el('videoFaceReference').classList.add('hidden');
    el('videoFaceReferencePlayer').pause();
    el('videoFaceReferencePlayer').removeAttribute('src');
    reference=null;
  }

  async function loadPeople(prefer=''){
    const selectedId=prefer||el('videoFacePicker').value;
    try{
      const result=await api('/api/v1/people');
      const picker=el('videoFacePicker');
      picker.innerHTML='<option value="">Choose a person</option>'+result.items.map(person=>
        '<option value="'+esc(person.id)+'">'+esc(person.name)+'</option>').join('');
      picker.value=result.items.some(p=>p.id===selectedId)?selectedId:
        (result.items.length?result.items[0].id:'');
      activePerson=picker.value;
      await Promise.all([loadMatches(),scanStatus()]);
    }catch(error){say('Unable to load people: '+error.message);}
  }

  async function startScan(){
    if(!activePerson){say('Save or select a person first.');return;}
    const person=activePerson;
    try{
      await send('/api/v1/people/'+encode(person)+'/video-scan');
      say('Scanning video library; new matches will require approval.');
      clearTimer();
      timer=setInterval(scanStatus,2500);
      await scanStatus();
    }catch(error){say('Video scan not started: '+error.message);}
  }

  async function scanStatus(){
    if(!activePerson)return;
    const person=activePerson;
    try{
      const state=await api('/api/v1/people/'+encode(person)+'/video-scan');
      if(person!==activePerson)return;
      say('Video scan: '+state.status+' · '+(state.scanned||0)+'/'+(state.total||0)+
        ' clips'+(state.error?' · '+state.error:'')+
        ' · Matches are similarity suggestions, not verified identities.');
      if(['completed','failed','interrupted'].includes(state.status)){
        clearTimer();await loadMatches();
      }
    }catch(error){say('Video scan status error: '+error.message);}
  }

  async function loadMatches(){
    const person=activePerson;
    if(!person){el('videoFaceMatches').innerHTML='<p class="empty">Select a person.</p>';return;}
    const request=++scanRequest;
    const state=el('videoFaceMatchState').value;
    try{
      const response=await api('/api/v1/people/'+encode(person)+'/video-matches?state='+encode(state));
      if(person!==activePerson||request!==scanRequest)return;
      shown=response.items||[];selected.clear();
      const host=el('videoFaceMatches');
      host.innerHTML=shown.map(item=>
        '<article class="face-match-card"><label class="face-match-check">'+
        '<input type="checkbox" data-video-match="'+esc(item.clip_id)+'" /> Select</label>'+
        (item.thumb_url?'<img loading="lazy" src="'+esc(mediaUrl(item.thumb_url))+'" alt="Clip preview" />':
          '<div class="face-video-no-thumb">▶ Video clip</div>')+
        '<div><b>'+esc(item.clip_id)+'</b><small>'+esc(item.state)+' · '+
        Number(item.time_seconds).toFixed(1)+'s · similarity '+Number(item.similarity).toFixed(3)+
        (Number(item.similarity)>=0.60?' · higher similarity':' · review carefully')+
        '</small></div></article>').join('')||
        '<p class="empty">No '+esc(state)+' video matches.</p>';
      host.querySelectorAll('[data-video-match]').forEach(input=>input.onchange=()=>{
        if(input.checked)selected.add(input.dataset.videoMatch);
        else selected.delete(input.dataset.videoMatch);
        selectionState();
      });
      selectionState();
    }catch(error){say('Unable to load video matches: '+error.message);}
  }

  async function decide(action){
    if(!activePerson||!selected.size)return;
    const ids=[...selected],person=activePerson;
    controls.forEach(id=>el(id).disabled=true);
    try{
      await send('/api/v1/people/'+encode(person)+'/video-'+action,{clip_ids:ids});
      toast(ids.length+' video face matches '+(action==='approve'?'approved and tagged':action==='reject'?'rejected':'reset for review'));
      await loadMatches();
      await loadClips(true);
    }catch(error){say('Video review failed: '+error.message);}
    finally{selectionState();}
  }

  el('identifyClipFace').addEventListener('click',()=>{
    const clip=state.current;
    if(!clip)return;
    closeDrawer();gotoPage('library');setLibraryTab('face');
    referenceClip(clip);
  });
  el('videoFaceRetry').addEventListener('click',()=>{if(reference)referenceClip(reference,true);});
  el('videoFaceReferenceClose').addEventListener('click',hideReference);
  el('videoFaceCreate').addEventListener('click',createPerson);
  el('videoFaceRefresh').addEventListener('click',()=>loadPeople());
  el('peopleRefresh').addEventListener('click',()=>loadPeople());
  document.querySelector('[data-library-tab="face"]').addEventListener('click',()=>loadPeople());
  el('videoFacePicker').addEventListener('change',()=>{
    clearTimer();activePerson=el('videoFacePicker').value;loadMatches();scanStatus();
  });
  el('videoFaceMatchState').addEventListener('change',loadMatches);
  el('videoFaceScan').addEventListener('click',startScan);
  el('videoFaceSelectAll').addEventListener('click',()=>{
    selected=new Set(shown.map(x=>x.clip_id));
    el('videoFaceMatches').querySelectorAll('[data-video-match]').forEach(x=>x.checked=true);
    selectionState();
  });
  el('videoFaceApprove').addEventListener('click',()=>decide('approve'));
  el('videoFaceReject').addEventListener('click',()=>decide('reject'));
  el('videoFaceUndo').addEventListener('click',()=>decide('undo'));
  selectionState();
  // No automatic frame processing or model downloads on page load.
})();

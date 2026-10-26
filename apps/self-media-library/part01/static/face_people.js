// Existing SML library UI extension: owner-reviewed person tagging only.
(() => {
  'use strict';
  const $face = id => document.getElementById(id);
  let activePerson = '';
  let referenceId = '';
  let faceIndex = -1;
  let detectionFailed = false;
  let matches = [];
  let selected = new Set();
  let scanTimer = null;

  const send = (url, body, method='POST') => api(url, {
    method,
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });

  const setMessage = (message) => {$face('peopleScanStatus').textContent=message;};
  const referenceMessage = message => {$face('peopleReferenceStatus').textContent=message;};

  async function inspectReference(id, retry=false) {
    const previousName = retry ? $face('peopleName').value : '';
    referenceId = id;
    faceIndex = -1;
    detectionFailed = false;
    const image = imageRows.find(row=>row.id===id);
    if (!image) return;
    $face('peopleReference').classList.remove('hidden');
    $face('peopleReferenceImage').src = mediaUrl(image.content_url);
    $face('peopleFaceBoxes').innerHTML = '';
    $face('peopleFaceChoices').innerHTML = '';
    $face('peopleName').value = previousName;
    referenceMessage('Detecting faces locally; this can also initialize CPU models.');
    $face('peopleCreate').disabled = true;
    $face('peopleCreate').textContent = 'Detecting face…';
    $face('peoplePanel').scrollIntoView({behavior:'smooth', block:'start'});
    try {
      const response=await api('/api/v1/faces/images/'+encodeURIComponent(id));
      if(referenceId!==id) return;
      if(!response.faces.length) {
        $face('peopleCreate').textContent = 'No face detected';
        referenceMessage('No clear face detected. Choose another reference image.');
        return;
      }
      const layer=$face('peopleFaceBoxes');
      const choices=$face('peopleFaceChoices');
      response.faces.forEach(face=>{
        const n=face.face_index;
        const box=document.createElement('button');
        box.type='button';box.className='face-box';
        box.style.left=(face.bbox[0]*100)+'%';
        box.style.top=(face.bbox[1]*100)+'%';
        box.style.width=(face.bbox[2]*100)+'%';
        box.style.height=(face.bbox[3]*100)+'%';
        box.dataset.faceIndex=n;
        box.textContent=String(n+1);
        box.title='Choose face '+(n+1);
        box.onclick=()=>selectFace(n);
        layer.appendChild(box);
        const button=document.createElement('button');
        button.type='button';button.className='ghost';
        button.dataset.faceIndex=n;
        button.textContent='Face '+(n+1);
        button.onclick=()=>selectFace(n);
        choices.appendChild(button);
      });
      if(response.faces.length===1) selectFace(response.faces[0].face_index);
      else $face('peopleCreate').textContent = 'Select a face first';
      referenceMessage(response.faces.length+' face(s) detected. Select face and enter person name.');
    } catch (error) {
      detectionFailed = true;
      $face('peopleCreate').disabled = false;
      $face('peopleCreate').textContent = 'Retry face detection';
      referenceMessage('Face detector unavailable: '+error.message+'. You can retry without losing the name.');
    }
  }

  function selectFace(n) {
    detectionFailed=false;
    faceIndex=n;
    for(const node of $face('peoplePanel').querySelectorAll('[data-face-index]')) {
      node.classList.toggle('selected',Number(node.dataset.faceIndex)===n);
    }
    $face('peopleCreate').disabled=false;
    $face('peopleCreate').textContent='Save person & scan images';
  }

  async function loadPeople(prefer='') {
    try {
      const data=await api('/api/v1/people');
      const picker=$face('peoplePicker');
      const prev=prefer || picker.value;
      picker.innerHTML='<option value="">Choose a person</option>'+data.items.map(p=>
        '<option value="'+esc(p.id)+'">'+esc(p.name)+' · '+p.approved+' approved · '+p.pending+' pending</option>').join('');
      if(data.items.some(p=>p.id===prev)) picker.value=prev;
      else if(data.items.length) picker.value=data.items[0].id;
      activePerson=picker.value;
      if(activePerson) await Promise.all([loadMatches(),updateScanStatus()]);
      else { $face('peopleMatches').innerHTML='';setMessage('Select an image and click Identify person to begin.'); }
    } catch(e) {setMessage('Could not load people: '+e.message);}
  }

  async function createPerson() {
    if(detectionFailed && referenceId) {
      await inspectReference(referenceId, true);
      return;
    }
    const name=$face('peopleName').value.trim();
    if(!referenceId || faceIndex<0 || !name) {
      referenceMessage('Choose a face and enter its name first.');return;
    }
    const button=$face('peopleCreate');
    button.disabled=true;
    button.textContent='Saving person…';
    try {
      const person=await send('/api/v1/people', {image_id:referenceId,face_index:faceIndex,name});
      $face('peopleReference').classList.add('hidden');
      await loadPeople(person.id);
      await startScan();
      toast('Person saved; scanning library images');
    } catch(error) {referenceMessage('Could not save person: '+error.message);}
    finally{button.disabled=false;button.textContent='Save person & scan images';}
  }

  async function startScan() {
    if(!activePerson) {toast('Choose a named person first');return;}
    try {
      await send('/api/v1/people/'+encodeURIComponent(activePerson)+'/scan',{});
      setMessage('Scanning all images. Matched faces require your approval.');
      if(scanTimer) clearInterval(scanTimer);
      scanTimer=setInterval(updateScanStatus,2500);
      await updateScanStatus();
    } catch(error){setMessage('Scan not started: '+error.message);}
  }

  async function updateScanStatus() {
    if(!activePerson)return;
    const person=activePerson;
    try {
      const state=await api('/api/v1/people/'+encodeURIComponent(person)+'/scan');
      if(person!==activePerson)return;
      const status=state.status||'not_started';
      setMessage('Scan: '+status+' · '+(state.scanned||0)+'/'+(state.total||0)+' images'+
        (state.error?' · '+state.error:'')+' · Matches are suggestions, not verified identities.');
      if(['completed','failed','interrupted'].includes(status)) {
        if(scanTimer){clearInterval(scanTimer);scanTimer=null;}
        await loadMatches();
        await refreshPersonCounts();
      }
    } catch(error){setMessage('Scan status unavailable: '+error.message);}
  }

  async function refreshPersonCounts() {
    const chosen=activePerson;
    const data=await api('/api/v1/people');
    $face('peoplePicker').innerHTML='<option value="">Choose a person</option>'+data.items.map(p=>
      '<option value="'+esc(p.id)+'">'+esc(p.name)+' · '+p.approved+' approved · '+p.pending+' pending</option>').join('');
    $face('peoplePicker').value=chosen;
  }

  function updateSelection() {
    $face('peopleSelectedCount').textContent=selected.size+' selected';
    $face('peopleApprove').disabled=!selected.size;
    $face('peopleReject').disabled=!selected.size;
    $face('peopleUndo').disabled=!selected.size;
  }

  async function loadMatches() {
    const person=activePerson;
    if(!person) return;
    const status=$face('peopleMatchState').value;
    try {
      const result=await api('/api/v1/people/'+encodeURIComponent(person)+'/matches?state='+status);
      if(person!==activePerson)return;
      const found=new Map();
      result.items.forEach(item=>{
        if(!found.has(item.image_id))found.set(item.image_id,item);
      });
      matches=[...found.values()];
      selected.clear();
      const host=$face('peopleMatches');
      host.innerHTML=matches.map(match=>
        '<article class="face-match-card"><label class="face-match-check">'+
          '<input type="checkbox" data-face-match="'+esc(match.image_id)+'" /> Select</label>'+
          '<img loading="lazy" src="'+esc(mediaUrl(match.thumb_url))+'" alt="'+esc(match.original_name)+'" />'+
          '<div><b>'+esc(match.original_name)+'</b><small>'+esc(match.state)+
          ' · Similarity '+Number(match.similarity).toFixed(3)+
          (match.similarity>=0.60?' · higher similarity':' · review carefully')+
          '</small></div></article>').join('') ||
          '<p class="empty">No '+esc(status)+' face matches.</p>';
      host.querySelectorAll('[data-face-match]').forEach(input=>input.onchange=()=>{
        input.checked?selected.add(input.dataset.faceMatch):selected.delete(input.dataset.faceMatch);
        updateSelection();
      });
      updateSelection();
    } catch(error) {setMessage('Could not load matches: '+error.message);}
  }

  async function decide(action) {
    if(!activePerson || !selected.size)return;
    const count=selected.size,person=activePerson,ids=[...selected];
    const buttons=['peopleApprove','peopleReject','peopleUndo'].map(id=>$face(id));
    buttons.forEach(b=>b.disabled=true);
    try {
      await send('/api/v1/people/'+encodeURIComponent(person)+'/'+action,{image_ids:ids});
      toast(count+' matches '+(action==='approve'?'approved and tagged':action==='reject'?'rejected':'returned to pending'));
      await Promise.all([loadMatches(),refreshPersonCounts(),loadImages(true)]);
    }catch(error){toast('Person tags not changed: '+error.message);}
    finally{updateSelection();}
  }

  $face('identifyImage').addEventListener('click',()=>{
    if(!selectedImage)return;
    // The reference picker now lives in its own pane, so reveal it before
    // inspectReference scrolls the chosen face into view.
    if(typeof setLibraryTab==='function')setLibraryTab('face');
    inspectReference(selectedImage.id);
  });
  $face('peopleReferenceClose').addEventListener('click',()=>{
    referenceId='';$face('peopleReference').classList.add('hidden');
  });
  $face('peopleCreate').addEventListener('click',createPerson);
  $face('peopleScan').addEventListener('click',startScan);
  $face('peopleRefresh').addEventListener('click',()=>loadPeople());
  $face('peoplePicker').addEventListener('change',()=>{
    activePerson=$face('peoplePicker').value;
    if(scanTimer){clearInterval(scanTimer);scanTimer=null;}
    loadMatches();updateScanStatus();
  });
  $face('peopleMatchState').addEventListener('change',loadMatches);
  $face('peopleSelectAll').addEventListener('click',()=>{
    selected=new Set(matches.map(m=>m.image_id));
    for(const input of $face('peopleMatches').querySelectorAll('[data-face-match]'))input.checked=true;
    updateSelection();
  });
  $face('peopleApprove').addEventListener('click',()=>decide('approve'));
  $face('peopleReject').addEventListener('click',()=>decide('reject'));
  $face('peopleUndo').addEventListener('click',()=>decide('undo'));
  updateSelection();
  // Do not scan any face on page load. Owner must opt in.
  loadPeople();
})();

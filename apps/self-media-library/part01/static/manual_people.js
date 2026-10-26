// Manual person collections: separate from optional Face Recognition and AI metadata.
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  if (!el('manualPeoplePanel')) return;
  let people = [];
  let selectedPerson = '';
  let editing = false;

  function status(message, error=false) {
    const node = el('manualPersonStatus');
    node.textContent = message;
    node.classList.toggle('manual-people-error', error);
  }
  const post = (path, payload, method='POST') => api(path, {
    method,
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  function tags() {
    const preset = el('manualActionPreset').value;
    const extra = el('manualPersonActionTags').value.split(',').map(x=>x.trim()).filter(Boolean);
    return [...new Set([preset, ...extra].filter(Boolean))];
  }
  const aliases = () => el('manualPersonAliases').value.split(',').map(x=>x.trim()).filter(Boolean);
  function setEditing(value) {
    editing = value;
    el('manualPersonSave').textContent = value ? 'Save person changes' : '+ Create person';
    el('manualPersonEditCancel').hidden = !value;
  }
  async function loadPeople(preferred=selectedPerson) {
    const response = await api('/api/v1/manual-people');
    people = response.items || [];
    const picker = el('manualPersonPicker');
    picker.innerHTML = '<option value="">Select a person</option>' + people.map(person=>
      '<option value="'+esc(person.id)+'">'+esc(person.name)+' ('+person.counts.image+' images, '+
      person.counts.video+' clips)</option>').join('');
    selectedPerson = people.some(p=>p.id===preferred) ? preferred : '';
    picker.value = selectedPerson;
    await loadAssets();
  }
  async function savePerson() {
    const name = el('manualPersonName').value.trim();
    if (!name) { status('Enter a person name first.', true); return; }
    const button = el('manualPersonSave');
    button.disabled = true;
    try {
      const result = await post(editing
        ? '/api/v1/manual-people/'+encodeURIComponent(selectedPerson)
        : '/api/v1/manual-people', {name, aliases: aliases()}, editing?'PATCH':'POST');
      el('manualPersonName').value = '';
      el('manualPersonAliases').value = '';
      setEditing(false);
      await loadPeople(result.id);
      status('Person saved. Now select images or video clips below and add them.');
    } catch(e) {
      status('Could not save person: '+e.message, true);
    } finally { button.disabled=false; }
  }
  async function loadAssets() {
    const host = el('manualPersonAssets');
    const person = people.find(p=>p.id===selectedPerson);
    el('manualPersonEdit').disabled = !person;
    if (!person) {
      host.innerHTML = '<p class="source-meta">Create or select a person to see linked media.</p>';
      return;
    }
    const data = await api('/api/v1/manual-people/'+encodeURIComponent(person.id)+'/assets?limit=100');
    const items = data.items || [];
    host.innerHTML = items.length ? items.map(item=>
      '<article class="manual-person-asset">'+
      (item.thumb_url?'<img loading="lazy" src="'+esc(mediaUrl(item.thumb_url))+'" alt="">':
        '<span class="manual-person-no-preview">▶</span>')+
      '<div><b>'+esc(item.name)+'</b><small>'+esc(item.type)+' · '+esc(item.status)+'</small>'+
      '<small>'+esc((item.action_tags||[]).join(', ')||'No action tags')+'</small></div>'+
      '<button class="ghost" type="button" data-unlink-kind="'+esc(item.type)+'" data-unlink-id="'+esc(item.id)+'">Unlink</button>'+
      '</article>').join('') : '<p class="source-meta">No media linked yet. Select images or clips in the existing library below.</p>';
    host.querySelectorAll('[data-unlink-id]').forEach(button=>button.onclick=async()=>{
      if (!confirm('Unlink this asset from '+person.name+'? Original media and keywords stay unchanged.')) return;
      button.disabled=true;
      try {
        await post('/api/v1/manual-people/'+encodeURIComponent(person.id)+'/unassign',
          {media_type:button.dataset.unlinkKind,ids:[button.dataset.unlinkId]});
        await loadPeople(person.id);
        status('Media unlinked. Original file and existing tags preserved.');
      } catch(e) { button.disabled=false; status(e.message,true); }
    });
    el('manualPersonSummary').textContent = person.counts.image+' images · '+person.counts.video+' clips';
  }
  async function assign(mediaType) {
    if (!selectedPerson) { status('Create or select a person first.', true); return; }
    const ids = [...(mediaType==='image' ? imageSelected : state.selected)];
    if (!ids.length) { status('Tick the '+(mediaType==='image'?'image':'clip')+' checkboxes below first.',true); return; }
    const actionTags = tags();
    const button = el(mediaType==='image'?'manualAssignImages':'manualAssignClips');
    button.disabled=true;
    let done=0;
    try {
      for (let start=0;start<ids.length;start+=500) {
        const batch=ids.slice(start,start+500);
        const result=await post('/api/v1/manual-people/'+encodeURIComponent(selectedPerson)+'/assign',
          {media_type:mediaType,ids:batch,action_tags:actionTags});
        done+=result.assigned||0;
      }
      await loadPeople(selectedPerson);
      status(done+' '+(mediaType==='image'?'images':'clips')+
        ' added. Person and action tags are now available to Scene Builder search.');
    } catch(e) { status(done+' linked; remaining batch failed: '+e.message,true); }
    finally {button.disabled=false;}
  }

  el('manualPersonSave').onclick=savePerson;
  el('manualPersonPicker').onchange=async()=>{
    selectedPerson=el('manualPersonPicker').value;
    setEditing(false);
    try{await loadAssets();}catch(e){status(e.message,true);}
  };
  el('manualPersonEdit').onclick=()=>{
    const person=people.find(p=>p.id===selectedPerson);
    if(!person)return;
    el('manualPersonName').value=person.name;
    el('manualPersonAliases').value=person.aliases.join(', ');
    setEditing(true);
    el('manualPersonName').focus();
  };
  el('manualPersonEditCancel').onclick=()=>{
    el('manualPersonName').value='';
    el('manualPersonAliases').value='';
    setEditing(false);
  };
  el('manualAssignImages').onclick=()=>assign('image');
  el('manualAssignClips').onclick=()=>assign('video');
  el('manualPersonRefresh').onclick=()=>loadPeople().catch(e=>status(e.message,true));
  loadPeople().catch(e=>status('Person collections unavailable: '+e.message,true));
})();

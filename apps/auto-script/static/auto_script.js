(() => {
  const $ = s => document.querySelector(s);
  const state = {channels:[],items:[],documents:[]};
  const esc = v => String(v ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
  const fmt = v => { if(!v) return '—'; try{return new Date(v).toLocaleString()}catch(_){return v} };
  const statusClass = v => {v=String(v||'').toLowerCase();if(v==='done')return'done';if(v==='error')return'error';if(v.includes('running'))return'running';if(v.includes('queued'))return'queued';if(v.includes('retry'))return'retry';return''};
  const toast = (msg, err=false) => { const n=document.createElement('div');n.className='as-toast'+(err?' error':'');n.textContent=msg;document.body.appendChild(n);setTimeout(()=>n.remove(),3600); };
  async function api(url,opt={}){
    const r=await fetch(url,{headers:{'Content-Type':'application/json','Accept':'application/json'},credentials:'same-origin',...opt});
    const ct=r.headers.get('content-type')||'';
    const data=ct.includes('json')?await r.json():{detail:await r.text()};
    if(!r.ok) throw new Error(data.detail||data.error||`HTTP ${r.status}`);
    return data;
  }
  function channelName(id){return state.channels.find(c=>Number(c.id)===Number(id))?.name||`#${id}`}
  function docLink(url,label){return url?`<a class="as-link" target="_blank" rel="noopener" href="${esc(url)}">${esc(label)}</a>`:'—'}
  function render(){
    const active=state.items.filter(i=>!['DONE','ERROR'].includes(i.status)).length;
    const done=state.items.filter(i=>i.status==='DONE').length;
    const errors=state.items.filter(i=>i.status==='ERROR').length+state.channels.filter(c=>c.last_error).length;
    $('#as-stat-channels').textContent=state.channels.length;$('#as-stat-active').textContent=active;$('#as-stat-done').textContent=done;$('#as-stat-errors').textContent=errors;
    const sched=$('#as-scheduler');sched.textContent=state.scheduler?'Scheduler active':'Scheduler stopped';sched.classList.toggle('off',!state.scheduler);

    $('#as-channels-body').innerHTML=state.channels.length?state.channels.map(c=>`
      <tr>
        <td><div class="as-channel-name">${esc(c.name)}</div><div class="as-muted">${esc(c.timezone||'')}</div></td>
        <td>${esc(c.sheet_gid)}</td><td>${esc(c.daily_limit)}</td><td>${esc(c.today_count)} / ${esc(c.daily_limit)}</td>
        <td>
          ${c.active_document_id?`<a class="as-link" target="_blank" rel="noopener" href="https://docs.google.com/document/d/${encodeURIComponent(c.active_document_id)}/edit">Scripts ${esc(c.tabs_used)} / ${esc(c.max_tabs_per_doc)}</a>`:'Scripts —'}
          <br>${c.active_scene_document_id?`<a class="as-link" target="_blank" rel="noopener" href="https://docs.google.com/document/d/${encodeURIComponent(c.active_scene_document_id)}/edit">Scene Plans ${esc(c.scene_tabs_used)} / ${esc(c.max_tabs_per_doc)}</a>`:'Scene Plans —'}
        </td>
        <td><span class="as-status ${c.enabled?'done':'error'}">${c.enabled?'Enabled':'Paused'}</span>${c.last_error?`<div class="as-muted">${esc(c.last_error).slice(0,110)}</div>`:''}</td>
        <td><button class="as-btn small" data-edit="${c.id}">Edit</button> <button class="as-btn small" data-run="${c.id}">Run</button> <button class="as-btn small ${c.enabled?'danger':''}" data-toggle="${c.id}" data-enabled="${c.enabled?0:1}">${c.enabled?'Pause':'Enable'}</button></td>
      </tr>`).join(''):`<tr><td colspan="7" class="as-empty">No channels configured.</td></tr>`;

    $('#as-items-body').innerHTML=state.items.length?state.items.slice(0,150).map(i=>`
      <tr>
        <td>${esc(channelName(i.channel_id))}</td><td><b>${esc(i.vid)}</b></td><td title="${esc(i.title)}">${esc(i.title).slice(0,80)}</td>
        <td><span class="as-status ${statusClass(i.status)}">${esc(i.status)}</span>${i.error_text?`<div class="as-muted">${esc(i.error_text).slice(0,120)}</div>`:''}</td>
        <td>M ${esc(i.metadata_attempts||0)} · S ${esc(i.script_attempts||0)} · P ${esc(i.scene_attempts||0)}</td>
        <td>${docLink(i.script_document_url,'Script')}<br>${docLink(i.scene_document_url,'Scene Plan')}</td>
        <td>${fmt(i.updated_at)}</td>
        <td>${i.status!=='DONE'?`<button class="as-btn small" data-retry="${i.id}">Retry</button>`:''}</td>
      </tr>`).join(''):`<tr><td colspan="8" class="as-empty">No queue items yet.</td></tr>`;

    $('#as-docs').innerHTML=state.documents.length?state.documents.map(d=>`
      <div class="as-doc-row"><b>${esc(d.name)} · ${esc(d.kind)}</b><a class="as-link" target="_blank" rel="noopener" href="https://docs.google.com/document/d/${encodeURIComponent(d.document_id)}/edit">${esc(d.document_title)}</a><div class="as-muted">${esc(d.tabs_used)} / ${esc(d.max_tabs_per_doc)} VID tabs · document #${esc(d.document_index)}</div></div>`).join(''):`<div class="as-empty">No documents created yet.</div>`;

    const errs=[
      ...state.channels.filter(c=>c.last_error).map(c=>({who:c.name,msg:c.last_error,at:c.updated_at})),
      ...state.items.filter(i=>i.error_text||i.status==='ERROR').map(i=>({who:`${channelName(i.channel_id)} · ${i.vid}`,msg:i.error_text||i.status,at:i.updated_at}))
    ].sort((a,b)=>String(b.at).localeCompare(String(a.at))).slice(0,20);
    $('#as-errors').innerHTML=errs.length?errs.map(e=>`<div class="as-error-row"><b>${esc(e.who)}</b><span class="as-muted">${fmt(e.at)}</span><p>${esc(e.msg)}</p></div>`).join(''):`<div class="as-empty">No errors.</div>`;
  }
  async function load(){try{const d=await api('/api/auto-script/state');Object.assign(state,d);render()}catch(e){toast(e.message,true)}}
  function openModal(c=null){
    $('#as-modal-title').textContent=c?'Edit channel':'Add channel';$('#as-id').value=c?.id||'';$('#as-name').value=c?.name||'';$('#as-daily').value=c?.daily_limit||5;$('#as-sheet-url').value=c?.sheet_url||'';$('#as-sheet-gid').value=c?.sheet_gid||'';$('#as-timezone').value=c?.timezone||'Asia/Karachi';$('#as-docs-folder').value=c?.docs_folder_url||'';$('#as-max-tabs').value=c?.max_tabs_per_doc||50;$('#as-worker').value=c?.preferred_worker_id||'';$('#as-meta-prompt').value=c?.metadata_prompt_text||'';$('#as-script-prompt').value=c?.script_prompt_text||c?.prompt_text||'';$('#as-scene-prompt').value=c?.scene_prompt_text||'';$('#as-enabled').checked=c?!!c.enabled:true;$('#as-modal').hidden=false;document.body.style.overflow='hidden';
  }
  function closeModal(){ $('#as-modal').hidden=true;document.body.style.overflow=''; }
  $('#as-add-channel').addEventListener('click',()=>openModal());$('#as-modal-close').addEventListener('click',closeModal);$('#as-cancel').addEventListener('click',closeModal);$('#as-modal').addEventListener('click',e=>{if(e.target===$('#as-modal'))closeModal()});
  $('#as-channel-form').addEventListener('submit',async e=>{
    e.preventDefault();
    const body={id:Number($('#as-id').value||0),name:$('#as-name').value.trim(),daily_limit:Number($('#as-daily').value||5),sheet_url:$('#as-sheet-url').value.trim(),sheet_gid:$('#as-sheet-gid').value.trim(),timezone:$('#as-timezone').value.trim(),docs_folder_url:$('#as-docs-folder').value.trim(),max_tabs_per_doc:Number($('#as-max-tabs').value||50),preferred_worker_id:$('#as-worker').value.trim(),metadata_prompt_text:$('#as-meta-prompt').value,script_prompt_text:$('#as-script-prompt').value,scene_prompt_text:$('#as-scene-prompt').value,enabled:$('#as-enabled').checked};
    try{await api('/api/auto-script/channels',{method:'POST',body:JSON.stringify(body)});closeModal();toast('Channel saved. Unified-sheet validation and scan started.');await load()}catch(err){toast(err.message,true)}
  });
  $('#as-run-all').addEventListener('click',async()=>{try{await api('/api/auto-script/tick',{method:'POST',body:'{}'});toast('Scan completed.');await load()}catch(e){toast(e.message,true)}});
  document.addEventListener('click',async e=>{
    const edit=e.target.closest('[data-edit]');if(edit){const c=state.channels.find(x=>Number(x.id)===Number(edit.dataset.edit));if(c)openModal(c);return}
    const run=e.target.closest('[data-run]');if(run){try{await api(`/api/auto-script/channels/${run.dataset.run}/run`,{method:'POST',body:'{}'});toast('Channel scan completed.');await load()}catch(err){toast(err.message,true)}return}
    const tog=e.target.closest('[data-toggle]');if(tog){try{await api(`/api/auto-script/channels/${tog.dataset.toggle}/toggle`,{method:'POST',body:JSON.stringify({enabled:Number(tog.dataset.enabled)===1})});await load()}catch(err){toast(err.message,true)}return}
    const retry=e.target.closest('[data-retry]');if(retry){try{await api(`/api/auto-script/items/${retry.dataset.retry}/retry`,{method:'POST',body:'{}'});toast('Retry queued.');await load()}catch(err){toast(err.message,true)}}
  });
  load();setInterval(load,15000);
})();

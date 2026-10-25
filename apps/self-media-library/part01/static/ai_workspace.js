/* AI Content Master: existing native five-tab workspace, no extra website shell. */
(()=>{
'use strict';
const find=s=>document.querySelector(s), all=s=>Array.from(document.querySelectorAll(s));
let results=[],selected=new Set(),current=null,returnFocus=null;
const safe=fn=>async event=>{try{await fn(event)}catch(error){toast(error.message);const status=find('#aiSearchStatus');if(status)status.textContent=error.message}};
const post=(url,body,method='POST')=>api(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function filters(){return Object.fromEntries(all('#aiFilters [name]').map(x=>[x.name,x.value]).filter(([k,v])=>v!==''))}
async function refresh(){
 const s=await api('/api/v1/ai/status');
 find('#aiDashboard').textContent=`${s.totals.images} images · ${s.totals.clips} clips · ${s.totals.approved_unused} approved unused · ${s.totals.exact_duplicate_review} exact duplicates · ${s.counts.complete||0} analyzed · ${s.counts.pending||0} pending · ${s.jobs.running||0} running · ${s.jobs.failed||0} failed · ${s.state}`;
 find('#aiModelState').textContent=s.state;
 find('#aiModelInfo').textContent=`${s.model} · CPU ${s.threads} threads · model version ${s.model_version} · index up to ${s.max_catalog} assets`;
 find('#aiCapabilities').textContent=s.score_note+' OCR: '+(s.ocr_available?'ready (English)':'not configured')+' · Speech: '+(s.speech_available?'ready (first 30 seconds)':'not configured')+'. Unavailable: '+s.unsupported.join(', ')+'. '+s.drive_cleanup+'.';
 find('#aiQueueNote').textContent=s.state+' · '+(s.worker_running?'Worker running':'Worker starts when the local model is ready')+' · jobs survive restarts.';
 find('#aiEnabled').checked=s.enabled;find('#aiBatchLimit').value=s.max_batch;find('#aiTimeout').value=s.job_timeout_seconds;
 const jobs=await api('/api/v1/ai/jobs');
 find('#aiJobs').innerHTML=jobs.items.length?jobs.items.map(j=>`<div class="sml-ai-job"><div><b>${esc(j.media_id||j.kind)}</b><p>${esc(j.state)} · attempt ${j.attempts}${j.error?' · '+esc(j.error):''}</p></div>${['failed','cancelled'].includes(j.state)?`<button class="ghost" data-job="${esc(j.id)}" data-action="retry">Retry</button>`:['queued','running'].includes(j.state)?`<button class="ghost" data-job="${esc(j.id)}" data-action="cancel">Cancel</button>`:''}</div>`).join(''):'<p class="empty">No AI jobs yet. Add media to begin.</p>';
 all('[data-job]').forEach(b=>b.onclick=safe(async()=>{await post('/api/v1/ai/jobs/'+b.dataset.job+'/'+b.dataset.action,{});await refresh()}));
}
function render(items){
 results=items;selected.clear();find('#aiSelectedCount').textContent='0 selected';
 find('#aiResults').innerHTML=items.length?items.map((a,i)=>`<article class="sml-ai-card"><label class="sml-ai-check"><input type="checkbox" data-select="${i}" aria-label="Select ${esc(a.id)}"> Select</label><button class="sml-ai-preview" data-inspect="${i}" aria-label="Inspect ${esc(a.source_filename||a.id)}"><img src="${esc(mediaUrl(a.thumb_url))}" loading="lazy" alt="${esc(a.source_filename||a.id)}"></button><div><h3>${esc(a.source_filename||a.id)}</h3><p>${esc(a.type)} · ${esc(a.status)} · Q${a.quality} · ${esc(a.ai_state)}</p><p>${esc(a.description||'No description yet')}</p><button class="ghost" data-inspect="${i}">Metadata &amp; preview</button></div></article>`).join(''):'<p class="empty">No matching indexed media. Analyze assets or adjust filters.</p>';
 all('[data-inspect]').forEach(b=>b.onclick=safe(()=>inspect(results[Number(b.dataset.inspect)])));
 all('[data-select]').forEach(b=>b.onchange=()=>{b.checked?selected.add(Number(b.dataset.select)):selected.delete(Number(b.dataset.select));find('#aiSelectedCount').textContent=selected.size+' selected'});
}
async function search(){
 find('#aiSearchStatus').textContent='Searching…';
 const params=new URLSearchParams({...filters(),query:find('#aiQuery').value.trim()});
 const r=await api('/api/v1/ai/search?'+params);render(r.items);
 find('#aiSearchStatus').textContent=r.total+' matches · '+(r.semantic?'Semantic relevance + quality ranking':'Catalog filters')+' · model suggestions need review';
}
async function inspect(a){
 const d=await api('/api/v1/ai/assets/'+a.type+'/'+encodeURIComponent(a.id));current={...a,detail:d};returnFocus=document.activeElement;
 find('#aiInspector').classList.remove('hidden');find('#aiInspectorTitle').textContent=a.source_filename||a.id;
 const machine=d.machine||{},manual=d.manual||{},effective=d.effective||manual;
 find('#aiInspectorMedia').innerHTML=a.type==='video'?`<video controls preload="metadata" src="${esc(mediaUrl(a.content_url))}"></video>`:`<img alt="Selected media" src="${esc(mediaUrl(a.content_url))}">`;
 find('#aiInspectorState').textContent=`${d.state} · ${d.model_version||'No visual analysis yet'}${d.analyzed_at?' · '+d.analyzed_at:''}`;
 find('#aiDescription').value=effective.description??a.description??'';find('#aiCustom').value=JSON.stringify(manual.custom||{});find('#aiAliases').value=JSON.stringify(manual.aliases||{});find('#aiKeywords').value=(manual.keywords||[]).join(', ');
 find('#aiReviewReason').value=manual.review_reason||'';find('#aiRights').checked=!!(manual.rights_confirmed??a.rights_confirmed);
 find('#aiManualCategories').innerHTML=['objects','actions','environment','lighting','mood','shot','camera_movement'].map(k=>`<label>${esc(k.replaceAll('_',' '))}<input data-category="${k}" value="${esc(manual[k]||'')}" placeholder="Manual correction (optional)"></label>`).join('');
 const fields=machine.fields||{};
 find('#aiFieldDetails').innerHTML='<h3>Visual model suggestions</h3><p>'+esc(machine.confidence_note||'No generated visual metadata')+'</p>'+Object.entries(fields).map(([k,v])=>'<h4>'+esc(k)+'</h4><ul>'+v.map(x=>`<li>${esc(x.value)} · relative score ${(x.confidence*100).toFixed(1)}% · frame ${x.timestamp}s · generated, uncertain</li>`).join('')+'</ul>').join('')+'<h3>OCR &amp; speech</h3><p>'+esc(machine.ocr?.text||'No text detected')+'</p><p>'+esc(machine.transcript?.text||'No transcript')+'</p><p>'+esc(machine.transcript?.note||'')+'</p><h3>Quality &amp; palette</h3><pre>'+esc(JSON.stringify(machine.quality||{},null,2))+'</pre>'+(machine.contact_sheet?'<img alt="Video contact sheet" src="'+esc(mediaUrl(machine.contact_sheet))+'">':'')+'<h3>Crop suggestions</h3><pre>'+esc(JSON.stringify(machine.crop_recommendations||[],null,2))+'</pre><p>'+esc((machine.review_alerts||[]).join(' · '))+'</p><h3>Frame timeline</h3>'+ (machine.frames||[]).map(f=>'<p>'+f.timestamp+'s · '+esc(f.fields.environment?.[0]?.value||'')+'</p>').join('');
 find('#aiHistoryDetails').textContent='';find('#aiInspectorClose').focus();
}
find('#aiInspectorClose').onclick=()=>{find('#aiInspector').classList.add('hidden');current=null;returnFocus?.focus()};
document.addEventListener('keydown',event=>{if(event.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)){event.preventDefault();gotoPage('library');find('#aiQuery').focus()}if(event.key==='Escape'&&!find('#aiInspector').classList.contains('hidden'))find('#aiInspectorClose').click()});
find('#aiMetadataForm').onsubmit=safe(async event=>{
 event.preventDefault();if(!current)return;
 const fields={description:find('#aiDescription').value,keywords:find('#aiKeywords').value.split(',').map(x=>x.trim()).filter(Boolean),review_reason:find('#aiReviewReason').value,rights_confirmed:find('#aiRights').checked,custom:JSON.parse(find('#aiCustom').value||'{}'),aliases:JSON.parse(find('#aiAliases').value||'{}')};
 all('[data-category]').forEach(x=>{if(x.value.trim()||current.detail.manual?.[x.dataset.category]!==undefined)fields[x.dataset.category]=x.value.trim()});
 await post('/api/v1/ai/assets/'+current.type+'/'+current.id,{fields,expected_revision:current.detail.revision},'PATCH');toast('Corrections saved; manual values take priority');await inspect(current);
});
find('#aiReanalyze').onclick=safe(async()=>{if(!current)return;await post('/api/v1/ai/analyze',{media_type:current.type,ids:[current.id]});toast('Visual analysis queued');await refresh()});
find('#aiSimilar').onclick=safe(async()=>{if(!current)return;const r=await api('/api/v1/ai/assets/'+current.type+'/'+current.id+'/similar');find('#aiInspectorClose').click();render(r.items);find('#aiSearchStatus').textContent='Visually similar media'});
find('#aiHistory').onclick=safe(async()=>{if(!current)return;const [h,u]=await Promise.all([api('/api/v1/ai/assets/'+current.type+'/'+current.id+'/history'),api('/api/v1/ai/usage/'+current.id)]);find('#aiHistoryDetails').innerHTML='<h3>Metadata changes</h3><pre>'+esc(JSON.stringify(h.items,null,2))+'</pre><h3>Usage history</h3><pre>'+esc(JSON.stringify(u.items,null,2))+'</pre>'});
find('#aiSearchForm').onsubmit=safe(async event=>{event.preventDefault();await search()});
all('[data-ai-action]').forEach(b=>b.onclick=safe(async()=>{if(['analyze','reindex'].includes(b.dataset.aiAction)){const r=await post('/api/v1/ai/analyze',{media_type:'all',reindex:b.dataset.aiAction==='reindex'});toast(r.jobs.length+' assets queued'+(r.remaining?' · '+r.remaining+' remain for the next batch':''))}await refresh()}));
find('#aiConfigForm').onsubmit=safe(async event=>{event.preventDefault();await post('/api/v1/ai/settings',{enabled:find('#aiEnabled').checked,batch_limit:Number(find('#aiBatchLimit').value),job_timeout:Number(find('#aiTimeout').value)},'PATCH');toast('AI controls saved');await refresh()});
find('#aiViewToggle').onclick=()=>{const on=find('#aiResults').classList.toggle('sml-ai-list');find('#aiViewToggle').textContent=on?'Grid view':'List view'};
async function loadCollections(){const c=await api('/api/v1/ai/collections');find('#aiCollections').innerHTML='<option value="">Saved searches &amp; collections</option>'+c.items.map(x=>`<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');find('#aiCollections').onchange=safe(async()=>{const x=c.items.find(x=>x.id===find('#aiCollections').value);if(!x)return;find('#aiQuery').value=x.query;const f=JSON.parse(x.filters_json);all('#aiFilters [name]').forEach(el=>{if(f[el.name]!==undefined)el.value=f[el.name]});await search()})}
find('#aiSaveSearch').onclick=safe(async()=>{const name=prompt('Saved search name');if(!name)return;await post('/api/v1/ai/collections',{name,query:find('#aiQuery').value,filters:filters()});await loadCollections();toast('Dynamic collection saved')});
find('#aiReference').onchange=safe(async()=>{const file=find('#aiReference').files[0];if(!file)return;const form=new FormData();form.append('file',file);const q=await api('/api/v1/ai/reference',{method:'POST',body:form});find('#aiSearchStatus').textContent='Reference image search queued…';let attempts=0;const poll=async()=>{try{const r=await api('/api/v1/ai/reference/'+q.job_id);if(r.state==='completed'){render(r.items);find('#aiSearchStatus').textContent='Reference-image similarity';return}if(['failed','cancelled'].includes(r.state))throw new Error(r.error||r.state);if(++attempts<120)setTimeout(poll,1000);else find('#aiSearchStatus').textContent='Reference search still queued. Check Process.'}catch(e){find('#aiSearchStatus').textContent=e.message}};poll()});
let duplicateGroups=[];
find('#aiCompare').onclick=safe(async()=>{if(selected.size!==2)throw new Error('Select two assets to compare');find('#aiDuplicateResults').innerHTML='<h3>Side-by-side comparison</h3><div class="sml-ai-grid">'+Array.from(selected).map(i=>{const a=results[i];return '<article class="panel"><h4>'+esc(a.source_filename||a.id)+'</h4>'+(a.type==='video'?'<video controls src="'+esc(mediaUrl(a.content_url))+'"></video>':'<img alt="Comparison media" src="'+esc(mediaUrl(a.content_url))+'">')+'<p>Q'+a.quality+' · '+esc(a.ai_state)+'</p><p>'+esc(a.description||'')+'</p></article>'}).join('')+'</div>'});
let duplicateOffset = 0;
async function showDuplicatePage(append = false){
 if(!append) duplicateOffset = 0;
 const size = 40;
 const d = await api('/api/v1/ai/duplicates?limit='+size+'&offset='+duplicateOffset);
 const container = find('#aiDuplicateResults');
 const previousMore = container.querySelector('[data-duplicate-more]');
 if(previousMore) previousMore.remove();
 const intro = '<h3>Similar-copy review</h3><p>Exact SHA-256 and reviewable perceptual similarity. Never deletes files automatically.</p>';
 const rows = d.groups.map(g=>{
   const displayed = g.matches.slice(0,100);
   const extra = g.matches.length - displayed.length;
   return '<div class="panel"><b>Suggested best copy: '+esc(g.recommended_keep)+'</b><p>'+esc(g.reason)+'</p><p>'+esc(g.asset.id)+' ↔ '+displayed.map(x=>esc(x.asset.id)+(x.exact?' (exact)':' ('+x.similarity+')')).join(', ')+(extra?' · +'+extra+' more copies (open search to inspect)':'')+'</p></div>';
 }).join('');
 if(!append) container.innerHTML = intro + '<p id="aiDuplicateProgress" role="status"></p>' + (rows || '<p class="empty">No duplicates found in this reviewed selection.</p>');
 else container.insertAdjacentHTML('beforeend',rows);
 duplicateOffset += d.groups.length;
 const note = container.querySelector('#aiDuplicateProgress');
 if(note) note.textContent = duplicateOffset+' of '+d.total_groups+' groups shown; '+d.scanned+' media scanned'+(d.incomplete?' · Comparison budget reached; results incomplete':'')+'. '+(d.coverage_note||'');
 if(duplicateOffset < d.total_groups){
   const more = document.createElement('button');more.type='button';more.className='ghost';more.textContent='Load more duplicate groups';
   more.dataset.duplicateMore='1';more.onclick=safe(()=>showDuplicatePage(true));container.appendChild(more);
 }
}
find('#aiDuplicates').onclick=safe(()=>showDuplicatePage(false));
all('[data-ai-bulk]').forEach(b=>b.onclick=safe(async()=>{let reason='';if(b.dataset.aiBulk==='rejected'){reason=prompt('Reason for rejecting the selected assets')||'';if(!reason.trim())return}for(const i of selected){const a=results[i];if(reason)await post('/api/v1/ai/assets/'+a.type+'/'+a.id,{fields:{review_reason:reason,duplicate_decision:'reviewed'}} ,'PATCH');if(a.type==='image')await post('/api/v1/images/'+a.id,{status:b.dataset.aiBulk},'PATCH');else await post('/api/clips/bulk/status',{ids:[a.id],status:b.dataset.aiBulk})}toast('Review states saved');await search()}));
find('#aiBulkTags').onclick=safe(async()=>{if(!selected.size)return;const text=prompt('Keywords to apply (comma separated)');if(text===null)return;for(const i of selected){const a=results[i];await post('/api/v1/ai/assets/'+a.type+'/'+a.id,{fields:{keywords:text.split(',').map(x=>x.trim()).filter(Boolean)}},'PATCH')}toast('Manual keywords saved')});
find('#aiFolderInput').onchange=safe(async()=>{
 const files=Array.from(find('#aiFolderInput').files||[]),videos=files.filter(f=>/\.(mp4|mov|mkv|webm|m4v|avi)$/i.test(f.name)),images=files.filter(f=>/\.(jpe?g|png|webp)$/i.test(f.name));let done=0;
 for(const file of images){const body=new FormData();body.append('file',file);await api('/api/v1/images/upload',{method:'POST',body});find('#aiInputStatus').textContent='Imported '+(++done)+'/'+images.length+' images'}
 if(videos.length)await uploadFiles(videos);
 find('#aiInputStatus').textContent=images.length+' images and '+videos.length+' videos handled; originals stay unchanged';
});
find('#aiBatchImport').onclick=safe(async()=>{
 if(!find('#aiBatchRights').checked)throw new Error('Confirm permission before importing');
 const urls=find('#aiBatchUrls').value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
 if(!urls.length||urls.length>32)throw new Error('Enter 1 to 32 authorized URLs');
 const r=await post('/api/v1/media/imports/batch',urls.map(value=>({source:'youtube',value,rights_confirmed:true})));
 find('#aiInputStatus').textContent=r.items.length+' imports durably queued; follow Input import history';await loadImports();
});
refresh().catch(e=>{find('#aiDashboard').textContent='AI status unavailable: '+e.message});loadCollections().catch(()=>{});
setInterval(()=>{if(!document.hidden&&['processing','dashboard'].includes(state.page))refresh().catch(()=>{})},5000);
})();

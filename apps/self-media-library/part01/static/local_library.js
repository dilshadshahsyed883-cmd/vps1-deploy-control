// Extends the existing SML UI and API; uses the same authenticated app path.
async function localPost(path,body,method='POST') {return api(path,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)})}
async function loadPairReviews(){
 const host=$('#pairReviews');if(!host)return;
 // Do not interrupt a video being compared on the periodic source refresh.
 if(host.querySelector('video') && [...host.querySelectorAll('video')].some(v=>!v.paused))return;
 try{
  const all=await api('/api/local/reviews');
  host.innerHTML=all.items.filter(r=>r.state!=='cleaned').map(r=>`<article class="panel"><h3>${esc(r.filename)}</h3><button class="ghost" data-review="${esc(r.source_id)}">Review consecutive pairs</button><button class="primary" data-finalize="${esc(r.source_id)}">Retry verification &amp; cleanup</button><div data-pairs="${esc(r.source_id)}"></div></article>`).join('')||'<p>No pending pair reviews.</p>';
  host.querySelectorAll('[data-review]').forEach(b=>b.onclick=()=>showPairs(b.dataset.review));
  host.querySelectorAll('[data-finalize]').forEach(b=>b.onclick=async()=>{try{await localPost('/api/local/reviews/'+b.dataset.finalize+'/finalize');toast('Selection saved; rejected clips and original cleaned');loadPairReviews()}catch(e){toast(e.message)}});
 }catch(e){host.textContent=e.message}
}
let openedReview='';
async function showPairs(id){
 openedReview=id;
 try{
  const data=await api('/api/local/reviews/'+id),host=document.querySelector('[data-pairs="'+id+'"]');
  host.innerHTML=data.pairs.map(p=>`<div class="local-pair"><h4>${p.items.length===1?'Odd last clip':'Pair '+(p.pair_index+1)} ${p.decided?'· decision saved':''}</h4><div class="local-pair-grid">${p.items.map(c=>`<div><video controls preload="metadata" src="${esc(mediaUrl(c.clip_url))}"></video><p>${esc(c.id)} · ${fmtTime(c.start_time)} – ${fmtTime(c.end_time)} · Q${c.quality}</p><button class="${p.keep_id===c.id?'primary':'ghost'}" data-pair-index="${p.pair_index}" data-keep="${esc(c.id)}">${p.keep_id===c.id?'✓ Keep this clip':p.items.length===1?'Keep last clip':'Keep One / Discard Other'}</button></div>`).join('')}</div>${p.items.length===1?`<button class="danger-soft" data-pair-index="${p.pair_index}" data-discard="true">${p.decided&&!p.keep_id?'✓ Discard selected':'Discard last clip'}</button>`:''}</div>`).join('');
  host.querySelectorAll('[data-pair-index]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const r=await localPost('/api/local/reviews/'+id+'/pairs/'+b.dataset.pairIndex,{keep_id:b.dataset.keep||null},'PUT');toast(r.cleanup_pending?'Decision saved · '+r.detail:'Saved and verified · original and discarded clips cleaned');if(r.state==='cleaned'){openedReview='';loadPairReviews();loadClips(true)}else await showPairs(id)}catch(e){toast(e.message);b.disabled=false}});
 }catch(e){toast(e.message)}
}
// Keep the open comparison stable while source progress polls.
const refreshPairReviews=loadPairReviews;
loadPairReviews=async function(){if(openedReview&&document.querySelector('[data-pairs="'+openedReview+'"] video'))return;return refreshPairReviews()};
async function loadLocalStorage(){try{const s=await api('/api/local/storage');$('#localStorage').textContent=`Library ${fmtSize(s.library_bytes)} · VPS used ${fmtSize(s.vps_used_bytes)} · Available ${fmtSize(s.free_bytes)} · ${s.clips} clips · ${s.images} images · ${s.pending_reviews} pending reviews · ${s.sources_cleaned} sources cleaned`;}catch(e){$('#localStorage').textContent=e.message}}
for(const id of ['localFrom','localTo'])$('#'+id).onchange=()=>{loadClips(true);loadImages()};
$('#clearLibraryDates').onclick=()=>{$('#localFrom').value='';$('#localTo').value='';loadClips(true);loadImages()};
$('#selectAllImages').onclick=()=>$$('[data-image-select]').forEach(x=>x.checked=true);
$('#deleteSelectedImages').onclick=async()=>{const ids=$$('[data-image-select]:checked').map(x=>x.dataset.imageSelect);if(!ids.length)return;if(!confirm('Permanently delete '+ids.length+' selected images from VPS?'))return;try{await localPost('/api/local/delete',{media_type:'image',ids});loadImages();toast('Images deleted')}catch(e){toast(e.message)}};
$('#deleteSelectedClips').onclick=async()=>{const ids=[...state.selected];if(!ids.length)return;if(!confirm('Permanently delete '+ids.length+' selected clips from VPS?'))return;try{await localPost('/api/local/delete',{media_type:'video',ids});loadClips(true);toast('Clips deleted')}catch(e){toast(e.message)}};

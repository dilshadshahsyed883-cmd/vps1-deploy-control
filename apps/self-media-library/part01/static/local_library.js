// Extends the existing SML UI and API; uses the same authenticated app path.
async function localPost(path,body,method='POST') {return api(path,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)})}
const PAIRS_PER_BATCH = 5;
let openedReview = '';
async function loadPairReviews(){
  const host=$('#pairReviews'); if(!host)return;
  // Leave the active comparison (and its unsaved selections) untouched by polling.
  if(openedReview && host.querySelector('[data-pairs="' + openedReview + '"]'))return;
  try{
    const all=await api('/api/local/reviews');
    host.innerHTML=all.items.filter(r=>r.state!=='cleaned').map(r=>
      '<article class="panel local-review-card"><h3>'+esc(r.filename)+'</h3>'+
      '<div class="local-review-actions"><button class="ghost" data-review="'+esc(r.source_id)+'">Review consecutive pairs</button>'+
      '<button class="primary" data-finalize="'+esc(r.source_id)+'">Retry verification &amp; cleanup</button></div>'+
      '<div data-pairs="'+esc(r.source_id)+'"></div></article>'
    ).join('')||'<p>No pending pair reviews.</p>';
    host.querySelectorAll('[data-review]').forEach(b=>b.onclick=()=>showPairs(b.dataset.review));
    host.querySelectorAll('[data-finalize]').forEach(b=>b.onclick=async()=>{
      b.disabled=true;
      try{
        const r=await localPost('/api/local/reviews/'+encodeURIComponent(b.dataset.finalize)+'/finalize');
        if(r.state==='cleaned')openedReview='';
        toast('Verified review cleanup completed');
        await loadPairReviews();
      }catch(e){toast(e.message)}
      finally{b.disabled=false}
    });
  }catch(e){host.textContent=e.message}
}

async function showPairs(id, batchNumber){
  openedReview=id;
  const source=id;
  try{
    let page=Number.isInteger(batchNumber)?Math.max(0,batchNumber):0;
    const getBatch=number=>api('/api/local/reviews/'+encodeURIComponent(source)+'?offset='+(number*PAIRS_PER_BATCH)+'&limit='+PAIRS_PER_BATCH);
    let data=await getBatch(page);
    if(batchNumber===undefined && data.first_pending_pair_index!==null){
      const pendingPage=Math.floor(data.first_pending_pair_index/PAIRS_PER_BATCH);
      if(pendingPage!==page){page=pendingPage;data=await getBatch(page)}
    }
    const host=document.querySelector('[data-pairs="'+source+'"]');
    if(!host)return;
    const total=data.total_pairs, pages=Math.max(1,Math.ceil(total/PAIRS_PER_BATCH));
    const draft=new Map(),items=data.pairs;
    const reviewCard=p=>{
      const odd=p.items.length===1;
      return '<section class="local-pair" data-local-pair="'+p.pair_index+'">'+
        '<div class="local-pair-heading"><h4>'+(odd?'Odd last clip':'Pair '+(p.pair_index+1))+'</h4>'+
        '<label class="local-pair-select"><input type="checkbox" data-bulk-pair="'+p.pair_index+'"> Bulk select</label>'+
        '<span class="local-pair-state" data-pair-state="'+p.pair_index+'">'+(p.decided?'Decision saved':'Pending')+'</span></div>'+
        '<div class="local-pair-grid">'+p.items.map((c,i)=>
          '<div class="local-pair-choice"><video controls preload="metadata" src="'+esc(mediaUrl(c.clip_url))+'"></video>'+
          '<p>'+esc(c.id)+' · '+fmtTime(c.start_time)+' – '+fmtTime(c.end_time)+' · Q'+esc(c.quality)+'</p>'+
          '<button class="ghost" type="button" data-pick-index="'+p.pair_index+'" data-pick-id="'+esc(c.id)+'" aria-pressed="false">'+
          (i===0?'Keep left clip':'Keep right clip')+'</button></div>'
        ).join('')+'</div>'+
        (odd?'<button class="danger-soft" type="button" data-odd-discard="'+p.pair_index+'" aria-pressed="false">Discard last clip</button>':'')+
        '</section>';
    };
    host.innerHTML='<div class="local-batch-toolbar">'+
      '<strong>Batch '+(page+1)+' of '+pages+' · up to 5 pairs</strong>'+
      '<span class="local-batch-progress" role="status" aria-live="polite">'+data.saved_pairs+' / '+total+' decisions saved overall</span>'+
      '<div class="local-batch-actions"><label><input type="checkbox" data-bulk-all> Select all 5</label>'+
      '<button class="ghost" type="button" data-bulk-keep="left">Keep Left (selected)</button>'+
      '<button class="ghost" type="button" data-bulk-keep="right">Keep Right (selected)</button></div></div>'+
      items.map(reviewCard).join('')+
      '<div class="local-batch-footer">'+
      '<button class="ghost" type="button" data-prev-batch '+(page===0?'disabled':'')+'>Previous 5</button>'+
      '<span data-batch-status></span>'+
      '<button class="primary" type="button" data-save-batch>Save decisions'+(page+1<pages?' &amp; next 5':' &amp; finish')+'</button></div>';
    const completed=p=>Boolean(p.decided)||draft.has(p.pair_index);
    const currentKeep=p=>draft.has(p.pair_index)?draft.get(p.pair_index):p.keep_id;
    const refresh=()=>{
      items.forEach(p=>{
        const selected=completed(p),keep=currentKeep(p);
        host.querySelectorAll('[data-pick-index="'+p.pair_index+'"]').forEach(b=>{
          const active=selected && keep===b.dataset.pickId;
          b.classList.toggle('primary',active);b.classList.toggle('ghost',!active);
          b.setAttribute('aria-pressed',String(active));
        });
        const discard=host.querySelector('[data-odd-discard="'+p.pair_index+'"]');
        if(discard)discard.setAttribute('aria-pressed',String(selected && keep===null));
        const state=host.querySelector('[data-pair-state="'+p.pair_index+'"]');
        if(state)state.textContent=draft.has(p.pair_index)?'Unsaved choice':(p.decided?'Decision saved':'Pending');
      });
      const ready=items.filter(completed).length,allReady=ready===items.length && items.length>0;
      host.querySelector('[data-batch-status]').textContent=ready+' / '+items.length+' ready to save';
      host.querySelector('[data-save-batch]').disabled=!allReady || (!draft.size && page+1>=pages);
      const all=host.querySelector('[data-bulk-all]'),selected=host.querySelectorAll('[data-bulk-pair]:checked').length;
      all.checked=selected===items.length && items.length>0; all.indeterminate=selected>0 && selected<items.length;
    };
    host.querySelectorAll('[data-pick-index]').forEach(b=>b.onclick=()=>{
      draft.set(Number(b.dataset.pickIndex),b.dataset.pickId);refresh();
    });
    host.querySelectorAll('[data-odd-discard]').forEach(b=>b.onclick=()=>{
      draft.set(Number(b.dataset.oddDiscard),null);refresh();
    });
    host.querySelectorAll('[data-bulk-pair]').forEach(b=>b.onchange=refresh);
    host.querySelector('[data-bulk-all]').onchange=e=>{
      host.querySelectorAll('[data-bulk-pair]').forEach(b=>b.checked=e.target.checked);refresh();
    };
    host.querySelectorAll('[data-bulk-keep]').forEach(b=>b.onclick=()=>{
      const selected=new Set([...host.querySelectorAll('[data-bulk-pair]:checked')].map(x=>Number(x.dataset.bulkPair)));
      if(!selected.size){toast('Select one or more pairs first');return}
      let skipped=0;
      items.filter(p=>selected.has(p.pair_index)).forEach(p=>{
        const choice=b.dataset.bulkKeep==='right'?p.items[1]:p.items[0];
        if(choice)draft.set(p.pair_index,choice.id);else skipped++;
      });
      if(skipped)toast('Odd last clip: choose Keep or Discard individually');
      refresh();
    });
    host.querySelector('[data-prev-batch]').onclick=()=>{
      if(draft.size && !confirm('Discard unsaved choices and view the previous five pairs?'))return;
      showPairs(source,page-1);
    };
    host.querySelector('[data-save-batch]').onclick=async()=>{
      const button=host.querySelector('[data-save-batch]');
      if(!items.every(completed)){toast('Choose Keep or Discard for every pair in this batch');return}
      button.disabled=true;
      try{
        const changes=items.filter(p=>draft.has(p.pair_index)).map(p=>({
          pair_index:p.pair_index,keep_id:draft.get(p.pair_index)
        }));
        let saved={};
        if(changes.length)saved=await localPost('/api/local/reviews/'+encodeURIComponent(source)+'/pairs/batch',{decisions:changes},'PUT');
        if(saved.state==='cleaned'){
          openedReview='';toast('All decisions saved and verified');await loadPairReviews();await loadClips(true);return;
        }
        toast('Batch decisions saved'+(saved.cleanup_pending?' · files retained safely':''));
        await showPairs(source,page+1<pages?page+1:undefined);
      }catch(e){toast('Batch not saved: '+e.message);button.disabled=false}
    };
    refresh();
  }catch(e){toast(e.message)}
}
// Polling must never wipe draft selections or reload media while comparing.
const refreshPairReviews=loadPairReviews;
loadPairReviews=async function(){
  if(openedReview && document.querySelector('[data-pairs="'+openedReview+'"]'))return;
  return refreshPairReviews();
};
async function loadLocalStorage(){try{const s=await api('/api/local/storage');$('#localStorage').textContent=`Library ${fmtSize(s.library_bytes)} · VPS used ${fmtSize(s.vps_used_bytes)} · Available ${fmtSize(s.free_bytes)} · ${s.clips} clips · ${s.images} images · ${s.pending_reviews} pending reviews · ${s.sources_cleaned} sources cleaned`;}catch(e){$('#localStorage').textContent=e.message}}
for(const id of ['localFrom','localTo'])$('#'+id).onchange=()=>{loadClips(true);loadImages()};
$('#clearLibraryDates').onclick=()=>{$('#localFrom').value='';$('#localTo').value='';loadClips(true);loadImages()};
$('#selectAllImages').onclick=()=>{
  imageRows.forEach(row=>imageSelected.add(row.id));
  document.querySelectorAll('#imagesGrid [data-image-select]').forEach(input=>input.checked=true);
  updateImageBulk();
};
$('#deleteSelectedImages').onclick=async()=>{
  const ids=[...imageSelected];
  if(!ids.length)return;
  if(!confirm('Permanently delete '+ids.length+' selected images and metadata from VPS? This cannot be undone.'))return;
  const button=$('#deleteSelectedImages');button.disabled=true;
  try{
    const result=await localPost('/api/local/delete',{media_type:'image',ids});
    await loadImages(true);
    toast(result.deleted+' images permanently deleted');
  }catch(e){toast('Unable to delete images: '+e.message)}
  finally{button.disabled=false}
};
$('#deleteSelectedClips').onclick=async()=>{const ids=[...state.selected];if(!ids.length)return;if(!confirm('Permanently delete '+ids.length+' selected clips and metadata from VPS? This cannot be undone.'))return;const button=$('#deleteSelectedClips');button.disabled=true;try{const r=await localPost('/api/local/delete',{media_type:'video',ids});await loadClips(true);toast(r.deleted+' clips permanently deleted')}catch(e){toast('Unable to delete: '+e.message)}finally{button.disabled=false}};

/* LUM_DARK_UX_CONSOLIDATED_V1 - outer Scene Builder host state bridge */
(() => {
  'use strict';
  const KEY='lum.route';
  const frame=document.getElementById('lum-host-frame');
  if(!frame) return;
  const fallback=frame.dataset.defaultSrc || '/local-upload-manager/app?embedded=1';

  function safeRoute(raw){
    try{
      const u=new URL(raw || fallback, window.location.origin);
      if(u.origin!==window.location.origin || u.pathname!=='/local-upload-manager/app') return fallback;
      const out=new URL('/local-upload-manager/app',window.location.origin);
      const tab=u.searchParams.get('tab')||'';
      if(tab) out.searchParams.set('tab',tab);
      const profile=u.searchParams.get('auto_profile_id');
      if(profile && /^\d+$/.test(profile)) out.searchParams.set('auto_profile_id',profile);
      const autoMode=u.searchParams.get('auto_mode');
      if(['engine1','engine2','shorts','short_stream'].includes(autoMode)) out.searchParams.set('auto_mode',autoMode);
      out.searchParams.set('embedded','1');
      return out.pathname+out.search;
    }catch(_){return fallback;}
  }

  let initial=fallback;
  try{initial=safeRoute(localStorage.getItem(KEY)||fallback)}catch(_){}
  if(frame.getAttribute('src')==='about:blank' || !frame.getAttribute('src')) frame.src=initial;

  window.addEventListener('message',event=>{
    if(event.origin!==window.location.origin || event.source!==frame.contentWindow) return;
    if(!event.data || event.data.type!=='lum-route-state') return;
    const route=safeRoute(event.data.route||'');
    try{localStorage.setItem(KEY,route)}catch(_){}
  });
})();

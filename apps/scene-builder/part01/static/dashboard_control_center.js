export const CONTRACT = 2;

export function mount(root, context = {}) {
  if (!root) throw new Error('Dashboard root required');
  const lifetime = new AbortController();
  const fetch = (url, options = {}) => window.fetch(url, {...options, credentials:'same-origin', signal:lifetime.signal});
  const $=id=>root.querySelector('#'+id);
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let timer=null, pending=false;
  const preferencesKey=context.identity?.username ? 'vps1.shell2.dashboard.'+encodeURIComponent(context.identity.username) : null;
  try {
    const saved=preferencesKey && JSON.parse(sessionStorage.getItem(preferencesKey)||'null');
    if(saved && typeof saved.auto==='boolean' && [5,10,30,60].includes(saved.rate)){
      $('dashboard-auto-refresh').checked=saved.auto;
      $('dashboard-refresh-rate').value=String(saved.rate);
    }
  } catch (_) {}
  const appLinks={'Scene Builder':'/queue','Auto Caption Studio':'/auto-caption/studio','Voice Studio':'/voice-studio','Auto Uploader':'/local-upload-manager','Local Upload Manager':'/local-upload-manager','Storage Manager':'/admin/storage'};
  const fmtGB=v=>Number.isFinite(Number(v))?Number(v).toFixed(1)+' GB':'—';
  const stat=(label,value,cls='')=>`<div class="app-stat ${cls}"><span>${esc(label)}</span><strong>${Number(value||0)}</strong></div>`;
  function appCard(a,detailed=false){const failed=Number(a.failed||0),active=Number(a.active||0),d=a.detail||{},online=a.available!==false,status=!online?'Offline':failed?'Attention':(active?'Active':'Ready');return `<article class="app-card ${detailed?'app-detail-card':''}"><div class="app-card-head"><h2>${esc(a.name)}</h2><span class="status-pill ${!online||failed?'warn':''}">${status}</span></div><div class="app-stats">${stat('Queued',a.waiting)}${stat('Active',a.active,'active')}${stat('Uploading',a.uploading,'upload')}${stat('Failed',a.failed,'failed')}${detailed?stat('Completed',d.completed||0):''}</div>${detailed?`<div class="app-meta"><span>Completed: <b>${Number(d.completed||0)}</b></span><span>${esc(d.service_summary||'Live source')}</span></div>`:''}<a class="open-app" href="${appLinks[a.name]||'#'}">Open ${esc(a.name)} →</a></article>`;}
  function render(data){const v=data.vps||{},disk=v.disk||{},ram=v.ram||{},svc=data.services||[],activeSvc=svc.filter(x=>x.active).length;
    $('vps-status').textContent=data.ok?'Online':'Attention';$('vps-status-note').textContent=data.ok?'System reachable':'Some checks failed';$('cpu-percent').textContent=(v.cpu_percent??0)+'%';$('cpu-cores').textContent=(v.cpu_cores||0)+' Cores';$('ram-percent').textContent=(ram.percent??0)+'%';$('ram-detail').textContent=`${fmtGB(ram.used_gb)} / ${fmtGB(ram.total_gb)}`;$('disk-percent').textContent=(disk.percent??0)+'%';$('disk-detail').textContent=`${fmtGB(disk.used_gb)} / ${fmtGB(disk.total_gb)}`;$('services-ratio').textContent=`${activeSvc} / ${svc.length}`;$('services-note').textContent=activeSvc===svc.length?'All Core Services Active':'Service attention required';
    $('overview-app-grid').innerHTML=(data.apps||[]).filter(a=>a.name!=='Storage Manager' && a.name!=='Algrow Voice').map(a=>appCard(a)).join('');
    const cpu=Number(v.cpu_percent||0),rp=Number(ram.percent||0),dp=Number(disk.percent||0);$('resource-cpu-label').textContent=cpu+'%';$('resource-ram-label').textContent=rp+'%';$('resource-disk-label').textContent=dp+'%';$('resource-cpu-bar').style.width=Math.min(100,cpu)+'%';$('resource-ram-bar').style.width=Math.min(100,rp)+'%';$('resource-disk-bar').style.width=Math.min(100,dp)+'%';$('load-average').textContent=v.load_1m??'—';$('uptime').textContent=v.uptime_human||'—';$('free-storage').textContent=fmtGB(disk.free_gb);
    const att=[];(data.apps||[]).filter(a=>a.name!=='Algrow Voice').forEach(a=>{if(Number(a.failed||0)>0)att.push({level:'bad',title:`${a.name}: ${a.failed} failed`,note:'Open app to review',count:a.failed});});if(dp>=85)att.push({level:'bad',title:`Storage usage is ${dp}%`,note:`${fmtGB(disk.free_gb)} free`,count:dp+'%'});else if(dp>=70)att.push({level:'',title:`Storage usage is ${dp}%`,note:`${fmtGB(disk.free_gb)} free`,count:dp+'%'});svc.filter(s=>!s.active).forEach(s=>att.push({level:'bad',title:`${s.label} is inactive`,note:s.role,count:'!'}));if(!att.length)att.push({level:'ok',title:'All core checks healthy',note:'No immediate attention required',count:'✓'});$('attention-list').innerHTML=att.slice(0,5).map(x=>`<div class="attention-item ${x.level}"><i class="attention-dot"></i><div><strong>${esc(x.title)}</strong><small>${esc(x.note)}</small></div><span class="attention-count">${esc(x.count)}</span></div>`).join('');
    const acts=data.activity||[], item=x=>`<div class="activity-item"><i class="activity-dot"></i><div><strong>${esc(x.title)}</strong><small>${esc(x.detail||'')}</small></div><span class="activity-time">${esc(x.when||'')}</span></div>`;$('activity-preview').innerHTML=acts.length?acts.slice(0,5).map(item).join(''):'<div class="empty-state">No recent activity.</div>';



    $('dashboard-updated').textContent='Updated '+(data.generated_at_local||new Date().toLocaleTimeString());
  }
  async function refresh(){if(pending || document.hidden || lifetime.signal.aborted)return;pending=true;const btn=$('dashboard-refresh-now');btn.disabled=true;try{const r=await fetch('/api/dashboard/v1',{headers:{Accept:'application/json'},cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);render(await r.json());}catch(e){$('vps-status').textContent='Unknown';$('vps-status-note').textContent='Dashboard API unavailable';$('dashboard-updated').textContent='Refresh failed: '+e.message;}finally{pending=false;btn.disabled=false;}}
  function schedule(){
    if(preferencesKey)try{sessionStorage.setItem(preferencesKey,JSON.stringify({auto:$('dashboard-auto-refresh').checked,rate:Number($('dashboard-refresh-rate').value)}));}catch(_){}
    if(timer)clearInterval(timer);timer=null;if($('dashboard-auto-refresh').checked)timer=setInterval(refresh,Math.max(5,Number($('dashboard-refresh-rate').value||10))*1000)}
  $('dashboard-refresh-now').addEventListener('click',refresh,{signal:lifetime.signal});
  $('dashboard-refresh-rate').addEventListener('change',schedule,{signal:lifetime.signal});
  $('dashboard-auto-refresh').addEventListener('change',schedule,{signal:lifetime.signal});
  refresh();schedule();
  return {unmount(){lifetime.abort();if(timer)clearInterval(timer);timer=null;},dispose(){}};
}
if (!document.getElementById('vps1-shell-outlet')) {
  const root=document.getElementById('control-dashboard');
  if(root){const view=mount(root);window.addEventListener('pagehide',()=>view.unmount(),{once:true});}
}

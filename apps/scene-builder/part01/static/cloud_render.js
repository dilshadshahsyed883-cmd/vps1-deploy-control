/* Cloud Render V1 — admin-only Media and GPU Configuration */
(() => {
  "use strict";
  const root = document.getElementById("cloud-render");
  if (!root) return;
  const csrf = root.dataset.csrf;
  const $ = id => document.getElementById(id);
  const msg = (text, error=false) => { $('cr-message').textContent = text; $('cr-message').dataset.error = error ? "1" : "0"; };
  const api = async (path, options={}) => {
    const headers = { "X-Cloud-Render-CSRF": csrf };
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch("/api/cloud-render" + path, {credentials:"same-origin", ...options, headers:{...headers,...(options.headers||{})}});
    const type = response.headers.get("content-type")||"";
    const result = type.includes("application/json") ? await response.json() : {detail:"HTTP "+response.status};
    if (!response.ok) throw Error(typeof result.detail === "string" ? result.detail : "Request failed ("+response.status+")");
    return result;
  };
  const send = (path, body={}) => api(path,{method:"POST",body:JSON.stringify(body)});
  const fmt = n => {
    const num=Number(n||0);
    return num>=1e9?(num/1e9).toFixed(2)+" GB":num>=1e6?(num/1e6).toFixed(1)+" MB":Math.round(num/1024)+" KB";
  };
  const button = (label, action, variant="secondary") => {
    const b = document.createElement("button");
    b.type="button"; b.className="cr-btn "+variant; b.textContent=label;
    b.addEventListener("click",action);
    return b;
  };
  let configured=false;
  let config={};
  async function loadConfig(){
    config = await api("/config");
    configured=true;
    $("cr-auto").checked=!!config.auto_upload;
    $("cr-image").value=config.image||"Not published — cloud_render/concat/gpu/Dockerfile";
    $("cr-endpoint").value=config.endpoint||"";
    $("cr-health-path").value=config.health_path||"/health/ready";
    $("cr-timeout").value=config.timeout||300;
    $("cr-priority").value=config.priority||"high";
    $("cr-salad-key-state").textContent=config.salad_key_saved?"Saved":"Not saved";
    $("cr-worker-key-state").textContent=config.worker_key_saved?"Saved":"Not saved";
    const details=[
      ["Engine",config.engine],["GPU",config.gpu],["VRAM",(config.vram_gb||8)+" GB"],
      ["CPU",config.cpu+" vCPU"],["RAM",config.ram_gb+" GB"],
      ["Storage",config.storage_gb+" GB"],["Replicas",String(config.replicas)],
      ["Architecture",config.architecture],["Port",String(config.port)],
      ["Codec",config.codec],["Concurrency",String(config.concurrency)]
    ];
    const container=$("cr-requirements");container.replaceChildren();
    details.forEach(([title,value])=>{
      const div=document.createElement("div");const span=document.createElement("span");span.textContent=title;
      const strong=document.createElement("strong");strong.textContent=value;div.append(span,strong);container.appendChild(div);
    });
    const online=!!config.enabled;
    $("cr-health").textContent=online?"Worker enabled":"Worker not connected";
    $("cr-connection").textContent=online?"Enabled — verify worker":"Image not GPU-verified";
  }
  const operations = async (label,callback) => {
    try {msg(label+"..."); await callback();msg(label+" successful");await refresh();}
    catch(e){msg(e.message,true);}
  };
  const refresh = async()=>{
    const data=await api("/media");
    $("cr-count").textContent=data.items.length;
    $("cr-ready").textContent=data.items.filter(x=>["ready","failed"].includes(x.state)).length;
    $("cr-delivered").textContent=data.items.filter(x=>x.state==="delivered").length;
    const list=$("cr-media-list");
    list.replaceChildren();
    if(!data.items.length){const p=document.createElement("p");p.textContent="No rendered MP4 files received yet.";list.appendChild(p);return;}
    for(const file of data.items){
      const card=document.createElement("article");card.className="cr-media-item";
      const head=document.createElement("div");head.className="cr-media-head";
      const name=document.createElement("strong");name.textContent=file.name;
      const sub=document.createElement("small");
      sub.textContent=fmt(file.size)+" · "+file.state+" · "+(file.drive_path?"Google Drive verified":"VPS");
      head.append(name,sub);card.appendChild(head);
      if(file.error){const warn=document.createElement("p");warn.className="cr-error";warn.textContent=file.error;card.appendChild(warn);}
      const actions=document.createElement("div");actions.className="cr-actions";
      const busy=["queued","uploading","receiving","verified"].includes(file.state);
      const local=["ready","failed","queued","uploading"].includes(file.state);
      if(local){
        actions.append(button("Preview",()=>window.open("/api/cloud-render/media/"+encodeURIComponent(file.id)+"/file","_blank","noopener,noreferrer")));
        actions.append(button("Download",()=>{window.location.href="/api/cloud-render/media/"+encodeURIComponent(file.id)+"/file?download=1";}));
      }
      if(["ready","failed"].includes(file.state)){
        actions.append(button(file.state==="failed"?"Retry Drive Upload":"Upload to Drive",
          ()=>operations("Drive upload queued",()=>send("/media/"+encodeURIComponent(file.id)+"/drive")),"primary"));
        actions.append(button("Rename",async()=>{
          const name=window.prompt("New MP4 filename",file.name);
          if(name===null)return;
          await operations("Rename",()=>send("/media/"+encodeURIComponent(file.id)+"/rename",{name}));
        }));
      }
      const remove=button("Delete",async()=>{
        if(!window.confirm("Permanently delete "+file.name+" from VPS? This does not delete a Drive copy."))return;
        await operations("Delete",()=>api("/media/"+encodeURIComponent(file.id),{method:"DELETE"}));
      },"danger");
      remove.disabled=busy;
      actions.append(remove);card.appendChild(actions);list.append(card);
    }
  };
  document.querySelectorAll(".cr-tabs button").forEach(b=>b.addEventListener("click",()=>{
    document.querySelectorAll(".cr-tabs button").forEach(x=>{
      const active=x===b;x.classList.toggle("active",active);x.setAttribute("aria-selected",String(active));
    });
    $("cr-media").hidden=b.dataset.tab!=="media";$("cr-gpu").hidden=b.dataset.tab!=="gpu";
  }));
  $("cr-refresh").onclick=()=>refresh().catch(e=>msg(e.message,true));
  $("cr-save-auto").onclick=()=>operations("Preference saved",async()=>{
    await send("/config",{auto_upload:$("cr-auto").checked});await loadConfig();
  });
  $("cr-save-gpu").onclick=()=>operations("GPU config saved",async()=>{
    const obj={endpoint:$("cr-endpoint").value.trim(),health_path:$("cr-health-path").value.trim(),
      timeout:Number($("cr-timeout").value),priority:$("cr-priority").value};
    if($("cr-salad-key").value) obj.salad_key=$("cr-salad-key").value;
    if($("cr-worker-key").value) obj.worker_key=$("cr-worker-key").value;
    await send("/config",obj);
    $("cr-salad-key").value="";$("cr-worker-key").value="";
    await loadConfig();
  });
  $("cr-test").onclick=async()=>{
    try {
      msg("Testing worker connection...");
      const result=await send("/gpu/test");
      msg(result.ok?"GPU worker reachable":"GPU worker unavailable: "+result.status,!result.ok);
    }catch(e){msg(e.message,true);}
  };
  $("cr-connect").onclick=()=>operations("Worker connection",async()=>{
    const result=await send("/gpu/connect");if(!result.ok)throw Error("Worker "+result.status);
    await loadConfig();
  });
  $("cr-disconnect").onclick=()=>operations("Worker disconnected",async()=>{
    await send("/gpu/disconnect");await loadConfig();
  });
  // Same GPU Config connection controls Concat upload. Salad keys stay on VPS.
  // The editor remains the source of native concat.json and its media bundle.
  let lastGpuJob="";
  const hex=bytes=>Array.from(new Uint8Array(bytes),x=>x.toString(16).padStart(2,"0")).join("");
  async function currentJob(){
    if(!lastGpuJob){msg("No CUDA project submitted in this browser session",true);return;}
    const status=await api("/gpu/jobs/"+encodeURIComponent(lastGpuJob));
    const detail=status.error ? " — "+status.error : "";
    $("cr-project-status").textContent="GPU job "+lastGpuJob+": "+status.state+detail;
    if(status.state==="delivered" || status.state==="rendered"){
      await refresh();
    }
  }
  $("cr-check-job").onclick=()=>currentJob().catch(e=>msg(e.message,true));
  $("cr-submit-project").onclick=async()=>{
    const control=$("cr-submit-project");
    const file=$("cr-project-zip").files[0];
    if(!file || !/\.zip$/i.test(file.name)){msg("Select a Concat project ZIP first",true);return;}
    if(file.size<1 || file.size>512*1024*1024){
      msg("Initial GPU Config upload supports ZIP files up to 512 MiB; no upload started",true);
      return;
    }
    if(!config.enabled){msg("Connect existing Salad worker in GPU Config first",true);return;}
    control.disabled=true;
    try{
      $("cr-project-status").textContent="Calculating full project SHA256 locally...";
      // Browser WebCrypto calculates digest once. Each network request stays
      // <=8 MiB; the already-established Salad worker handles resume safely.
      let bytes=await file.arrayBuffer();
      const checksum=hex(await crypto.subtle.digest("SHA-256",bytes));
      bytes=null;
      const job="concat-cuda-"+checksum.slice(0,24);
      lastGpuJob=job;
      let session;
      try{
        session=await send("/gpu/uploads/"+job,
          {size:file.size,sha256:checksum,render_mode:"cuda-stage1"});
      }catch(e){
        // Same ZIP/job could already be submitted. Never create duplicate work.
        if(String(e.message).includes("409")){
          await currentJob();
          return;
        }
        throw e;
      }
      if(!Array.isArray(session.uploaded) || !Number.isInteger(session.chunk_size) ||
          session.chunk_size<1 || session.chunk_size>8388608){
        throw Error("Salad worker returned invalid chunk manifest");
      }
      const done=new Set(session.uploaded);
      const total=Math.ceil(file.size/session.chunk_size);
      for(let index=0;index<total;index++){
        if(done.has(index))continue;
        const part=file.slice(index*session.chunk_size,(index+1)*session.chunk_size);
        const content=await part.arrayBuffer();
        const chunkHash=hex(await crypto.subtle.digest("SHA-256",content));
        const response=await fetch(
          "/api/cloud-render/gpu/uploads/"+encodeURIComponent(job)+"/chunks/"+index,{
            method:"PUT",credentials:"same-origin",
            headers:{"X-Cloud-Render-CSRF":csrf,"X-Content-SHA256":chunkHash,
                     "Content-Type":"application/octet-stream"},
            body:content
          });
        if(!response.ok){
          const data=await response.json().catch(()=>({detail:"Upload failed"}));
          throw Error(typeof data.detail==="string"?data.detail:"Chunk "+index+" rejected");
        }
        $("cr-project-status").textContent="Uploaded "+(index+1)+"/"+total+
          " verified chunks (resumable).";
      }
      await send("/gpu/uploads/"+job+"/complete");
      $("cr-project-status").textContent="Project verified; CUDA render queued: "+job;
      await currentJob();
    }catch(e){msg(e.message,true);
      $("cr-project-status").textContent="Upload/render submission failed: "+e.message+
        ". No Concat source media was modified.";
    }finally{control.disabled=false;}
  };
  window.setInterval(()=>{
    if(lastGpuJob&&!document.hidden && !$("cr-gpu").hidden)
      currentJob().catch(e=>{ $("cr-project-status").textContent="Job status: "+e.message; });
  },5000);
  $("cr-rotate").onclick=async()=>{
    if(!window.confirm("Generate a new receiver token? Existing Salad workers will need the new token."))return;
    try {const result=await send("/token/rotate");$("cr-token").hidden=false;
      $("cr-token").textContent=result.receiver_token;msg("New receiver token shown once. Copy securely to Salad worker.");
    }catch(e){msg(e.message,true);}
  };
  Promise.all([loadConfig(),refresh()]).catch(e=>msg(e.message,true));
  // Refresh the visible list to surface queued/verified delivery states.
  window.setInterval(()=>{if(configured&&!document.hidden&&!$("cr-media").hidden)refresh().catch(()=>{});},12000);
})();
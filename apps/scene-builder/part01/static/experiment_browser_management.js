/* EXP-UX-20261021-02: app-scoped profile management. */
(() => {
"use strict";
const root=document.getElementById("experiment-browser");
const bridge=window.antidetectManagementBridge;
if(!root||!bridge)return;
const el=id=>document.getElementById(id);
let groups=[],templates=[],csvRows=[],page=1;
const picked=new Set();
function message(text,failed=false){
 el("exp-manager-alert").textContent=text;
 el("exp-manager-alert").style.color=failed?"#b42318":"#475467";
 bridge.notice(text,failed);
}
async function job(fn){try{await fn();}catch(error){message(error.message||String(error),true);}}
function choices(id,items,first){
 const field=el(id),value=field.value;
 field.replaceChildren(new Option(first,""));
 for(const x of items)field.add(new Option(x.name,x.id));
 field.value=Array.from(field.options).some(o=>o.value===value)?value:"";
}
function list(container,items,description){
 const target=el(container);target.replaceChildren();
 for(const item of items){
  const row=document.createElement("div"),name=document.createElement("strong"),small=document.createElement("small");
  row.className="exp-management-item";name.textContent=item.name;small.textContent=description(item);
  row.append(name,small);target.append(row);
 }
 if(!items.length)target.textContent="Nothing saved yet.";
}
async function refreshData(){
 const [a,b]=await Promise.all([bridge.api("/groups"),bridge.api("/templates")]);
 groups=a.groups||[];templates=b.templates||[];
 for(const id of ["exp-create-group","exp-edit-group","exp-batch-group","exp-bulk-group"])
  choices(id,groups,"Ungrouped");
 choices("exp-filter-group",groups,"All groups");
 choices("exp-group-pick",groups,"Create new group");
 choices("exp-template-pick",templates,"New template");
 choices("exp-batch-template",templates,"Standard defaults");
 list("exp-groups-list",groups,g=>(g.profile_count||0)+" profiles · "+(g.proxy_server||"Direct"));
 list("exp-template-list",templates,t=>(t.settings.locale||"en-US")+" · "+(t.settings.privacy_mode||"strict"));
 filter();
}
function tabs(tab){
 root.dataset.expTab=tab;
 el("exp-original-panels").hidden=["batch","groups"].includes(tab);
 el("exp-batch-pane").hidden=tab!=="batch";
 el("exp-groups-pane").hidden=tab!=="groups";
 root.querySelectorAll('[role="tab"][data-exp-tab]').forEach(button=>{
  const active=button.dataset.expTab===tab;
  button.setAttribute("aria-selected",String(active));button.tabIndex=active?0:-1;
 });
}
root.querySelectorAll('[role="tab"][data-exp-tab]').forEach(button=>{
 button.addEventListener("click",()=>tabs(button.dataset.expTab));
 button.addEventListener("keydown",event=>{
  if(!["ArrowRight","ArrowLeft"].includes(event.key))return;
  const buttons=Array.from(root.querySelectorAll('[role="tab"][data-exp-tab]'));
  const index=(buttons.indexOf(button)+(event.key==="ArrowRight"?1:-1)+buttons.length)%buttons.length;
  tabs(buttons[index].dataset.expTab);buttons[index].focus();
 });
});
tabs("profiles");
function filter(){
 const profiles=bridge.profiles(),list=el("browser-profiles");
 const rows=Array.from(list.querySelectorAll(".experiment-profile"));
 if(profiles.length!==rows.length)return;
 const query=el("exp-search").value.trim().toLowerCase(),group=el("exp-filter-group").value;
 const status=el("exp-filter-status").value,sort=el("exp-sort").value;
 const items=rows.map((row,i)=>{
  if(!row.dataset.expProfileId)row.dataset.expProfileId=profiles[i].id;
  return {profile:profiles.find(p=>p.id===row.dataset.expProfileId),row};
 });
 for(const {profile,row} of items){
  if(!row.querySelector(".exp-row-check")){
   const check=document.createElement("input");check.type="checkbox";check.className="exp-row-check";
   check.setAttribute("aria-label","Bulk select "+profile.name);
   check.addEventListener("change",()=>{if(check.checked)picked.add(profile.id);else picked.delete(profile.id);filter();});
   row.prepend(check);
  }
  row.querySelector(".exp-row-check").checked=picked.has(profile.id);
 }
 items.sort((a,b)=>sort==="name"?a.profile.name.localeCompare(b.profile.name):
  sort==="recent"?Number(b.profile.last_used||0)-Number(a.profile.last_used||0):
  Number(b.profile.created_at||0)-Number(a.profile.created_at||0));
 const matches=items.filter(({profile:p})=>(!group||p.group_id===group)&&(!status||p.state===status)&&
  (!query||[p.name,p.tags,p.proxy_server].some(x=>String(x||"").toLowerCase().includes(query))));
 const pages=Math.max(1,Math.ceil(matches.length/10));page=Math.min(page,pages);
 const active=new Set(matches.slice((page-1)*10,page*10).map(x=>x.profile.id));
 for(const {profile,row} of items){row.hidden=!active.has(profile.id);list.append(row);}
 el("exp-page-label").textContent="Page "+page+" / "+pages+" · "+matches.length+" profiles";
 el("exp-prev").disabled=page<=1;el("exp-next").disabled=page>=pages;
 el("exp-selected-count").textContent=picked.size+" selected";
}
document.addEventListener("antidetect:render",filter);
["exp-search","exp-filter-group","exp-filter-status","exp-sort"].forEach(id=>
 el(id).addEventListener("input",()=>{page=1;filter();}));
el("exp-prev").addEventListener("click",()=>{page--;filter();});
el("exp-next").addEventListener("click",()=>{page++;filter();});
el("exp-select-page").addEventListener("click",()=>{
 for(const x of el("browser-profiles").querySelectorAll(".experiment-profile:not([hidden]) .exp-row-check")){x.checked=true;x.dispatchEvent(new Event("change"));}
});
el("exp-clear-selection").addEventListener("click",()=>{picked.clear();filter();});
el("exp-bulk-move").addEventListener("click",()=>job(()=>bulk("move")));
el("exp-bulk-delete").addEventListener("click",()=>job(()=>bulk("delete")));
async function bulk(action){
 const ids=[...picked];if(!ids.length)throw Error("Select profiles first.");
 if(action==="delete"&&!confirm("Delete selected STOPPED profiles and their browsing data?"))return;
 await bridge.api("/profiles/bulk","POST",{action,profile_ids:ids,group_id:el("exp-bulk-group").value});
 picked.clear();await bridge.sync();message("Bulk "+action+" completed.");
}

function loadEdit(){
 const item=bridge.profiles().find(x=>x.id===bridge.selected());
 if(!item)throw Error("Select a profile.");
 for(const [id,key] of Object.entries({"exp-edit-name":"name","exp-edit-url":"start_url",
  "exp-edit-locale":"locale","exp-edit-timezone":"timezone","exp-edit-proxy":"proxy_server",
  "exp-edit-privacy":"privacy_mode","exp-edit-group":"group_id","exp-edit-tags":"tags"}))
  el(id).value=item[key]||"";
}
el("exp-load-edit").addEventListener("click",()=>job(async()=>{loadEdit();message("Profile loaded for editing.");}));
el("exp-edit-form").addEventListener("submit",event=>{
 event.preventDefault();job(async()=>{
  const id=bridge.selected();if(!id)throw Error("Select a profile.");
  await bridge.api("/profiles/"+id,"PATCH",Object.fromEntries(new FormData(event.currentTarget).entries()));
  await bridge.sync(id);message("Profile updated; existing cookies and proxy credentials preserved.");
 });
});
function loadGroup(){
 const group=groups.find(x=>x.id===el("exp-group-pick").value);
 el("exp-group-name").value=group?.name||"";
 el("exp-group-proxy").value=group?.proxy_server||"";
}
el("exp-group-load").addEventListener("click",loadGroup);
el("exp-group-form").addEventListener("submit",event=>{
 event.preventDefault();job(async()=>{
  const id=el("exp-group-pick").value;
  await bridge.api("/groups"+(id?"/"+id:""),id?"PATCH":"POST",
   {name:el("exp-group-name").value,proxy_server:el("exp-group-proxy").value});
  await refreshData();await bridge.sync();
  el("exp-group-pick").value="";loadGroup();message("Group saved.");
 });
});
el("exp-group-delete").addEventListener("click",()=>job(async()=>{
 const id=el("exp-group-pick").value;if(!id)throw Error("Select a group.");
 if(!confirm("Remove group? Its profiles and storage will be preserved."))return;
 await bridge.api("/groups/"+id,"DELETE");
 el("exp-group-pick").value="";loadGroup();await refreshData();await bridge.sync();
 message("Group deleted. Profiles are ungrouped, not deleted.");
}));
function settingsFromForm(){
 const data=Object.fromEntries(new FormData(el("browser-create-form")).entries());
 for(const key of ["name","proxy_username","proxy_password","tags","group_id"])delete data[key];
 return data;
}
function loadTemplate(){
 const tpl=templates.find(x=>x.id===el("exp-template-pick").value);
 el("exp-template-name").value=tpl?.name||"";
 if(!tpl)return;
 const form=el("browser-create-form");
 for(const [name,value] of Object.entries(tpl.settings)){
  const field=Array.from(form.elements).find(x=>x.name===name);
  if(field)field.value=String(value);
 }
 message("Template settings loaded into the Create Profile form on the first tab.");
}
el("exp-template-load").addEventListener("click",loadTemplate);
el("exp-template-form").addEventListener("submit",event=>{
 event.preventDefault();job(async()=>{
  const id=el("exp-template-pick").value;
  await bridge.api("/templates"+(id?"/"+id:""),id?"PATCH":"POST",
   {name:el("exp-template-name").value,settings:settingsFromForm()});
  await refreshData();message("Template saved without proxy credentials.");
 });
});
el("exp-template-delete").addEventListener("click",()=>job(async()=>{
 const id=el("exp-template-pick").value;if(!id)throw Error("Select a template.");
 if(!confirm("Delete template? Existing profiles will not change."))return;
 await bridge.api("/templates/"+id,"DELETE");await refreshData();
 message("Template deleted.");
}));

function readCSV(source){
 const records=[];let fields=[],value="",quoted=false;
 for(let i=0;i<source.length;i++){
  const char=source[i];
  if(char==='"'){
   if(quoted&&source[i+1]==='"'){value+='"';i++;}
   else quoted=!quoted;
  }else if(char===","&&!quoted){fields.push(value);value="";}
  else if((char==="\n"||char==="\r")&&!quoted){
   if(char==="\r"&&source[i+1]==="\n")i++;
   fields.push(value);value="";
   if(fields.some(x=>x.trim()))records.push(fields);
   fields=[];
  }else value+=char;
 }
 if(quoted)throw Error("Unclosed CSV quote.");
 fields.push(value);if(fields.some(x=>x.trim()))records.push(fields);
 if(records.length<2||records.length>101)throw Error("CSV supports 1–100 data rows.");
 const header=records.shift().map(x=>x.trim().toLowerCase());
 const valid=new Set(["name","proxy_server","locale","timezone","start_url","tags","privacy_mode","user_agent"]);
 if(!header.includes("name")||new Set(header).size!==header.length||header.some(x=>!valid.has(x)))
  throw Error("CSV header must contain name and supported profile columns.");
 return records.map((values,index)=>{
  if(values.length!==header.length)throw Error("Wrong number of cells on row "+(index+1));
  return Object.fromEntries(header.map((key,j)=>[key,values[j].trim()]));
 });
}
el("exp-csv-file").addEventListener("change",event=>job(async()=>{
 const file=event.currentTarget.files[0];if(!file)return;
 if(file.size>128000)throw Error("CSV is too large (128 KB maximum).");
 csvRows=readCSV(await file.text());
 el("exp-batch-count").value=String(csvRows.length);
 el("exp-csv-status").textContent=csvRows.length+" CSV rows ready (no profiles created yet).";
}));
el("exp-clear-csv").addEventListener("click",()=>{
 csvRows=[];el("exp-csv-file").value="";
 el("exp-csv-status").textContent="CSV cleared; generated names will be used.";
});
el("exp-batch-form").addEventListener("submit",event=>{
 event.preventDefault();job(async()=>{
  const count=Number(el("exp-batch-count").value);
  if(csvRows.length&&csvRows.length!==count)throw Error("CSV row count must match batch count.");
  const button=el("exp-batch-submit");button.disabled=true;
  el("exp-batch-results").textContent="Creating "+count+" independent profiles...";
  try{
   const result=await bridge.api("/batches","POST",{
    count,prefix:el("exp-batch-prefix").value,start_index:Number(el("exp-batch-start").value),
    template_id:el("exp-batch-template").value,group_id:el("exp-batch-group").value,
    proxy_server:el("exp-batch-proxy").value,csv_rows:csvRows
   });
   await bridge.sync();
   el("exp-batch-results").textContent="Created "+result.created_count+" of "+result.requested+
    "; failures: "+result.failed_count+"\n"+
    result.errors.map(x=>"Row "+x.row+": "+x.error).join("\n");
   message(result.failed_count?"Batch completed with validation errors; review results.":"Batch created.",
    result.failed_count>0);
  }finally{button.disabled=false;}
 });
});
function csvValue(value){
 let text=String(value||"");
 if(/^[=+\-@]/.test(text))text="'"+text;
 return '"'+text.replace(/"/g,'""')+'"';
}
el("exp-export-csv").addEventListener("click",()=>{
 const cols=["name","proxy_server","locale","timezone","start_url","tags","privacy_mode","user_agent"];
 const lines=[cols.join(",")];
 for(const profile of bridge.profiles())lines.push(cols.map(key=>csvValue(profile[key])).join(","));
 const blob=new Blob(["\ufeff"+lines.join("\r\n")],{type:"text/csv;charset=utf-8"});
 const url=URL.createObjectURL(blob),a=document.createElement("a");
 a.href=url;a.download="browser-profiles.csv";a.click();
 setTimeout(()=>URL.revokeObjectURL(url),1500);
});
refreshData().catch(error=>message(error.message,true));
})();

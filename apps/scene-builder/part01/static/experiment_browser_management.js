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
 const items=profiles.map((profile,i)=>({profile,row:rows[i]}));
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

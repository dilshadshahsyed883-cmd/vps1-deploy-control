//! Concat v0.2.8 native MCP JSON-RPC. No hand-edited concat.json.
use crate::{auto_edit::{self,Options},library::{Catalog,Result},plan::{Plan,preflight,parse_srt}};
use serde_json::{json,Value};
use std::{collections::BTreeMap,fs,io::{BufRead,BufReader,Write},path::{Path,PathBuf},
          process::{Child,ChildStdin,ChildStdout,Command,Stdio}};

pub struct Concat {
    child:Child,
    input:ChildStdin,
    output:BufReader<ChildStdout>,
    next:u64,
}
impl Concat {
    pub fn start(exe:&Path,roots:&[PathBuf])->Result<Self>{
        if !exe.is_file(){return Err("Concat.exe is missing".into());}
        let mut cmd=Command::new(exe);
        cmd.args(["--mcp","headless"])
            .env("CONCAT_MCP_ROOTS",std::env::join_paths(roots.iter())?)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000);
        }
        let mut child=cmd.spawn()?;
        let input=child.stdin.take().ok_or("Concat input pipe missing")?;
        let output=BufReader::new(child.stdout.take().ok_or("Concat output pipe missing")?);
        let mut api=Self{child,input,output,next:0};
        let _=api.request("initialize",json!({
            "protocolVersion":"2025-11-25","capabilities":{},
            "clientInfo":{"name":"Concat Studio Native","version":"0.1.0"}}))?;
        api.send(&json!({"jsonrpc":"2.0","method":"notifications/initialized"}))?;
        Ok(api)
    }
    fn send(&mut self,msg:&Value)->Result<()>{
        writeln!(self.input,"{}",serde_json::to_string(msg)?)?;
        self.input.flush()?;Ok(())
    }
    fn request(&mut self,method:&str,params:Value)->Result<Value>{
        self.next+=1;let id=self.next;
        self.send(&json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}))?;
        let mut line=String::new();
        loop{
            line.clear();
            if self.output.read_line(&mut line)?==0{return Err("Concat closed the automation pipe".into());}
            let reply:Value=serde_json::from_str(&line)?;
            if reply.get("id").and_then(Value::as_u64)!=Some(id){continue;}
            if let Some(error)=reply.get("error"){return Err(format!("Concat protocol error: {error}").into());}
            return Ok(reply["result"].clone());
        }
    }
    pub fn tool(&mut self,name:&str,args:Value)->Result<Value>{
        let response=self.request("tools/call",json!({"name":name,"arguments":args}))?;
        if response["isError"]==true{
            return Err(format!("Concat {name}: {}",response["content"]).into());
        }
        if let Some(value)=response.get("structuredContent").filter(|v|!v.is_null()) {
            return Ok(value.clone());
        }
        // Catalogue methods return a JSON array in their MCP text block.
        response["content"].as_array().into_iter().flatten()
            .filter_map(|block|block["text"].as_str())
            .find_map(|body|serde_json::from_str(body).ok())
            .ok_or_else(||format!("Concat {name} returned no JSON response").into())
    }
}
impl Drop for Concat{
    fn drop(&mut self) {
        let _=self.child.kill();
        let _=self.child.wait();
    }
}

fn video_settings(plan:&Plan)->Value{
    json!({"width":plan.width,"height":plan.height,"rateNum":plan.fps,"rateDen":1})
}
pub fn build_project(exe:&Path,plan:&Plan,root:&Path,narration:Option<&Path>,
        srt:Option<&Path>)->Result<PathBuf>{
    let issues=preflight(plan);
    if !issues.is_empty(){return Err(format!("Scene preflight failed: {}",issues.join("; ")).into());}
    if !root.is_dir(){return Err("Project destination folder must exist".into());}
    let name=plan.name.trim();
    if name.is_empty()||name=="."||name==".." ||
       name.chars().any(|c|r#"<>:"/\|?*"#.contains(c)||c.is_control()) {
        return Err("Unsafe project name".into());
    }
    let dest=root.join(name);
    if dest.exists(){return Err("Project exists; refusing overwrite".into());}
    let captions=if let Some(path)=srt {
        parse_srt(&fs::read_to_string(path)?,plan.duration())?
    }else{vec![]};
    let mut paths=vec![fs::canonicalize(root)?];
    for shot in &plan.shots {
        if let Some(media)=&shot.file { if let Some(parent)=media.parent(){
            paths.push(fs::canonicalize(parent)?);
        }}
    }
    if let Some(path)=narration{
        if !path.is_file(){return Err("Narration file is missing".into());}
        if let Some(parent)=path.parent(){paths.push(fs::canonicalize(parent)?);}
    }
    let mut api=Concat::start(exe,&paths)?;
    api.tool("project_create",json!({"location":root.to_string_lossy(),
        "name":name,"video":video_settings(plan)}))?;
    let result=(||->Result<()>{
        let summary=api.tool("timeline_read",json!({"path":dest}))?;
        let visual=summary["timeline"]["tracks"].as_array()
            .and_then(|a|a.last()).and_then(|t|t["id"].as_str())
            .ok_or("Concat project lacks visual track")?.to_owned();
        let mut imported:BTreeMap<PathBuf,Value>=BTreeMap::new();
        for (index,shot) in plan.shots.iter().enumerate(){
            let path=shot.file.as_ref().ok_or("Unresolved media")?;
            let canon=fs::canonicalize(path)?;
            let item=if let Some(m)=imported.get(&canon){m.clone()}else{
                let reply=api.tool("media_import",json!({"path":dest,"file":canon}))?;
                let identity=reply["media"].as_array()
                    .and_then(|items|items.iter().find(|m|m["path"].as_str()
                        .is_some_and(|raw|Path::new(raw)==canon)))
                    .ok_or("Native Concat media import not found")?.clone();
                imported.insert(canon.clone(),identity.clone());identity
            };
            let id=item["id"].as_str().ok_or("Native media has no ID")?;
            let duration=item["duration"].as_f64();
            if shot.kind=="video" && duration.is_some_and(
                |d|shot.source_in+shot.duration()>d+0.05){
                return Err(format!("Scene {} exceeds video source length",index+1).into());
            }
            let placed=api.tool("clip_add",json!({"path":dest,"mediaId":id,
                "trackId":visual,"start":shot.start,"ripple":false}))?;
            let clip=placed["createdId"].as_str().ok_or("Concat failed to add clip")?.to_owned();
            let base=placed["timeline"]["clips"].as_array()
                .and_then(|a|a.iter().find(|c|c["id"]==clip))
                .and_then(|c|c["duration"].as_f64()).ok_or("Missing clip duration")?;
            let mut remaining=base;
            if shot.source_in>0.0 {
                api.tool("clip_trim",json!({"path":dest,"clipId":clip,"edge":"start",
                    "delta":shot.source_in,"ripple":true}))?;
                remaining-=shot.source_in;
            }
            if (shot.duration()-remaining).abs()>0.001 {
                api.tool("clip_trim",json!({"path":dest,"clipId":clip,
                    "edge":"end","delta":shot.duration()-remaining,"ripple":false}))?;
            }
            if narration.is_some()&&shot.kind=="video" {
                api.tool("clip_update",json!({"path":dest,"clipId":clip,"patch":{"muted":true}}))?;
            }
            let view=api.tool("timeline_read",json!({"path":dest}))?;
            let verified=view["timeline"]["clips"].as_array()
                .and_then(|a|a.iter().find(|c|c["id"]==clip)).ok_or("Native clip vanished")?;
            let actual_start=verified["start"].as_f64().ok_or("Clip start missing")?;
            let actual_duration=verified["duration"].as_f64().ok_or("Clip duration missing")?;
            if (actual_start-shot.start).abs()>0.04||
               (actual_duration-shot.duration()).abs()>0.04 {
                return Err(format!("Concat timing mismatch in scene {}",index+1).into());
            }
        }
        if let Some(path)=narration {
            let imported=api.tool("media_import",json!({"path":dest,"file":fs::canonicalize(path)?}))?;
            let voice=imported["media"].as_array()
                .and_then(|list|list.iter().find(|m|m["path"].as_str()
                .is_some_and(|p|Path::new(p)==fs::canonicalize(path).unwrap_or_default())))
                .ok_or("Narration import failed")?;
            let id=voice["id"].as_str().ok_or("Narration id missing")?;
            let lane=api.tool("track_add",json!({"path":dest,"bottom":true}))?;
            let track=lane["createdId"].as_str().ok_or("Narration track missing")?;
            let added=api.tool("clip_add",json!({"path":dest,"mediaId":id,
                "trackId":track,"start":0.0,"ripple":false}))?;
            let cid=added["createdId"].as_str().ok_or("Narration clip missing")?;
            let actual=added["timeline"]["clips"].as_array()
                .and_then(|a|a.iter().find(|c|c["id"]==cid))
                .and_then(|c|c["duration"].as_f64()).ok_or("Narration length unavailable")?;
            if actual>plan.duration() {
                api.tool("clip_trim",json!({"path":dest,"clipId":cid,
                    "edge":"end","delta":plan.duration()-actual,"ripple":false}))?;
            }
        }
        if !captions.is_empty(){
            api.tool("captions_add",json!({"path":dest,"captions":captions,"offsetY":0.35}))?;
        }
        let inline:Vec<Value>=plan.shots.iter().filter(|s|!s.text.is_empty())
            .map(|s|json!({"start":s.start,"end":s.end,"text":s.text})).collect();
        if !inline.is_empty(){
            api.tool("captions_add",json!({"path":dest,"captions":inline,"offsetY":-0.30}))?;
        }
        api.tool("project_save",json!({"path":dest}))?;
        Ok(())
    })();
    let _=api.tool("project_close",json!({"path":dest,"save":false}));
    result?;
    if !dest.join("concat.json").is_file(){return Err("Concat did not save project".into());}
    Ok(dest)
}

pub fn render_pc(exe:&Path,project:&Path,output:&Path,
    should_stop:&dyn Fn()->bool, progress:&dyn Fn(&str))->Result<String>{
    if output.exists(){return Err("MP4 exists: refusing overwrite".into());}
    let roots=vec![fs::canonicalize(project)?,fs::canonicalize(project.parent().ok_or("No parent")?)?,
                   fs::canonicalize(output.parent().ok_or("No output folder")?)?];
    let mut api=Concat::start(exe,&roots)?;
    api.tool("project_open",json!({"path":project}))?;
    let render=(||->Result<String>{
        let started=api.tool("export_start",json!({"path":project,"output":output,
            "codec":"h264"}))?;
        let job=started["job"].as_str().ok_or("Native render returned no job id")?;
        let id=job.to_owned();
        for _ in 0..3600 {
            if should_stop(){
                let _=api.tool("export_cancel",json!({"job":id}));
                return Err("Render cancelled by operator".into());
            }
            let state=api.tool("export_wait",json!({"job":id,"timeoutSeconds":1}))?;
            let name=state["state"].as_str().unwrap_or("unknown");
            progress(name);
            match name {
                "done"=>return Ok(crate::library::verify_mp4(output)?),
                "failed"|"cancelled"=>return Err(format!("Native render {name}: {}",state["error"]).into()),
                "running"=>{},
                _=>return Err("Unexpected native render state".into()),
            }
        }
        Err("Native render poll time exceeded".into())
    })();
    let _=api.tool("project_close",json!({"path":project,"save":false}));
    render
}

pub fn read_timeline(exe:&Path,project:&Path)->Result<Value>{
    let roots=vec![fs::canonicalize(project)?,fs::canonicalize(project.parent().ok_or("No parent")?)?];
    let mut api=Concat::start(exe,&roots)?;
    api.tool("project_open",json!({"path":project}))?;
    let result=api.tool("timeline_read",json!({"path":project}));
    let _=api.tool("project_close",json!({"path":project,"save":false}));
    result
}


/// Apply native atomic edits or render a temporary preview without saving.
/// Both targets use the same native concat.json; no GPU-only JSON fork exists.
pub fn auto_edit_project(exe:&Path,project:&Path,options:&Options,
                         selected:Option<&str>,preview:Option<&Path>)->Result<usize>{
    let project=fs::canonicalize(project)?;
    if !project.join("concat.json").is_file(){return Err("Select a saved Concat project first".into());}
    let mut roots=vec![project.clone(),fs::canonicalize(project.parent().ok_or("No project parent")?)?];
    if let Some(file)=preview {
        let parent=file.parent().ok_or("Preview output directory unavailable")?;
        fs::create_dir_all(parent)?;
        roots.push(fs::canonicalize(parent)?);
    }
    let mut api=Concat::start(exe,&roots)?;
    let action=(||->Result<usize>{
        api.tool("project_open",json!({"path":project}))?;
        if options.transition=="cross-fade" {
            let available=api.tool("effects_list",json!({"kind":"transition"}))?;
            if !available.as_array().is_some_and(|items|items.iter().any(|item|item["id"]=="cross-fade")) {
                return Err("Official Concat catalogue does not provide cross-fade".into());
            }
        }
        let mut timeline=api.tool("timeline_read",json!({"path":project}))?;
        // Ask the official engine for framing scale before planning motion.
        // Undo the staging batch; the final edits remain one atomic step.
        if options.animation!="None" && ["Fit","Fill"].contains(&options.scale.as_str()) {
            let frames:Vec<Value>=auto_edit::visual_clips(&timeline).iter()
                .filter(|c|selected.is_none_or(|id|c["id"]==id))
                .map(|c|json!({"op":"frameClip","clipId":c["id"],
                    "framing":options.scale.to_ascii_lowercase()})).collect();
            if !frames.is_empty(){
                let staged=api.tool("edit_apply",json!({"path":project,
                    "command":{"op":"batch","commands":frames}}))?;
                let framed=api.tool("timeline_read",json!({"path":project}));
                if staged["canUndo"]==true {api.tool("edit_undo",json!({"path":project}))?;}
                timeline=framed?;
            }
        }
        let edits=auto_edit::commands(&timeline,options,selected)?;
        let amount=edits.len();
        api.tool("edit_apply",json!({"path":project,
            "command":{"op":"batch","commands":edits}}))?;
        if let Some(file)=preview {
            let clips=auto_edit::visual_clips(&timeline);
            let reference=selected.and_then(|id|clips.iter().find(|c|c["id"]==id))
                .or_else(||clips.first()).ok_or("Preview image/video not found")?;
            let middle=reference["start"].as_f64().unwrap_or(0.0)
                + reference["duration"].as_f64().unwrap_or(0.0)*0.5;
            // Return a real Concat-composited frame, never a mock screenshot.
            api.tool("preview_frame",json!({"path":project,"time":middle,
                "width":800,"height":450,"output":file}))?;
            api.tool("edit_undo",json!({"path":project}))?;
        } else {
            api.tool("project_save",json!({"path":project}))?;
        }
        Ok(amount)
    })();
    let _=api.tool("project_close",json!({"path":project,"save":false}));
    action
}

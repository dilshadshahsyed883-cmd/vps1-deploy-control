//! Acceptance against the unmodified packaged official engine, never a mock API.
use crate::{auto_edit::Options, library::{Catalog,Result},mcp,plan,queue::Queue};
use serde_json::{json,Value};
use std::{fs,path::Path};

fn ensure(ok:bool,message:&str)->Result<()> {
    if ok {Ok(())} else {Err(message.to_owned().into())}
}
fn clips(document:&Value)->Result<Vec<Value>> {
    let timelines=document["timelines"].as_array().ok_or("Missing native timelines")?;
    let active=timelines.iter().find(|t|t["id"]==document["activeTimelineId"])
        .ok_or("Missing active timeline")?;
    Ok(active["clips"].as_array().ok_or("Missing native clips")?.clone())
}
fn document(root:&Path)->Result<Value>{Ok(serde_json::from_slice(&fs::read(root.join("concat.json"))?)?)}

pub fn run(engine:&Path,root:&Path)->Result<()> {
    fs::create_dir_all(root)?;
    let result=core(engine,root);
    if let Err(error)=&result {
      if !root.join("acceptance.json").exists() {
        fs::write(root.join("acceptance.json"),serde_json::to_vec_pretty(&json!({
            "native_editing":"failed","error":error.to_string(),"salad_gpu":"not_tested"
        }))?)?;
      }
    }
    result
}
fn core(engine:&Path,root:&Path)->Result<()> {
    let root=fs::canonicalize(root)?;
    let mut catalog=Catalog::open(&root.join("library.sqlite"))?;
    catalog.add(&root.join("a.png"))?;
    catalog.add(&root.join("b.png"))?;
    let source="name=Acceptance\nresolution=720p\nfps=24\n00:00 --> 00:02 | image | a.png\n00:02 --> 00:04 | image | b.png";
    let mut scene=plan::parse(source)?;
    ensure(plan::bind(&mut scene,&catalog.assets()?).is_empty(),"Synthetic media matching failed")?;
    // Keep the actual render tiny; plan import itself has tested standard settings.
    scene.width=320;scene.height=180;
    let project=mcp::build_project(engine,&scene,&root,Some(&root.join("voice.wav")),None)?;
    let initial=document(&project)?;
    let visual=clips(&initial)?.into_iter().filter(|c|c["kind"]=="image").collect::<Vec<_>>();
    ensure(visual.len()==2,"Native project must contain two real image clips")?;
    let selected=visual[0]["id"].as_str().ok_or("No actual clip ID")?;
    let other=visual[1]["id"].as_str().ok_or("No second clip ID")?;
    let mut options=Options{transition:"None".into(),transition_seconds:0.4,
        animation:"Zoom In".into(),animation_seconds:1.0,scale:"Keep".into()};
    mcp::auto_edit_project(engine,&project,&options,Some(selected),None)?;
    let saved=document(&project)?;
    let after=clips(&saved)?;
    ensure(after.iter().find(|c|c["id"]==other)==Some(&visual[1]),"Apply Selected modified an unrelated clip")?;
    let edited=after.iter().find(|c|c["id"]==selected).ok_or("Selected clip lost")?;
    ensure(edited["keys"].as_array().is_some_and(|keys|keys.len()>=2),"Image motion produced no real keyframes")?;
    let reopened=mcp::read_timeline(engine,&project)?;
    ensure(reopened["timeline"]["clips"].as_array().is_some_and(|items|
        items.iter().any(|c|c["id"]==selected&&c["keyframed"].as_array().is_some_and(|k|k.contains(&json!("scale"))))),
        "Saved keyframes did not survive engine reopen")?;
    options.animation="None".into();options.scale="110%".into();options.transition="cross-fade".into();
    mcp::auto_edit_project(engine,&project,&options,None,None)?;
    let all=document(&project)?;
    let second=clips(&all)?.into_iter().find(|c|c["id"]==other).ok_or("Second clip lost")?;
    ensure(second["transitionIn"]["id"]=="cross-fade","Real transition did not persist")?;
    ensure((second["scale"].as_f64().unwrap_or(0.0)-1.1).abs()<0.001,"Scale did not persist")?;
    let before=fs::read(project.join("concat.json"))?;
    options.transition="None".into();options.scale="105%".into();
    let preview=mcp::auto_edit_project(engine,&project,&options,Some(other),Some(&root.join("preview.png")));
    ensure(before==fs::read(project.join("concat.json"))?,"Preview modified the saved native project")?;
    let preview_result=match preview {
        Ok(_)=>{
            let picture=image::open(root.join("preview.png"))?.to_rgb8();
            let shows_selected_media=picture.pixels().any(|pixel|
                pixel[2]>80 && pixel[2]>pixel[0].saturating_add(20));
            if shows_selected_media {json!({"state":"passed","selected_media_pixels":"verified"})}
            else {json!({"state":"blocked","error":"Preview does not show the selected blue fixture media"})}
        },
        Err(e)=>json!({"state":"blocked","error":e.to_string()})
    };
    options.transition="unsupported".into();
    ensure(mcp::auto_edit_project(engine,&project,&options,None,None).is_err(),"Unsupported effect accepted")?;
    ensure(before==fs::read(project.join("concat.json"))?,"Rejected effect modified saved project")?;
    let queue=Queue::open(root.join("queue.sqlite"))?;
    let id=queue.add(&project,&root.join("final.mp4"),"pc")?;
    let restarted=Queue::open(root.join("queue.sqlite"))?;
    ensure(restarted.get(&id)?.state=="queued","Queue failed restart persistence")?;
    let output=root.join("final.mp4");
    let render_result=match mcp::render_pc(engine,&project,&output,&||false,&|_|{}) {
        Ok(digest)=>{
            ensure(catalog.record_verified_render(&project,&output)?==2,"Render did not count used media")?;
            ensure(catalog.record_verified_render(&project,&output)?==0,"Repeated verification counted media twice")?;
            catalog.add(&output)?;
            let mut mixed=plan::parse("name=Mixed\n00:00 --> 00:01 | image | a.png\n00:01 --> 00:02 | video | final.mp4")?;
            // Matching avoids prior-use selection policy for this deliberate fixture reuse.
            mixed.shots[0].file=Some(root.join("a.png"));mixed.shots[1].file=Some(output.clone());
            mixed.width=320;mixed.height=180;mixed.fps=24;
            let mixed_project=mcp::build_project(engine,&mixed,&root,None,None)?;
            let mixed_output=root.join("mixed.mp4");
            mcp::render_pc(engine,&mixed_project,&mixed_output,&||false,&|_|{})?;
            json!({"state":"passed","sha256":digest,"mixed_image_video":"passed","usage_deduplication":"passed"})
        },
        Err(e)=>json!({"state":"blocked","error":e.to_string()})
    };
    let render_passed=render_result["state"]=="passed";
    let preview_passed=preview_result["state"]=="passed";
    fs::write(root.join("acceptance.json"),serde_json::to_vec_pretty(&json!({
        "engine":"official Concat v0.2.8", "native_editing":"passed",
        "selected_scope":"passed","transitions":"passed","save_reopen":"passed",
        "queue_persistence":"passed","preview_preserves_saved_project":"passed",
        "preview":preview_result,"pc_render":render_result,"salad_gpu":"not_tested"
    }))?)?;
    ensure(preview_passed,"Real engine preview failed; see acceptance.json")?;
    ensure(render_passed,"Real PC render failed; see acceptance.json")?;
    Ok(())
}

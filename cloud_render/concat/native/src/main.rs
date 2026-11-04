//! Concat Studio Native: Rust + Slint 4-tab Windows desktop.
//! Intentionally no Python runtime, Tkinter, PyInstaller or fake timeline.
use concat_studio_native::{auto_edit::{self,Options},gpu,library::Catalog,mcp,plan::{self,Plan},queue::{self,Queue}};
use slint::{ComponentHandle,ModelRc,SharedString,VecModel};
use std::{cell::RefCell,fs,path::{Path,PathBuf},rc::Rc,
    sync::{mpsc,Arc,atomic::{AtomicBool,Ordering}},
    thread,time::Duration};
slint::include_modules!();

enum Event{
    Status(String),Project(PathBuf),Editor(String),Gpu(String),RefreshLibrary,
    AutoClips(serde_json::Value),AutoDone(String),AutoPreview(PathBuf),
}
struct State{
    plan:Option<Plan>,
    catalog_file:PathBuf,
    queue:Queue,
    projects_root:PathBuf,
    settings_file:PathBuf,
}
fn string(path:&Path)->SharedString{path.to_string_lossy().to_string().into()}
fn err(ui:&App,e:impl std::fmt::Display){ui.set_status(format!("Error: {e}").into());}
fn info(ui:&App,message:impl Into<String>){ui.set_status(message.into().into());}
fn rows<T,F>(items:&[T],mut mapper:F)->ModelRc<ItemRow>
where F:FnMut(&T)->ItemRow {
    let all:Vec<ItemRow>=items.iter().map(&mut mapper).collect();
    ModelRc::from(Rc::new(VecModel::from(all)))
}
fn show_scene_rows(ui:&App,plan:&Plan){
    let rendered=rows(&plan.shots,|shot|{
        let media=shot.file.as_ref().map(|p|p.file_name()
            .unwrap_or_default().to_string_lossy().to_string())
            .unwrap_or_else(||format!("UNMATCHED: {}",shot.requested));
        ItemRow{id:shot.section.clone().into(),
            title:format!("{:8.3}–{:8.3}s | {:5} | {:40} | in={:.3}s {}",
                shot.start,shot.end,shot.kind,media,shot.source_in,shot.warning).into()}
    });
    ui.set_scenes(rendered);
    ui.set_plan_info(format!("{} scenes  ·  {:.3} sec  ·  {}×{}  ·  {} FPS  ·  {} warnings",
        plan.shots.len(),plan.duration(),plan.width,plan.height,plan.fps,
        plan::preflight(plan).len()).into());
}
fn refresh_assets(ui:&App,path:&Path){
    match Catalog::open(path).and_then(|catalog|catalog.assets()){
        Ok(assets)=>{
            let filter=ui.get_media_filter().to_string();
            let showing=assets.iter().filter(|asset|match filter.as_str(){
                "Images"=>asset.kind=="image",
                "Videos"=>asset.kind=="video",
                "Used"=>asset.used>0||asset.legacy_used>0,
                "Unused"=>asset.used==0&&asset.legacy_used==0,
                "Missing"=>!asset.exists,
                _=>true,
            }).cloned().collect::<Vec<_>>();
            ui.set_assets(rows(&showing,|asset|{
                ItemRow{id:asset.id.clone().into(),
                    title:format!("{:6}  used={:<3} legacy={:<3}  {:10}   {}  | {}",
                       asset.kind,asset.used,asset.legacy_used,
                       if !asset.exists{"MISSING"}else if !asset.approved{"BLOCKED"}else{"READY"},
                       asset.filename,asset.tags).into()}
            }));
            ui.set_library_info(format!("{} indexed · {} images · {} videos · {} used · {} showing",
                assets.len(),assets.iter().filter(|x|x.kind=="image").count(),
                assets.iter().filter(|x|x.kind=="video").count(),
                assets.iter().filter(|x|x.used>0||x.legacy_used>0).count(),
                showing.len()).into());
            if let Some(item)=assets.iter().find(|a|a.id==ui.get_selected_asset().as_str()){
                ui.set_media_tags(item.tags.clone().into());
            }
        }
        Err(e)=>err(ui,e)
    }
}
fn refresh_summary(ui:&App,path:&Path){
    match Catalog::open(path).and_then(|catalog|catalog.summary()){
        Ok(report)=>ui.set_summary_text(report.into()),
        Err(e)=>err(ui,e)
    }
}
fn refresh_jobs(ui:&App,q:&Queue){
    match q.jobs(){
        Ok(items)=>{
            ui.set_jobs(rows(&items,|j|{
                ItemRow{id:j.id.clone().into(),
                    title:format!("{}   [{:10}]   {:3}   {:22}   {}",
                    j.id,j.state,j.mode,j.project.file_name()
                        .unwrap_or_default().to_string_lossy(),j.progress).into()}
            }));
            let paused=q.paused().unwrap_or(true);
            ui.set_queue_info(format!("{} · {} persisted queue items · selected {}",
                 if paused{"PAUSED: no new jobs start"}else{"RUNNING: worker accepts new jobs"},
                 items.len(),ui.get_selected_job()).into());
        }
        Err(e)=>err(ui,e)
    }
}
fn read_settings(path:&Path)->serde_json::Value{
    fs::read(path).ok().and_then(|b|serde_json::from_slice(&b).ok())
        .unwrap_or_else(||serde_json::json!({}))
}
fn save_settings(ui:&App,path:&Path)->std::result::Result<(),Box<dyn std::error::Error>>{
    let value=serde_json::json!({
        "gpu_url":ui.get_gpu_url().to_string(),
        "concat_exe":ui.get_concat_exe().to_string(),
        "projects_dir":ui.get_project_folder().to_string()
    });
    let stage=path.with_extension("tmp");
    fs::write(&stage,serde_json::to_vec_pretty(&value)?)?;
    fs::rename(stage,path)?;
    Ok(())
}
/// The verified portable ZIP is the first choice; legacy installations remain a fallback.
fn packaged_concat()->Option<PathBuf>{
    let editor=std::env::current_exe().ok()?.parent()?.join("Concat.exe");
    if editor.is_file(){Some(editor)}else{None}
}
fn picked_file(extension:&str)->Option<PathBuf>{
    rfd::FileDialog::new().add_filter(extension,&[extension]).pick_file()
}

fn main()->Result<(),Box<dyn std::error::Error + Send + Sync>>{
    if std::env::args().any(|arg|arg=="--self-test") {
        assert_eq!(plan::seconds("01:02.500")?,62.5);
        let p=plan::parse("name=Native Test\n00:00 --> 00:01 | image | a.jpg")?;
        assert_eq!(p.shots.len(),1);
        println!("concat-studio-native self-test passed");
        return Ok(());
    }
    let home=dirs::data_local_dir().unwrap_or_else(||std::env::temp_dir())
        .join("ConcatStudioNative");
    fs::create_dir_all(&home)?;
    let settings_file=home.join("settings.json");
    let settings=read_settings(&settings_file);
    let catalog_file=home.join("media.sqlite");
    let queue=Queue::open(home.join("render_queue.sqlite"))?;
    let projects_root=dirs::document_dir().unwrap_or_else(||home.clone())
        .join("Concat Studio Projects");
    fs::create_dir_all(&projects_root)?;
    let ui=App::new()?;
    ui.set_project_folder(settings["projects_dir"].as_str().map(SharedString::from)
        .unwrap_or_else(||string(&projects_root)));
    ui.set_concat_exe(packaged_concat().as_ref().map(|p|string(p))
        .or_else(||settings["concat_exe"].as_str().map(SharedString::from))
        .unwrap_or_else(||{
            let local=std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from).unwrap_or_default();
            string(&local.join("Programs/Concat/concat.exe"))
        }));
    ui.set_gpu_url(settings["gpu_url"].as_str().unwrap_or("").into());
    ui.set_render_output(string(&home.join("rendered-video.mp4")));
    let state=Rc::new(RefCell::new(State{
        plan:None,catalog_file:catalog_file.clone(),
        queue:queue.clone(),projects_root,settings_file:settings_file.clone()
    }));
    let (send,recv)=mpsc::channel::<Event>();

    {
        let weak=ui.as_weak();
        ui.on_browse_plan(move ||if let (Some(app),Some(path))=(weak.upgrade(),picked_file("txt")){
            app.set_plan_file(string(&path));
        });
    }
    {
        let weak=ui.as_weak();
        ui.on_browse_media(move ||if let (Some(app),Some(path))=(
            weak.upgrade(),rfd::FileDialog::new().pick_folder()){
            app.set_media_folder(string(&path));
        });
    }
    {
        let weak=ui.as_weak();
        ui.on_browse_voice(move ||if let (Some(app),Some(path))=(
            weak.upgrade(),rfd::FileDialog::new()
                .add_filter("Audio",&["mp3","wav","m4a","flac"]).pick_file()){
            app.set_voice_file(string(&path));
        });
    }
    {
        let weak=ui.as_weak();
        ui.on_browse_srt(move ||if let (Some(app),Some(path))=(weak.upgrade(),picked_file("srt")){
            app.set_srt_file(string(&path));
        });
    }
    {
        let weak=ui.as_weak();
        ui.on_browse_project_dir(move ||if let (Some(app),Some(path))=(
            weak.upgrade(),rfd::FileDialog::new().pick_folder()){
            app.set_project_folder(string(&path));
        });
    }
    {
        let weak=ui.as_weak();
        ui.on_browse_concat(move ||if let (Some(app),Some(path))=(
            weak.upgrade(),rfd::FileDialog::new().add_filter("Windows application",&["exe"]).pick_file()){
            app.set_concat_exe(string(&path));
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_parse_plan(move ||if let Some(app)=weak.upgrade(){
            let source=fs::read_to_string(app.get_plan_file().as_str());
            match source.and_then(|s|plan::parse(&s).map_err(std::io::Error::other)){
                Ok(mut p)=>{
                    if let Ok(lib)=Catalog::open(&state.borrow().catalog_file) {
                        if let Ok(assets)=lib.assets(){plan::bind(&mut p,&assets);}
                    }
                    show_scene_rows(&app,&p);
                    info(&app,format!("Imported {} scenes",p.shots.len()));
                    state.borrow_mut().plan=Some(p);
                }
                Err(e)=>err(&app,e)
            }
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_auto_match(move ||if let Some(app)=weak.upgrade(){
            let catalog=Catalog::open(&state.borrow().catalog_file)
                .and_then(|c|c.assets());
            match catalog {
                Ok(assets)=>{
                    if let Some(plan)=state.borrow_mut().plan.as_mut(){
                        let issues=plan::bind(plan,&assets);
                        show_scene_rows(&app,plan);
                        info(&app,if issues.is_empty(){"Media matched successfully".to_owned()}
                             else{format!("{} scenes need manual media resolution",issues.len())});
                    }else{err(&app,"Import Scene Plan first");}
                }
                Err(e)=>err(&app,e),
            }
        });
    }
    {
        let weak=ui.as_weak();let send=send.clone();let state=state.clone();
        ui.on_create_project(move ||if let Some(app)=weak.upgrade(){
            let Some(plan)=state.borrow().plan.clone() else {
                err(&app,"Load and match Scene Plan first");return;
            };
            if !plan::preflight(&plan).is_empty(){
                err(&app,format!("Preflight: {}",plan::preflight(&plan).join("; ")));return;
            }
            let exe=PathBuf::from(app.get_concat_exe().as_str());
            let target=PathBuf::from(app.get_project_folder().as_str());
            let narration=app.get_voice_file().to_string();
            let subtitles=app.get_srt_file().to_string();
            let _=save_settings(&app,&state.borrow().settings_file);
            info(&app,"Generating native editable Concat timeline...");
            let sender=send.clone();
            thread::spawn(move || {
                let result=mcp::build_project(&exe,&plan,&target,
                    if narration.is_empty(){None}else{Some(Path::new(&narration))},
                    if subtitles.is_empty(){None}else{Some(Path::new(&subtitles))});
                let _=sender.send(match result {
                    Ok(project)=>Event::Project(project),
                    Err(e)=>Event::Status(format!("Build error: {e}"))
                });
            });
        });
    }

    {
        let weak=ui.as_weak();let send=send.clone();let state=state.clone();
        ui.on_import_media(move ||if let Some(app)=weak.upgrade(){
            let dir=PathBuf::from(app.get_media_folder().as_str());
            let db=state.borrow().catalog_file.clone();
            let sender=send.clone();
            info(&app,"Importing recursive media folder into SQLite...");
            thread::spawn(move || {
                let result=Catalog::open(&db).and_then(|mut c|c.add_folder(&dir));
                let msg=match result{
                    Ok((n,errors))=>format!("Imported {n} assets; {} warnings",errors.len()),
                    Err(e)=>format!("Media import failed: {e}")
                };
                let _=sender.send(Event::Status(msg));
                let _=sender.send(Event::RefreshLibrary);
            });
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();let send=send.clone();
        ui.on_import_z_library(move ||if let Some(app)=weak.upgrade(){
            let Some(file)=rfd::FileDialog::new().add_filter("SQLite catalog",&["sqlite","db"])
                .pick_file()else{return;};
            let db=state.borrow().catalog_file.clone();
            let sender=send.clone();
            info(&app,"Importing legacy Z Builder metadata read-only...");
            thread::spawn(move||{
                let result=Catalog::open(&db).and_then(|mut c|c.import_z_builder(&file));
                let msg=match result{Ok(n)=>format!("Imported {n} Z Builder assets; legacy draft usage kept separate"),
                    Err(e)=>format!("Legacy import failed: {e}")};
                let _=sender.send(Event::Status(msg));
                let _=sender.send(Event::RefreshLibrary);
            });
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_refresh_assets(move ||if let Some(app)=weak.upgrade(){
            refresh_assets(&app,&state.borrow().catalog_file);
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_change_filter(move |filter|if let Some(app)=weak.upgrade(){
            app.set_media_filter(filter);
            refresh_assets(&app,&state.borrow().catalog_file);
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_select_asset(move |id|if let Some(app)=weak.upgrade(){
            app.set_selected_asset(id.clone());
            if let Ok(assets)=Catalog::open(&state.borrow().catalog_file).and_then(|c|c.assets()){
                if let Some(asset)=assets.iter().find(|a|a.id==id.as_str()){
                    if asset.kind=="image" && asset.exists {
                        app.set_selected_preview(slint::Image::load_from_path(&asset.path).unwrap_or_default());
                    }else{app.set_selected_preview(slint::Image::default());}
                }
            }
            refresh_assets(&app,&state.borrow().catalog_file);
        });
    }
    {
        let weak=ui.as_weak();
        ui.on_select_scene(move |id|if let Some(app)=weak.upgrade(){
            app.set_selected_scene(id);
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_assign_selected_to_scene(move ||if let Some(app)=weak.upgrade(){
            let asset_id=app.get_selected_asset().to_string();
            let scene_id=app.get_selected_scene().to_string();
            let assets=Catalog::open(&state.borrow().catalog_file).and_then(|c|c.assets());
            match assets {
                Ok(assets)=>{
                    let Some(asset)=assets.iter().find(|a|a.id==asset_id) else{
                        err(&app,"Select a Media Library item");return;
                    };
                    let mut state=state.borrow_mut();
                    let Some(plan)=state.plan.as_mut() else{
                        err(&app,"Load Scene Plan first");return;
                    };
                    let Some(index)=plan.shots.iter().position(|s|s.section==scene_id) else{
                        err(&app,"Select a scene row in Step 1 or Step 3");return;
                    };
                    match plan::manual_match(plan,index,asset){
                        Ok(_)=>{show_scene_rows(&app,plan);info(&app,"Manual media match saved in current plan");}
                        Err(e)=>err(&app,e),
                    }
                }
                Err(e)=>err(&app,e),
            }
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_open_selected_media(move ||if let Some(app)=weak.upgrade(){
            let id=app.get_selected_asset().to_string();
            match Catalog::open(&state.borrow().catalog_file).and_then(|c|c.assets()){
                Ok(assets)=>{
                    if let Some(asset)=assets.iter().find(|a|a.id==id){
                        if !asset.exists{err(&app,"Selected source media is missing");return;}
                        match opener::open(&asset.path){
                            Ok(_)=>info(&app,"Opened media with the Windows default viewer/player"),
                            Err(e)=>err(&app,e),
                        }
                    }else{err(&app,"Select a Media Library row first");}
                }
                Err(e)=>err(&app,e),
            }
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_save_tags(move ||if let Some(app)=weak.upgrade(){
            let result=Catalog::open(&state.borrow().catalog_file).and_then(
                |c|c.update_tags(app.get_selected_asset().as_str(),app.get_media_tags().as_str()));
            match result{Ok(_)=>{info(&app,"Tags saved");refresh_assets(&app,&state.borrow().catalog_file)},
                Err(e)=>err(&app,e)}
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_refresh_summary(move ||if let Some(app)=weak.upgrade(){
            refresh_summary(&app,&state.borrow().catalog_file);
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_export_summary(move ||if let Some(app)=weak.upgrade(){
            let Some(path)=rfd::FileDialog::new().set_file_name("Media_Summary.txt")
                .add_filter("Text report",&["txt"]).save_file()else{return;};
            let result=Catalog::open(&state.borrow().catalog_file)
                .and_then(|lib|lib.save_summary(&path));
            match result{Ok(_)=>info(&app,format!("Media Summary saved: {}",path.display())),
                Err(e)=>err(&app,e)}
        });
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_clear_library(move ||if let Some(app)=weak.upgrade(){
            let confirm=rfd::MessageDialog::new().set_title("Clear catalog records only?")
                .set_description("This clears imported media records and usage history. ORIGINAL PC MEDIA AND MP4 FILES ARE PRESERVED.")
                .set_buttons(rfd::MessageButtons::YesNo).show();
            if confirm!=rfd::MessageDialogResult::Yes{return;}
            let result=Catalog::open(&state.borrow().catalog_file).and_then(|lib|lib.clear_records());
            match result{Ok(n)=>{info(&app,format!("{n} catalog records cleared; originals retained"));
                refresh_assets(&app,&state.borrow().catalog_file);},
                Err(e)=>err(&app,e)}
        });
    }
    // Auto Editing: MCP runs in worker threads; GUI and media remain responsive.
    {
        let weak=ui.as_weak();let sender=send.clone();
        ui.on_auto_refresh(move ||if let Some(app)=weak.upgrade(){
            let editor=PathBuf::from(app.get_concat_exe().as_str());
            let project=PathBuf::from(app.get_current_project().as_str());
            let sender=sender.clone();
            info(&app,"Reading native Concat visual clips...");
            thread::spawn(move||{
                let result=mcp::read_timeline(&editor,&project);
                let _=sender.send(match result{
                    Ok(timeline)=>Event::AutoClips(timeline),
                    Err(e)=>Event::AutoDone(format!("Cannot read native clips: {e}. Close the same project in the official editor first."))
                });
            });
        });
    }
    {
        let weak=ui.as_weak();
        ui.on_auto_select(move |clip|if let Some(app)=weak.upgrade(){
            app.set_selected_auto_clip(clip.clone());
            app.set_auto_info(format!("Selected native clip: {clip}").into());
        });
    }
    {
        let weak=ui.as_weak();
        ui.on_auto_reset_presets(move ||if let Some(app)=weak.upgrade(){
            app.set_auto_transition("None".into());
            app.set_auto_animation("None".into());
            app.set_auto_scale("Keep".into());
            app.set_auto_info("Presets cleared; saved Concat project untouched.".into());
        });
    }
    for scope in ["all","selected","preview"] {
        let weak=ui.as_weak();let sender=send.clone();let home=home.clone();
        let callback=move ||if let Some(app)=weak.upgrade(){
            let options=Options{
                transition:app.get_auto_transition().to_string(),
                transition_seconds:app.get_auto_transition_seconds().parse().unwrap_or(f64::NAN),
                animation:app.get_auto_animation().to_string(),
                animation_seconds:app.get_auto_animation_seconds().parse().unwrap_or(f64::NAN),
                scale:app.get_auto_scale().to_string()
            };
            if let Err(e)=options.check(){err(&app,e);return;}
            let selected=if scope=="all"{None}else{
                Some(app.get_selected_auto_clip().to_string())
            };
            if selected.as_deref()==Some(""){
                err(&app,"Load native clips and select an image/video first");return;
            }
            let editor=PathBuf::from(app.get_concat_exe().as_str());
            let project=PathBuf::from(app.get_current_project().as_str());
            let preview=if scope=="preview"{Some(home.join("auto-preview.png"))}else{None};
            let sender=sender.clone();
            info(&app,if scope=="preview"{"Compositing temporary auto-edit preview..."}
                  else{"Applying atomic native Concat edit batch and saving..."});
            thread::spawn(move||{
                let result=mcp::auto_edit_project(&editor,&project,&options,
                    selected.as_deref(),preview.as_deref());
                let event=match result{
                    Ok(count)=>{
                        if let Some(path)=preview{Event::AutoPreview(path)}
                        else{Event::AutoDone(format!("Saved {count} native Concat edits; desktop and GPU share concat.json"))}
                    },
                    Err(e)=>Event::AutoDone(format!("Auto Editing failed: {e}. Project not overwritten."))
                };
                let _=sender.send(event);
            });
        };
        match scope {
            "all"=>ui.on_auto_apply_all(callback),
            "selected"=>ui.on_auto_apply_selected(callback),
            _=>ui.on_auto_preview_frame(callback),
        }
    }
    {
        let weak=ui.as_weak();
        ui.on_open_native_editor(move ||if let Some(app)=weak.upgrade(){
            let exe=PathBuf::from(app.get_concat_exe().as_str());
            if !exe.is_file(){err(&app,"Official Concat.exe not found");return;}
            match std::process::Command::new(&exe).spawn(){
                Ok(_)=>info(&app,"Native Concat editor launched; open saved project from Home"),
                Err(e)=>err(&app,e),
            }
        });
    }
    {
        let weak=ui.as_weak();let send=send.clone();
        ui.on_refresh_timeline(move ||if let Some(app)=weak.upgrade(){
            let exe=PathBuf::from(app.get_concat_exe().as_str());
            let project=PathBuf::from(app.get_current_project().as_str());
            let sender=send.clone();
            info(&app,"Reading native Concat timeline...");
            thread::spawn(move||{
                let reply=mcp::read_timeline(&exe,&project);
                let outcome=match reply{
                    Ok(v)=>{
                        let clips=v["timeline"]["clips"].as_array().cloned().unwrap_or_default();
                        let mut summary=format!("Native project: {}\n{} clips\n\n",
                            project.display(),clips.len());
                        for c in clips {
                            summary.push_str(&format!(
                                "{:.3}s — {:.3}s | {} | {}\n",c["start"].as_f64().unwrap_or(0.),
                                c["end"].as_f64().unwrap_or(0.),c["kind"],c["name"]));
                        }
                        Event::Editor(summary)
                    },
                    Err(e)=>Event::Editor(format!("Native Concat timeline unavailable: {e}\nClose the project in Concat editor before reading via MCP.")),
                };
                let _=sender.send(outcome);
            });
        });
    }
    {
        let weak=ui.as_weak();
        ui.on_browse_output(move ||if let(Some(app),Some(path))=(
            weak.upgrade(),rfd::FileDialog::new()
              .set_file_name("concat-output.mp4").add_filter("MP4",&["mp4"]).save_file()){
            app.set_render_output(string(&path));
        });
    }
    for mode in ["pc","gpu"] {
        let weak=ui.as_weak();let q=queue.clone();
        let callback=move ||if let Some(app)=weak.upgrade(){
            let project=PathBuf::from(app.get_current_project().as_str());
            let output=PathBuf::from(app.get_render_output().as_str());
            match q.add(&project,&output,mode){
                Ok(id)=>{
                    info(&app,format!("Queued {} job {}",mode.to_uppercase(),id));
                    refresh_jobs(&app,&q);
                }
                Err(e)=>err(&app,e)
            }
        };
        if mode=="pc"{ui.on_add_pc_job(callback);}else{ui.on_add_gpu_job(callback);}
    }
    {
        let weak=ui.as_weak();let q=queue.clone();
        ui.on_queue_start(move ||if let Some(app)=weak.upgrade(){
            match q.set_paused(false){Ok(_)=>info(&app,"Queue started"),Err(e)=>err(&app,e)}
            refresh_jobs(&app,&q);
        });
    }
    {
        let weak=ui.as_weak();let q=queue.clone();
        ui.on_queue_pause(move ||if let Some(app)=weak.upgrade(){
            match q.set_paused(true){Ok(_)=>info(&app,"Queue paused: active render continues"),
                Err(e)=>err(&app,e)}
            refresh_jobs(&app,&q);
        });
    }
    {
        let weak=ui.as_weak();let q=queue.clone();
        ui.on_queue_resume(move ||if let Some(app)=weak.upgrade(){
            match q.set_paused(false){Ok(_)=>info(&app,"Queue resumed"),Err(e)=>err(&app,e)}
            refresh_jobs(&app,&q);
        });
    }
    for command in ["skip","cancel","retry","delete"] {
        let weak=ui.as_weak();let q=queue.clone();
        let callback=move ||if let Some(app)=weak.upgrade(){
            match q.action(app.get_selected_job().as_str(),command){
                Ok(message)=>info(&app,message),Err(e)=>err(&app,e)
            }
            refresh_jobs(&app,&q);
        };
        match command {
            "skip"=>ui.on_queue_skip(callback),
            "cancel"=>ui.on_queue_cancel(callback),
            "retry"=>ui.on_queue_retry(callback),
            _=>ui.on_queue_delete(callback),
        }
    }
    {
        let weak=ui.as_weak();let q=queue.clone();
        ui.on_select_job(move |id|if let Some(app)=weak.upgrade(){
            app.set_selected_job(id);
            refresh_jobs(&app,&q);
        });
    }
    {
        let weak=ui.as_weak();let q=queue.clone();
        ui.on_refresh_jobs(move ||if let Some(app)=weak.upgrade(){refresh_jobs(&app,&q);});
    }
    {
        let weak=ui.as_weak();let state=state.clone();
        ui.on_save_gpu(move ||if let Some(app)=weak.upgrade(){
            let salad=app.get_gpu_salad_key().to_string();
            let renderer=app.get_gpu_renderer_key().to_string();
            let result=gpu::store_credentials(&salad,&renderer)
                .and_then(|_|save_settings(&app,&state.borrow().settings_file)
                          .map_err(|e|format!("{e}").into()));
            match result{
                Ok(_)=>{
                    app.set_gpu_salad_key("".into());app.set_gpu_renderer_key("".into());
                    info(&app,"Gateway URL saved; keys stored securely in OS Credential Manager");
                }
                Err(e)=>err(&app,e),
            }
        });
    }
    {
        let weak=ui.as_weak();let send=send.clone();
        ui.on_test_gpu(move ||if let Some(app)=weak.upgrade(){
            let url=app.get_gpu_url().to_string();
            let sender=send.clone();
            info(&app,"Checking Salad GPU and Vulkan drivers...");
            thread::spawn(move||{
                let result=gpu::gateway_saved(&url).and_then(|g|g.check_nvidia());
                let report=match result{
                    Ok(d)=>format!("GPU ready\nNVIDIA Vulkan: {}\nNVIDIA Compute: {}\nAdapters: {}",
                         d["nvidia_vulkan_visible"],d["nvidia_compute_visible"],d["vulkan_adapters"]),
                    Err(e)=>format!("GPU not ready: {e}")
                };
                let _=sender.send(Event::Gpu(report));
            });
        });
    }
    {
        let weak=ui.as_weak();let send=send.clone();let q=queue.clone();
        ui.on_show_gpu_job(move ||if let Some(app)=weak.upgrade(){
            let url=app.get_gpu_url().to_string();
            let id=app.get_selected_job().to_string();
            let sender=send.clone();
            let remote=q.get(&id).ok().and_then(|j|j.remote_id).unwrap_or(id);
            thread::spawn(move||{
                let result=gpu::gateway_saved(&url).and_then(|g|g.status(&remote));
                let msg=match result{Ok(v)=>format!("Salad GPU job {remote}:\n{}",
                    serde_json::to_string_pretty(&v).unwrap_or_default()),
                    Err(e)=>format!("GPU job check unavailable: {e}")};
                let _=sender.send(Event::Gpu(msg));
            });
        });
    }

    refresh_assets(&ui,&catalog_file);
    refresh_summary(&ui,&catalog_file);
    refresh_jobs(&ui,&queue);
    let shutdown=Arc::new(AtomicBool::new(false));
    // Each job reads latest settings; credential values stay in the OS vault.
    queue::spawn_scheduler(queue.clone(),settings_file.clone(),
                           catalog_file.clone(),shutdown.clone());
    let weak=ui.as_weak();
    let tick=slint::Timer::default();
    let q=queue.clone();let db=catalog_file.clone();
    tick.start(slint::TimerMode::Repeated,Duration::from_millis(300),move||{
        if let Some(app)=weak.upgrade() {
            while let Ok(ev)=recv.try_recv(){
                match ev {
                    Event::Status(s)=>info(&app,s),
                    Event::Project(path)=>{
                        app.set_current_project(string(&path));
                        app.set_tab(1);
                        info(&app,format!("Created native editable Concat project: {}",path.display()));
                    }
                    Event::Editor(s)=>app.set_editor_info(s.into()),
                    Event::Gpu(s)=>app.set_gpu_info(s.into()),
                    Event::AutoDone(message)=>{
                        app.set_auto_info(message.clone().into());
                        info(&app,message);
                    }
                    Event::AutoPreview(path)=>{
                        match slint::Image::load_from_path(&path){
                            Ok(picture)=>{
                                app.set_auto_preview(picture);
                                app.set_auto_info("Preview rendered by Concat MCP; no project changes saved.".into());
                            }
                            Err(e)=>err(&app,format!("Preview image unavailable: {e}"))
                        }
                    }
                    Event::AutoClips(timeline)=>{
                        let clips=auto_edit::visual_clips(&timeline);
                        app.set_auto_clips(rows(&clips,|c|ItemRow{
                            id:c["id"].as_str().unwrap_or("").into(),
                            title:format!("{:.2}–{:.2}s  {}  {}  {}",
                                c["start"].as_f64().unwrap_or_default(),
                                c["end"].as_f64().unwrap_or_default(),
                                c["kind"].as_str().unwrap_or("media"),
                                c["name"].as_str().unwrap_or(""),
                                c["id"].as_str().unwrap_or("")).into()
                        }));
                        app.set_auto_info(format!("{} native visual clips loaded. Select one or Apply All.",clips.len()).into());
                    }
                    Event::RefreshLibrary=>{
                        refresh_assets(&app,&db);refresh_summary(&app,&db);
                    }
                }
            }
            refresh_jobs(&app,&q);
        }
    });
    let result=ui.run();
    shutdown.store(true,Ordering::Relaxed);
    result?;
    Ok(())
}

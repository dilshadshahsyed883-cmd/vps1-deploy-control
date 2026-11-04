//! Persistent render queue with explicit safe state transitions.
//! Pause stops starting NEXT jobs, and never pretends to suspend the
//! already-running encoder. A remote submitted Salad render cannot be cancelled
//! by the current worker API; Cancel/Skip detaches from it instead.
use crate::{gpu,library::{Catalog,Result},mcp};
use rusqlite::{Connection,params,OptionalExtension};
use serde::{Serialize,Deserialize};
use sha2::{Sha256,Digest};
use std::{fs,path::{Path,PathBuf},sync::atomic::{AtomicBool,Ordering},
          sync::Arc,thread,time::{Duration,SystemTime,UNIX_EPOCH}};

#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct Job {
    pub id:String,pub project:PathBuf,pub output:PathBuf,
    pub mode:String,pub state:String,pub progress:String,
    pub remote_id:Option<String>,pub attempts:u32,
}

#[derive(Clone)]
pub struct Queue {
    pub path:PathBuf,
}
impl Queue {
    pub fn open(path:PathBuf)->Result<Self>{
        if let Some(parent)=path.parent(){fs::create_dir_all(parent)?;}
        let this=Self{path};
        let db=this.db()?;
        db.execute_batch(
            "PRAGMA journal_mode=WAL;
             CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,project TEXT NOT NULL,output TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('pc','gpu')),
                state TEXT NOT NULL,progress TEXT NOT NULL DEFAULT '',
                remote_id TEXT,attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
             );
             CREATE TABLE IF NOT EXISTS options (
                name TEXT PRIMARY KEY,value TEXT NOT NULL
             );
             INSERT OR IGNORE INTO options(name,value) VALUES('paused','1');
             UPDATE jobs SET state='interrupted',
                progress='Studio restarted; review remote status before retry'
             WHERE state IN ('running','cancel_requested','skip_requested');")?;
        Ok(this)
    }
    fn db(&self)->Result<Connection>{
        let db=Connection::open(&self.path)?;
        db.busy_timeout(Duration::from_secs(10))?;
        Ok(db)
    }
    fn from_row(row:&rusqlite::Row)->rusqlite::Result<Job>{
        Ok(Job {id:row.get(0)?,project:PathBuf::from(row.get::<_,String>(1)?),
            output:PathBuf::from(row.get::<_,String>(2)?),
            mode:row.get(3)?,state:row.get(4)?,progress:row.get(5)?,
            remote_id:row.get(6)?,attempts:row.get::<_,i64>(7)?.max(0) as u32})
    }
    pub fn jobs(&self)->Result<Vec<Job>>{
        let db=self.db()?;
        let mut stmt=db.prepare("SELECT id,project,output,mode,state,progress,remote_id,attempts
                         FROM jobs ORDER BY created_at,id")?;
        Ok(stmt.query_map([],Self::from_row)?
            .collect::<std::result::Result<Vec<_>,_>>()?)
    }
    pub fn get(&self,id:&str)->Result<Job>{
        let db=self.db()?;
        db.query_row("SELECT id,project,output,mode,state,progress,remote_id,attempts
                      FROM jobs WHERE id=?1",[id],Self::from_row)
            .optional()?.ok_or_else(||"Queue item not found".into())
    }
    pub fn add(&self,project:&Path,output:&Path,mode:&str)->Result<String>{
        if !["pc","gpu"].contains(&mode){return Err("Unknown render target".into());}
        let canon=fs::canonicalize(project)?;
        if !canon.is_dir()||!canon.join("concat.json").is_file(){
            return Err("Queue requires saved Concat project".into());
        }
        if mode=="pc" && output.exists(){return Err("PC output already exists".into());}
        let nanos=SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
        let seed=format!("{}:{}:{}",canon.display(),std::process::id(),nanos);
        let id=format!("studio-{}",&hex::encode(Sha256::digest(seed.as_bytes()))[..20]);
        let db=self.db()?;
        db.execute("INSERT INTO jobs(id,project,output,mode,state,progress)
                    VALUES(?1,?2,?3,?4,'queued','Ready')",
            params![id,canon.to_string_lossy(),output.to_string_lossy(),mode])?;
        Ok(id)
    }
    pub fn paused(&self)->Result<bool>{
        let db=self.db()?;
        let pause:String=db.query_row("SELECT value FROM options WHERE name='paused'",
                [],|r|r.get(0))?;
        Ok(pause=="1")
    }
    pub fn set_paused(&self,paused:bool)->Result<()>{
        let db=self.db()?;
        db.execute("INSERT INTO options(name,value) VALUES('paused',?1)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                   [if paused{"1"}else{"0"}])?;Ok(())
    }
    pub fn claim(&self)->Result<Option<Job>>{
        let mut db=self.db()?;
        let tx=db.transaction()?;
        let paused:String=tx.query_row("SELECT value FROM options WHERE name='paused'",
                [],|r|r.get(0))?;
        if paused=="1"{tx.commit()?;return Ok(None);}
        let id:Option<String>=tx.query_row("SELECT id FROM jobs WHERE state='queued'
             ORDER BY created_at,id LIMIT 1",[],|r|r.get(0)).optional()?;
        let result=if let Some(id)=id {
            tx.execute("UPDATE jobs SET state='running',progress='Starting',
                        attempts=attempts+1,updated_at=CURRENT_TIMESTAMP
                        WHERE id=?1 AND state='queued'",[&id])?;
            Some(tx.query_row("SELECT id,project,output,mode,state,progress,remote_id,attempts
                              FROM jobs WHERE id=?1",[id],Self::from_row)?)
        }else{None};
        tx.commit()?;Ok(result)
    }
    pub fn update(&self,id:&str,message:&str)->Result<()>{
        let db=self.db()?;
        db.execute("UPDATE jobs SET progress=?1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?2 AND state IN ('running','cancel_requested','skip_requested')",
                   params![message.chars().take(350).collect::<String>(),id])?;
        Ok(())
    }
    pub fn set_remote(&self,id:&str,remote:&str)->Result<()>{
        if !gpu::valid_id(remote){return Err("Unsafe Salad job ID".into());}
        let db=self.db()?;
        db.execute("UPDATE jobs SET remote_id=?1 WHERE id=?2",
                   params![remote,id])?;
        Ok(())
    }
    pub fn stop_requested(&self,id:&str)->bool{
        self.get(id).is_ok_and(|item|["skip_requested","cancel_requested"].contains(&item.state.as_str()))
    }
    pub fn finish(&self,id:&str,state:&str,message:&str)->Result<()>{
        if !["done","failed","skipped","cancelled","detached"].contains(&state) {
            return Err("Unsafe queue terminal state".into());
        }
        let db=self.db()?;
        db.execute("UPDATE jobs SET state=?1,progress=?2,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?3",params![state,message.chars().take(500).collect::<String>(),id])?;
        Ok(())
    }
    pub fn action(&self,id:&str,action:&str)->Result<String>{
        let job=self.get(id)?;
        let db=self.db()?;
        let (state,message)=match action {
            "skip" if job.state=="queued"=>("skipped","Skipped before starting"),
            "skip" if job.state=="running"=>("skip_requested","Stopping locally if possible"),
            "cancel" if job.state=="queued"=>("cancelled","Cancelled before starting"),
            "cancel" if job.state=="running"=>("cancel_requested","Cancelling local operation"),
            "retry" if ["skipped","cancelled","failed","interrupted"].contains(&job.state.as_str())
                && job.remote_id.is_none()=>("queued","Queued for another attempt"),
            "retry" if job.remote_id.is_some()=>return Err(
                "Remote job may exist; check its status. Re-upload could double-charge".into()),
            "delete" if !["running","skip_requested","cancel_requested"].contains(&job.state.as_str())=>{
                db.execute("DELETE FROM jobs WHERE id=?1",[id])?;
                return Ok("Removed queue record; original media and rendered MP4 retained".to_string());
            }
            _=>return Err(format!("Action {action} is unsafe for {}",job.state).into()),
        };
        db.execute("UPDATE jobs SET state=?1,progress=?2,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?3",params![state,message,id])?;
        Ok(message.to_string())
    }
}

fn process_job(queue:&Queue,job:&Job,concat_exe:&Path,gpu_endpoint:&str,library_db:&Path)->Result<()>{
    if job.mode=="pc" {
        let output=&job.output;
        if output.exists(){return Err("PC output path exists".into());}
        let _digest=mcp::render_pc(concat_exe,&job.project,output,
            &||queue.stop_requested(&job.id),
            &|p|{let _=queue.update(&job.id,&format!("Native PC render: {p}"));})?;
        if queue.stop_requested(&job.id){return Err("Local PC job was cancelled".into());}
        let mut library=Catalog::open(library_db)?;
        let used=library.record_verified_render(&job.project,output)?;
        queue.finish(&job.id,"done",&format!("Verified local MP4; {used} media items recorded"))?;
        Ok(())
    } else {
        if job.remote_id.is_some() {
            return Err("Remote job was previously submitted; inspect its status".into());
        }
        let gateway=gpu::gateway_saved(gpu_endpoint)?;
        // Persist job id before HTTP begins, so retries never duplicate paid work.
        queue.set_remote(&job.id,&job.id)?;
        queue.update(&job.id,"Uploading verified native project to Salad")?;
        gateway.upload(&job.project,&job.id,
            &||queue.stop_requested(&job.id),
            &|m|{let _=queue.update(&job.id,m);})?;
        for _ in 0..1800 {
            if queue.stop_requested(&job.id){
                queue.finish(&job.id,"detached",
                    "Remote GPU job may continue; this API cannot cancel a submitted render")?;
                return Ok(());
            }
            let status=gateway.status(&job.id)?;
            let state=status["state"].as_str().unwrap_or("unknown");
            queue.update(&job.id,&format!("Salad GPU: {state}"))?;
            match state {
                "delivered"=> {
                    queue.finish(&job.id,"done","MP4 delivered and acknowledged by VPS Cloud Render")?;
                    return Ok(());
                },
                "rendered"|"delivery_failed"=> {
                    queue.finish(&job.id,"done",
                        "MP4 available on GPU; download or retry VPS delivery manually")?;
                    return Ok(());
                },
                "failed" =>return Err(format!("Salad GPU failed: {}",status["error"]).into()),
                "queued"|"running"|"delivering"=>{},
                _=>return Err(format!("Unexpected GPU state: {state}").into()),
            }
            thread::sleep(Duration::from_secs(2));
        }
        Err("GPU render still pending; use Check GPU Job".into())
    }
}
pub fn spawn_scheduler(queue:Queue,settings_file:PathBuf,library_db:PathBuf,
                       shutdown:Arc<AtomicBool>) {
    thread::spawn(move ||{
        while !shutdown.load(Ordering::Relaxed){
            if let Ok(Some(job))=queue.claim(){
                // Read latest saved UI config per job: no restart after setting
                // Concat.exe or Salad credentials. Never persist secret tokens.
                let config:serde_json::Value=fs::read(&settings_file).ok()
                    .and_then(|bytes|serde_json::from_slice(&bytes).ok())
                    .unwrap_or_else(||serde_json::json!({}));
                let exe=PathBuf::from(config["concat_exe"].as_str().unwrap_or(""));
                let gpu_endpoint=config["gpu_url"].as_str().unwrap_or("");
                let result=process_job(&queue,&job,&exe,gpu_endpoint,&library_db);
                if let Err(error)=result {
                    if let Ok(current)=queue.get(&job.id) {
                        let state=match current.state.as_str(){
                            "skip_requested"=>"skipped",
                            "cancel_requested"=>"cancelled",
                            _=>"failed"
                        };
                        let note=if job.mode=="gpu"&&current.remote_id.is_some(){
                            "Remote GPU may continue; review GPU job before any retry".to_owned()
                        }else{error.to_string()};
                        let state=if job.mode=="gpu"&&current.remote_id.is_some(){"detached"}else{state};
                        let _=queue.finish(&job.id,state,&note);
                    }
                }
            }
            thread::sleep(Duration::from_millis(400));
        }
    });
}

#[cfg(test)]
mod tests{
    use super::*;
    fn sample()->(Queue,PathBuf){
        let seed=SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let dir=std::env::temp_dir().join(format!("studioq-{}-{seed}",std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let project=dir.join("project");
        fs::create_dir_all(&project).unwrap();
        fs::write(project.join("concat.json"),br#"{"media":[]}"#).unwrap();
        (Queue::open(dir.join("jobs.sqlite")).unwrap(),project)
    }
    #[test]
    fn skip_resume_retry_cancel_delete_transitions(){
        let (q,project)=sample();
        let id=q.add(&project,&project.join("out.mp4"),"pc").unwrap();
        assert!(q.paused().unwrap());
        assert!(q.claim().unwrap().is_none());
        q.action(&id,"skip").unwrap();assert_eq!(q.get(&id).unwrap().state,"skipped");
        q.action(&id,"retry").unwrap();
        q.set_paused(false).unwrap();
        assert_eq!(q.claim().unwrap().unwrap().state,"running");
        q.action(&id,"cancel").unwrap();assert!(q.stop_requested(&id));
        q.finish(&id,"cancelled","Stopped").unwrap();
        q.action(&id,"delete").unwrap();
        assert!(project.join("concat.json").is_file());
        assert!(q.jobs().unwrap().is_empty());
    }
    #[test]
    fn no_double_billing_when_remote_exists(){
        let(q,project)=sample();let id=q.add(&project,&project.join("output.mp4"),"gpu").unwrap();
        q.set_remote(&id,&id).unwrap();
        q.finish(&id,"failed","Remote unknown").unwrap();
        assert!(q.action(&id,"retry").is_err());
    }
}

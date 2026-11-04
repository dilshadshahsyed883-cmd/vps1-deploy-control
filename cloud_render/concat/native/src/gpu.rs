//! Rust-to-Salad GPU transport. Same canonical bundle manifest as
//! cloud_render/concat/project_bundle.py; no shell, Python, or invented GPU API.
use crate::library::{Result,verify_mp4};
use reqwest::{blocking::Client,header::{HeaderMap,HeaderName,HeaderValue,AUTHORIZATION,CONTENT_TYPE}};
use serde_json::{Value,json};
use sha2::{Sha256,Digest};
use std::{collections::BTreeMap,fs,io::{Read,Seek,SeekFrom,Write},path::{Path,PathBuf},time::Duration};
use zip::{ZipWriter,write::SimpleFileOptions,CompressionMethod};

const CHUNK:usize=8*1024*1024;
const MAX_UPLOAD:u64=4*1024*1024*1024;

fn checksum(bytes:&[u8])->String{hex::encode(Sha256::digest(bytes))}
fn hash_file(path:&Path)->Result<String>{
    let mut file=fs::File::open(path)?;let mut hash=Sha256::new();
    let mut buf=vec![0u8;CHUNK];
    loop{let n=file.read(&mut buf)?;if n==0{break;}hash.update(&buf[..n]);}
    Ok(hex::encode(hash.finalize()))
}

fn origin(raw:&str)->Result<String>{
    let url=reqwest::Url::parse(raw.trim())?;
    if url.scheme()!="https" || url.host_str().is_none() ||
       !url.username().is_empty() || url.password().is_some() ||
       !url.path().is_empty() && url.path()!="/" ||
       url.query().is_some() || url.fragment().is_some() ||
       url.port().is_some_and(|p|p!=443) {
        return Err("Use a trusted HTTPS Salad Gateway domain without path or credentials".into());
    }
    Ok(format!("https://{}",url.host_str().ok_or("Missing host")?))
}

#[derive(Clone)]
pub struct Gateway {
    client:Client,
    base:String,
}
impl Gateway {
    pub fn new(endpoint:&str,salad_key:&str,renderer_token:&str)->Result<Self>{
        if salad_key.is_empty() || renderer_token.is_empty(){return Err("Both GPU credentials required".into());}
        let base=origin(endpoint)?;
        let mut headers=HeaderMap::new();
        headers.insert(HeaderName::from_static("salad-api-key"),
                       HeaderValue::from_str(salad_key)?);
        headers.insert(AUTHORIZATION,HeaderValue::from_str(&format!("Bearer {renderer_token}"))?);
        let client=Client::builder().default_headers(headers)
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(140)).build()?;
        Ok(Self{client,base})
    }
    fn get(&self,path:&str)->Result<Value>{
        let response=self.client.get(format!("{}{}",self.base,path)).send()?;
        let code=response.status();
        if !code.is_success(){return Err(format!("Salad Gateway HTTP {}",code.as_u16()).into());}
        Ok(response.json()?)
    }
    fn post_json(&self,path:&str,data:&Value)->Result<Value>{
        let response=self.client.post(format!("{}{}",self.base,path)).json(data).send()?;
        let code=response.status();
        if !code.is_success(){return Err(format!("Salad Gateway HTTP {}",code.as_u16()).into());}
        Ok(response.json()?)
    }
    fn put_chunk(&self,path:&str,bytes:Vec<u8>,digest:&str)->Result<Value>{
        let response=self.client.put(format!("{}{}",self.base,path))
            .header(CONTENT_TYPE,"application/octet-stream")
            .header("X-Content-SHA256",digest).body(bytes).send()?;
        let code=response.status();
        if !code.is_success(){return Err(format!("Salad Gateway HTTP {}",code.as_u16()).into());}
        Ok(response.json()?)
    }
    pub fn diagnose(&self)->Result<Value>{self.get("/api/diagnostics/gpu")}
    pub fn check_nvidia(&self)->Result<Value>{
        let d=self.diagnose()?;
        if d["selected_backend"]=="cuda_nvenc" {
            if d["cuda_nvenc_ready"]!=true {
                return Err(format!("Salad CUDA/NVENC unavailable: {}; no CPU fallback",
                    d["missing"]).into());
            }
        } else if d["nvidia_vulkan_visible"]!=true {
            return Err("NVIDIA Vulkan unavailable for native Concat render".into());
        }
        Ok(d)
    }
    pub fn status(&self,id:&str)->Result<Value>{
        if !valid_id(id){return Err("Invalid GPU job ID".into());}
        self.get(&format!("/api/jobs/{id}"))
    }
    pub fn logs(&self,id:&str)->Result<Value>{
        if !valid_id(id){return Err("Invalid GPU job ID".into());}
        self.get(&format!("/api/jobs/{id}/logs"))
    }
    pub fn upload(&self,project:&Path,id:&str,stop:&dyn Fn()->bool,
                  progress:&dyn Fn(&str))->Result<()> {
        if !valid_id(id){return Err("Unsafe GPU job ID".into());}
        self.check_nvidia()?;
        let archive=std::env::temp_dir().join(format!("concat-studio-{id}.zip"));
        if archive.exists(){return Err("Existing transfer archive; choose new job".into());}
        struct Cleanup(PathBuf);
        impl Drop for Cleanup {fn drop(&mut self){let _=fs::remove_file(&self.0);}}
        let _cleanup=Cleanup(archive.clone());
        pack_project(project,&archive)?;
        let size=fs::metadata(&archive)?.len();
        if size==0 || size>MAX_UPLOAD{return Err("GPU project bundle is too large".into());}
        let sha=hash_file(&archive)?;
        let route=format!("/api/uploads/{id}");
        let start=self.post_json(&route,&json!({"size":size,"sha256":sha}))?;
        let block=start["chunk_size"].as_u64().ok_or("GPU chunk size missing")? as usize;
        if block==0 || block>CHUNK{return Err("GPU returned invalid chunk size".into());}
        let total=size.div_ceil(block as u64) as usize;
        if start["total_chunks"].as_u64()!=Some(total as u64){
            return Err("GPU chunk count mismatch".into());
        }
        let mut completed=start["uploaded"].as_array().cloned().unwrap_or_default()
            .iter().filter_map(Value::as_u64).collect::<std::collections::BTreeSet<_>>();
        let mut file=fs::File::open(&archive)?;
        for i in 0..total {
            if stop(){return Err("GPU upload stopped locally; remote job not cancelled".into());}
            let mut buf=vec![0;block.min((size-(i*block) as u64) as usize)];
            file.read_exact(&mut buf)?;
            if !completed.contains(&(i as u64)) {
                let digest=checksum(&buf);
                let url=format!("{route}/chunks/{i}");
                let mut delivered=false;
                for attempt in 0..3 {
                    match self.put_chunk(&url,buf.clone(),&digest) {
                        Ok(_)=>{delivered=true;break;}
                        Err(e)=>{
                            let remote=self.get(&route)?;
                            completed=remote["uploaded"].as_array().cloned().unwrap_or_default()
                                .iter().filter_map(Value::as_u64).collect();
                            if completed.contains(&(i as u64)){delivered=true;break;}
                            if attempt==2{return Err(e);}
                        }
                    }
                }
                if !delivered{return Err("GPU upload could not confirm chunk".into());}
            }
            progress(&format!("Uploaded {}/{} chunks",i+1,total));
        }
        if stop(){return Err("GPU upload stopped before completion".into());}
        let response=self.client.post(format!("{}{route}/complete",self.base)).body(Vec::new()).send()?;
        if !response.status().is_success(){
            return Err(format!("GPU completion HTTP {}",response.status().as_u16()).into());
        }
        let result:Value=response.json()?;
        let state=result["state"].as_str().unwrap_or("");
        if !["queued","running","rendered","delivering","delivered"].contains(&state){
            return Err(format!("GPU returned invalid completion state: {state}").into());
        }
        Ok(())
    }
    pub fn download(&self,id:&str,target:&Path)->Result<String>{
        if target.exists(){return Err("Output exists: refusing overwrite".into());}
        let status=self.status(id)?;
        if status["state"]!="rendered"&&status["state"]!="delivery_failed" {
            return Err("Output is not available on GPU; inspect VPS Media for delivered jobs".into());
        }
        let expected=status["output_sha256"].as_str().ok_or("GPU checksum unavailable")?;
        let partial=target.with_extension("mp4.partial");
        if partial.exists(){return Err("Partial output already exists".into());}
        let response=self.client.get(format!("{}/api/jobs/{id}/output",self.base)).send()?;
        if !response.status().is_success(){return Err("GPU MP4 download unavailable".into());}
        let result=(||->Result<String>{
            let mut dst=fs::OpenOptions::new().write(true).create_new(true).open(&partial)?;
            let mut stream=response;
            std::io::copy(&mut stream,&mut dst)?;
            dst.flush()?;drop(dst);
            let digest=verify_mp4(&partial)?;
            if digest!=expected{return Err("GPU MP4 checksum mismatch".into());}
            fs::rename(&partial,target)?;
            Ok(digest)
        })();
        if result.is_err(){let _=fs::remove_file(partial);}
        result
    }
}
pub fn valid_id(id:&str)->bool{
    !id.is_empty() && id.len()<=80&&id.chars().all(|c|c.is_ascii_alphanumeric()||c=='-'||c=='_')
}

/// Zip layout MUST match the Python/Linux project_bundle.restore contract.
/// Stored paths are canonical, SHA256 checked by receiver before GPU rendering.
pub fn pack_project(root:&Path,target:&Path)->Result<()>{
    if target.exists(){return Err("Bundle exists".into());}
    let root=fs::canonicalize(root)?;
    let document_name=if root.join("concat.json").is_file(){"concat.json"}
        else if root.join("wolfcut.json").is_file(){"wolfcut.json"}
        else{return Err("Native Concat project missing".into())};
    let original=fs::read(root.join(document_name))?;
    let document:Value=serde_json::from_slice(&original)?;
    let media=document["media"].as_array().ok_or("Invalid project media array")?;
    if media.len()>20000{return Err("Too many media items".into());}
    let mut references=Vec::new();
    let mut members:BTreeMap<PathBuf,(String,String,u64)>=BTreeMap::new();
    let mut total=0u64;
    for (index,item) in media.iter().enumerate(){
        let raw=item["path"].as_str().unwrap_or("");
        if raw.is_empty(){continue;}
        let source=PathBuf::from(raw);
        let canon=fs::canonicalize(if source.is_absolute(){source}else{root.join(source)})?;
        if !canon.is_file() || canon.is_symlink(){return Err("Missing/unsafe project media".into());}
        let (member,digest,size)=if let Some(known)=members.get(&canon){known.clone()}else{
            let digest=hash_file(&canon)?;
            let identity=checksum(canon.to_string_lossy().as_bytes());
            let ext=canon.extension().and_then(|s|s.to_str()).unwrap_or("").to_ascii_lowercase();
            let suffix=if ext.len()<=12&&ext.chars().all(|c|c.is_ascii_alphanumeric()){
                format!(".{ext}")}else{String::new()};
            let member=format!("media/{}{}", &identity[..32],suffix);
            let size=fs::metadata(&canon)?.len();
            total=total.checked_add(size).ok_or("Size overflow")?;
            if total>MAX_UPLOAD{return Err("GPU bundle exceeds 4 GiB".into());}
            members.insert(canon.clone(),(member.clone(),digest.clone(),size));
            (member,digest,size)
        };
        references.push(json!({"index":index,"member":member,"sha256":digest,"size":size}));
    }
    let file=fs::OpenOptions::new().write(true).create_new(true).open(target)?;
    let result=(||->Result<()>{
        let mut zip=ZipWriter::new(file);
        let compressed=SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
        let stored=SimpleFileOptions::default().compression_method(CompressionMethod::Stored);
        zip.start_file("bundle.json",compressed)?;
        zip.write_all(serde_json::to_string(&json!({"version":1,"project_file":document_name,
            "references":references}))?.as_bytes())?;
        zip.start_file(format!("project/{document_name}"),compressed)?;
        zip.write_all(&original)?;
        for (path,(name,digest,size)) in members {
            if fs::metadata(&path)?.len()!=size || hash_file(&path)?!=digest{
                return Err("Media changed during GPU packaging".into());
            }
            zip.start_file(name,stored)?;
            let mut source=fs::File::open(&path)?;
            std::io::copy(&mut source,&mut zip)?;
        }
        zip.finish()?;Ok(())
    })();
    if result.is_err(){let _=fs::remove_file(target);}
    result
}

pub fn store_credentials(salad:&str,renderer:&str)->Result<()>{
    if !salad.is_empty(){keyring::Entry::new("ConcatStudio.SaladCloud.v1","salad_api_key")?
        .set_password(salad)?;}
    if !renderer.is_empty(){keyring::Entry::new("ConcatStudio.SaladCloud.v1","renderer_token")?
        .set_password(renderer)?;}
    Ok(())
}
pub fn gateway_saved(endpoint:&str)->Result<Gateway>{
    let salad=keyring::Entry::new("ConcatStudio.SaladCloud.v1","salad_api_key")?
        .get_password()?;
    let renderer=keyring::Entry::new("ConcatStudio.SaladCloud.v1","renderer_token")?
        .get_password()?;
    Gateway::new(endpoint,&salad,&renderer)
}
#[cfg(test)]
mod tests{
    use super::*;
    #[test] fn origin_checks_credentials_and_https(){
        assert!(origin("https://worker.example.com/").is_ok());
        assert!(origin("http://worker.example.com").is_err());
        assert!(origin("https://alice:secret@worker.example.com").is_err());
        assert!(origin("https://worker.example.com/api/private").is_err());
    }
    #[test] fn rejects_unsafe_ids(){assert!(!valid_id("../escape"));assert!(valid_id("studio-a12_45"));}
}

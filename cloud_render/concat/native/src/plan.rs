//! Z Builder V6.3 simple timed and SCENE_PLAN_VERSION=2 plans.
//! Exact, safe catalog matching. Used videos cannot be silently reused.
use crate::library::{Asset,kind_of,Result};
use serde::{Deserialize,Serialize};
use std::{collections::{BTreeMap,BTreeSet},fs,path::{Path,PathBuf}};

#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct Shot {
    pub section: String,
    pub start: f64,
    pub end: f64,
    pub kind: String,
    pub requested: String,
    pub tags: String,
    pub source_in: f64,
    pub text: String,
    pub file: Option<PathBuf>,
    pub warning: String,
}
impl Shot { pub fn duration(&self)->f64{ (self.end-self.start).max(0.0) } }

#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct Plan {
    pub name:String,
    pub width:u32,
    pub height:u32,
    pub fps:u32,
    pub shots:Vec<Shot>,
    pub warnings:Vec<String>,
}
impl Plan {pub fn duration(&self)->f64{self.shots.last().map(|s|s.end).unwrap_or_default()}}

fn number(value:&str)->Result<f64>{
    let n:f64=value.trim().replace(',','.').parse()?;
    if !n.is_finite(){return Err("Non-finite number".into())}
    Ok(n)
}
pub fn seconds(value:&str)->Result<f64>{
    let trimmed=value.trim();
    let normalized=trimmed.replace(',','.');
    let fields=normalized.split(':').collect::<Vec<_>>();
    if fields.is_empty() || fields.len()>3{return Err(format!("Invalid time: {value}").into());}
    let mut total=0.0;
    for (i,field) in fields.iter().enumerate(){
        if field.is_empty() || field.chars().any(|c|!c.is_ascii_digit()&&c!='.') ||
           (i<fields.len()-1 && field.contains('.')) ||
           field.matches('.').count()>1{return Err(format!("Invalid time: {value}").into());}
        let n=number(field)?;
        if i>0 && n>=60.0{return Err(format!("Timestamp component out of range: {value}").into());}
        total=total*60.0+n;
    }
    Ok((total*1000.0).round()/1000.0)
}
fn settings(name:String,res:&str,aspect:&str,fps:&str)->Result<(String,u32,u32,u32)>{
    let (mut width,mut height)=match res.trim().to_ascii_lowercase().as_str(){
        "720p"=>(1280,720),"1080p"=>(1920,1080),"4k"=>(3840,2160),
        _=>return Err("Supported resolution: 720p, 1080p, 4k".into())
    };
    match aspect.trim(){"16:9"=>{},"9:16"=>std::mem::swap(&mut width,&mut height),
        _=>return Err("Supported aspect ratio: 16:9 or 9:16".into())}
    let fps:u32=fps.trim().parse()?;
    if ![24,25,30,60].contains(&fps){return Err("FPS must be 24, 25, 30 or 60".into());}
    Ok((name,width,height,fps))
}

pub fn parse(source:&str)->Result<Plan>{
    let input=source.trim_start_matches('\u{feff}');
    if input.lines().any(|l| l.trim().eq_ignore_ascii_case("SCENE_PLAN_VERSION=2")) {
        parse_legacy(input)
    } else { parse_timed(input) }
}
fn parse_timed(source:&str)->Result<Plan>{
    let mut name="Concat Studio Project".to_owned();
    let (mut res,mut aspect,mut fps)=("1080p".to_owned(),"16:9".to_owned(),"30".to_owned());
    let mut shots=Vec::new();
    let mut cursor=0.0;
    for (line_no,raw) in source.lines().enumerate(){
        let line=raw.trim();
        if line.is_empty()||line.starts_with('#')||line.starts_with(';'){continue;}
        if !line.contains("-->"){
            if !shots.is_empty(){return Err(format!("Setting after shots on line {}",line_no+1).into());}
            let (key,val)=line.split_once('=').ok_or("Expected setting=value")?;
            match key.trim().to_ascii_lowercase().as_str(){
                "name"|"project"=>name=val.trim().to_owned(),
                "aspect"|"aspect_ratio"=>aspect=val.trim().to_owned(),
                "resolution"=>res=val.trim().to_owned(),
                "fps"=>fps=val.trim().to_owned(),
                _=>return Err(format!("Unsupported setting: {key}").into())
            }
            continue;
        }
        let mut sections=line.split('|').map(str::trim);
        let range=sections.next().ok_or("Missing time range")?;
        let (begin,end)=range.split_once("-->").ok_or("Missing arrow")?;
        let start=seconds(begin)?;
        let stop=seconds(end)?;
        if start<0.0 || stop<=start || stop-start>1800.0 || (start-cursor).abs()>0.001 {
            return Err(format!("Scene {} has gap, overlap or invalid duration",line_no+1).into());
        }
        let kind=sections.next().ok_or("Missing media type")?.to_ascii_lowercase();
        if kind!="image"&&kind!="video"{return Err("Media type must be image or video".into());}
        let requested=sections.next().ok_or("Missing scene filename")?.to_owned();
        if requested.trim().is_empty(){return Err("Empty media selector".into());}
        let(mut in_sec,mut text)=(0.0,String::new());
        for extra in sections {
            let (key,value)=extra.split_once('=').ok_or("Invalid scene option")?;
            match key.trim().to_ascii_lowercase().as_str(){
                "in"=>in_sec=number(value)?,
                "text"=>text=value.trim().to_owned(),
                _=>return Err(format!("Unsupported scene option: {key}").into())
            }
        }
        if kind=="image"&&in_sec!=0.0{return Err("Images cannot use in=".into());}
        if in_sec<0.0{return Err("Negative source in-point".into());}
        shots.push(Shot{section:format!("SCENE_{:03}.SHOT_01",shots.len()+1),
            start,end:stop,kind,requested,tags:String::new(),source_in:in_sec,
            text,file:None,warning:String::new()});
        cursor=stop;
    }
    if shots.is_empty(){return Err("Scene Plan has no timed shots".into());}
    let(name,width,height,fps)=settings(name,&res,&aspect,&fps)?;
    Ok(Plan{name,width,height,fps,shots,warnings:Vec::new()})
}
fn parse_legacy(source:&str)->Result<Plan>{
    let mut current="_ROOT".to_string();
    let mut blocks:Vec<(String,BTreeMap<String,String>)>=vec![(current.clone(),BTreeMap::new())];
    for (line_no,line) in source.lines().enumerate(){
        let s=line.trim();
        if s.is_empty()||s.starts_with('#')||s.starts_with(';'){continue;}
        if s.starts_with('[')&&s.ends_with(']'){
            current=s[1..s.len()-1].trim().to_ascii_uppercase();
            if blocks.iter().any(|(name,_)|name==&current) {
                return Err(format!("Duplicate section at line {}",line_no+1).into());
            }
            blocks.push((current.clone(),BTreeMap::new()));continue;
        }
        let (key,value)=s.split_once('=').ok_or("Legacy scene line must be key=value")?;
        let map=&mut blocks.last_mut().ok_or("No legacy block")?.1;
        if map.insert(key.trim().to_ascii_lowercase(),value.trim().to_owned()).is_some(){
            return Err("Duplicate legacy field".into());
        }
    }
    let get=|section:&str,key:&str,default:&str|->String{
        blocks.iter().find(|(n,_)|n==section).and_then(|(_,m)|m.get(key)).map(|s|s.as_str())
            .unwrap_or(default).to_owned()
    };
    if get("_ROOT","scene_plan_version","")!="2" {
        return Err("Unsupported SCENE_PLAN_VERSION".into());
    }
    let (name,width,height,fps)=settings(
        get("PROJECT","name","Concat Studio Project"),
        &get("OUTPUT","resolution","1080p"),
        &get("OUTPUT","aspect_ratio","16:9"),
        &get("OUTPUT","fps","30"))?;
    let mut shots=Vec::new();
    let mut warnings=Vec::new();
    let mut cursor=0.0;
    for (section,fields) in &blocks {
        if !section.contains(".SHOT_") ||
            !(section.starts_with("SCENE_")||section.starts_with("INTRO.")){continue;}
        let kind=fields.get("media_type").map(String::as_str).unwrap_or("video").to_ascii_lowercase();
        if kind!="image"&&kind!="video"{return Err(format!("{section} invalid media type").into());}
        let requested=fields.get("library_filename")
            .filter(|v| !["none","null","n/a","na"].contains(&v.to_ascii_lowercase().as_str()))
            .cloned().unwrap_or_default();
        let keyword=fields.get("library_keyword").cloned().unwrap_or_default();
        let (actual,duration)=if let Some(value)=fields.get("shot_duration_seconds")
                  .or_else(||fields.get("duration_seconds")){
            (requested.clone(),number(value)?)
        }else if let Some(value)=fields.get("animation_duration"){
            warnings.push(format!("{section}: provisional animation_duration; verify voice sync"));
            (requested.clone(),number(value)?)
        }else{
            warnings.push(format!("{section}: default provisional 3s; verify timing"));
            (requested.clone(),3.0)
        };
        if duration<0.1 || duration>1800.0{
            return Err(format!("{section} has invalid duration").into());
        }
        let target=if actual.is_empty(){format!("keyword:{keyword}")}else{actual};
        shots.push(Shot{section:section.clone(),start:cursor,end:cursor+duration,kind,
            requested:target,tags:keyword,source_in:0.0,text:fields.get("narration")
            .cloned().unwrap_or_default(),file:None,warning:String::new()});
        cursor+=duration;
    }
    if shots.is_empty(){return Err("Legacy plan has no shots".into());}
    Ok(Plan{name,width,height,fps,shots,warnings})
}

pub fn bind(plan:&mut Plan,assets:&[Asset])->Vec<String>{
    let mut warnings=Vec::new();
    for shot in &mut plan.shots {
        shot.file=None;shot.warning.clear();
        let keyword=shot.requested.strip_prefix("keyword:")
            .map(str::trim).or_else(|| if kind_of(Path::new(&shot.requested)).is_none() {
                Some(shot.requested.as_str())
            } else {None});
        let mut choices:Vec<&Asset>=assets.iter()
            .filter(|a|a.kind==shot.kind&&a.approved&&a.exists)
            .filter(|a| !(a.kind=="video"&&(a.used>0||a.legacy_used>0)))
            .filter(|a|{
                if let Some(keyword)=keyword {
                    let words=keyword.to_lowercase().split_whitespace()
                        .map(str::to_owned).collect::<Vec<_>>();
                    let hay=format!("{} {}",a.filename,a.tags).to_lowercase();
                    !words.is_empty()&&words.iter().all(|word|hay.contains(word))
                }else{a.filename.eq_ignore_ascii_case(shot.requested.as_str())}
            }).collect();
        choices.sort_by_key(|a|a.filename.to_ascii_lowercase());
        if choices.len()==1 {
            shot.file=Some(choices[0].path.clone());
        }else{
            shot.warning=format!("{}: {} {}",
                shot.section,shot.requested,
                if choices.is_empty(){"missing/previously used"}else{"ambiguous"});
            warnings.push(shot.warning.clone());
        }
    }
    warnings
}

pub fn manual_match(plan:&mut Plan,index:usize,asset:&Asset)->Result<()>{
    let shot=plan.shots.get_mut(index).ok_or("Invalid shot index")?;
    if shot.kind!=asset.kind || !asset.exists || !asset.approved{
        return Err("Selected media must be approved, existing and matching kind".into());
    }
    if shot.kind=="video"&&(asset.used>0||asset.legacy_used>0){
        return Err("Previously exported video is protected against reuse".into());
    }
    shot.file=Some(asset.path.clone());shot.warning.clear();Ok(())
}
pub fn preflight(plan:&Plan)->Vec<String>{
    let mut issues=plan.warnings.clone();
    let mut cursor=0.0;
    let mut seen_videos=BTreeSet::new();
    let mut previous_kind="";
    for shot in &plan.shots {
        if (shot.start-cursor).abs()>0.001{issues.push(format!("{} gap/overlap",shot.section));}
        cursor=shot.end;
        if shot.file.is_none(){issues.push(format!("{} unmatched media",shot.section));}
        if shot.kind=="video"{
            if previous_kind=="video"{issues.push(format!("{} consecutive videos disallowed by Z Studio",shot.section));}
            if let Some(file)=&shot.file {
                if !seen_videos.insert(file.clone()){
                    issues.push(format!("{} same video reused",shot.section));
                }
            }
        }
        previous_kind=&shot.kind;
        if shot.source_in<0.0 || shot.duration()<0.1 || shot.duration()>1800.0 {
            issues.push(format!("{} invalid timing",shot.section));
        }
    }
    issues
}
pub fn parse_srt(content:&str,total:f64)->Result<Vec<serde_json::Value>>{
    let normalized=content.replace("\r\n","\n");
    let mut result=Vec::new();
    for block in normalized.split("\n\n"){
        let lines=block.lines().map(str::trim).filter(|s|!s.is_empty()).collect::<Vec<_>>();
        if lines.is_empty(){continue;}
        let i=lines.iter().position(|s|s.contains("-->")).ok_or("SRT missing time range")?;
        let (left,right)=lines[i].split_once("-->").ok_or("SRT arrow missing")?;
        let start=seconds(left)?;let end=seconds(right.trim().split_whitespace().next().unwrap_or(""))?;
        let text=lines.iter().skip(i+1).copied().collect::<Vec<_>>().join(" ");
        if text.is_empty()||end<=start||end>total+0.01{return Err("SRT caption invalid/out of range".into());}
        result.push(serde_json::json!({"start":start,"end":end.min(total),"text":text}));
    }
    Ok(result)
}
pub fn write_timeline_csv(plan:&Plan,path:&Path)->Result<()>{
    if path.exists(){return Err("Timeline CSV destination already exists".into());}
    let mut output=String::from("\u{feff}shot,start_seconds,end_seconds,duration_seconds,source_in_seconds,filename,status\n");
    for shot in &plan.shots {
        let filename=shot.file.as_ref().map(|p|p.file_name()
            .unwrap_or_default().to_string_lossy().to_string()).unwrap_or_else(||shot.requested.clone());
        let value=|s:&str|format!("\"{}\"",s.replace('"',"\"\""));
        output.push_str(&format!("{},{:.3},{:.3},{:.3},{:.3},{},{}\n",
            value(&shot.section),shot.start,shot.end,shot.duration(),shot.source_in,
            value(&filename),if shot.file.is_some(){"matched"}else{"missing"}));
    }
    fs::write(path,output)?;Ok(())
}

#[cfg(test)]
mod tests{
    use super::*;
    #[test]
    fn simple_plan_timing_and_srt(){
        let p=parse("name=Alpha\n00:00 --> 00:02.5 | image | a.jpg\n00:02.5 --> 00:04 | video | b.mp4 | in=0.5").unwrap();
        assert_eq!(p.shots.len(),2);assert_eq!(p.duration(),4.0);
        assert_eq!(parse_srt("1\n00:00:00,000 --> 00:00:02,000\nHello\n",4.0).unwrap().len(),1);
        assert!(parse("00:01 --> 00:02 | image | a.jpg").is_err());
    }
    #[test]
    fn legacy_plan_loads_shots(){
        let x="SCENE_PLAN_VERSION=2\n[PROJECT]\nname=Big Video\n[OUTPUT]\nresolution=1080p\n[SCENE_001.SHOT_01]\nmedia_type=image\nlibrary_filename=a.jpg\nshot_duration_seconds=4\n";
        let p=parse(x).unwrap();assert_eq!(p.shots.len(),1);assert_eq!(p.duration(),4.0);
    }
}

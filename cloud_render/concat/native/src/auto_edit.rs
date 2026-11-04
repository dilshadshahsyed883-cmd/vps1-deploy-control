//! Z Builder-style Auto Editing plans using official Concat v0.2.8 MCP commands.
//! Editing stays inside native concat.json; no external FFmpeg filter sidecar.
use crate::library::Result;
use serde_json::{json,Value};

#[derive(Clone, Debug)]
pub struct Options {
    pub transition:String,
    pub transition_seconds:f64,
    pub animation:String,
    pub animation_seconds:f64,
    pub scale:String,
}

impl Options {
    pub fn check(&self)->Result<()> {
        if !self.transition_seconds.is_finite() || self.transition_seconds < 0.05
            || self.transition_seconds > 4.0 {
            return Err("Transition duration must be between 0.05 and 4 seconds".into());
        }
        if !self.animation_seconds.is_finite() || self.animation_seconds < 0.1
            || self.animation_seconds > 600.0 {
            return Err("Animation duration must be between 0.1 and 600 seconds".into());
        }
        if !["None","cross-fade"].contains(&self.transition.as_str()) {
            return Err("Unsupported native Concat transition".into());
        }
        if !["None","Zoom In","Zoom Out","Pan Left","Pan Right","Combo Zoom + Pan"]
             .contains(&self.animation.as_str()) {
            return Err("Unsupported native Concat motion preset".into());
        }
        if !["Keep","Fit","Fill","105%","110%"].contains(&self.scale.as_str()) {
            return Err("Unsupported native Concat scale setting".into());
        }
        Ok(())
    }
}

pub fn visual_clips(timeline:&Value)->Vec<Value> {
    timeline["timeline"]["clips"].as_array().into_iter().flatten()
        .filter(|c| matches!(c["kind"].as_str(),Some("image"|"video")))
        .cloned().collect()
}

pub fn commands(timeline:&Value, options:&Options, selected:Option<&str>)->Result<Vec<Value>> {
    options.check()?;
    let clips=visual_clips(timeline);
    if clips.is_empty(){return Err("No visual clips found in current Concat timeline".into());}
    if let Some(id)=selected {
        if id.is_empty() || !clips.iter().any(|c|c["id"].as_str()==Some(id)){
            return Err("Select a valid image or video clip first".into());
        }
    }
    let mut edits=Vec::<Value>::new();
    for clip in &clips {
        let Some(id)=clip["id"].as_str() else {continue};
        if selected.is_some_and(|selected_id|selected_id!=id){continue;}
        let duration=clip["duration"].as_f64().unwrap_or(0.0);
        if duration<=0.0 {continue;}
        if options.transition=="cross-fade" {
            let start=clip["start"].as_f64().unwrap_or(f64::NAN);
            // timeline_read is ordered globally, with other tracks interleaved.
            // Find the immediate predecessor on this lane, including nonvisual
            // clips so titles/gaps cannot be mistaken for a supported cut.
            let previous=timeline["timeline"]["clips"].as_array().into_iter().flatten()
                .filter(|c|c["id"]!=clip["id"] && c["track"].as_str().is_some()
                    && c["track"]==clip["track"]
                    && c["start"].as_f64().is_some_and(|at|at<start))
                .max_by(|a,b|a["start"].as_f64().unwrap_or(0.0)
                    .total_cmp(&b["start"].as_f64().unwrap_or(0.0)));
            if let Some(previous)=previous {
                let before_end=previous["end"].as_f64().unwrap_or(-1.0);
                if matches!(previous["kind"].as_str(),Some("image"|"video"))
                    && (before_end-start).abs()<0.001 {
                    let prev_duration=previous["duration"].as_f64().unwrap_or(0.0);
                    let seconds=options.transition_seconds
                        .min(duration*0.49).min(prev_duration*0.49);
                    if seconds>=0.05 {
                        edits.push(json!({"op":"updateClip","clipId":id,
                            "patch":{"transitionIn":{"id":"cross-fade",
                                                     "duration":seconds}}}));
                    }
                }
            }
        }
        if options.scale=="Fit" || options.scale=="Fill" {
            edits.push(json!({"op":"frameClip","clipId":id,
                "framing":options.scale.to_ascii_lowercase()}));
        } else if options.scale=="105%" || options.scale=="110%" {
            let n=if options.scale=="105%"{1.05}else{1.10};
            edits.push(json!({"op":"setClipTransform","clipId":id,"scale":n}));
        }
        if clip["kind"]!="image" || options.animation=="None" {continue;}
        let stop=(options.animation_seconds/duration).min(1.0);
        let base=match options.scale.as_str(){
            "105%"=>1.05,"110%"=>1.10,
            _=>clip["scale"].as_f64().unwrap_or(1.0),
        };
        let offset=if ["Fit","Fill"].contains(&options.scale.as_str()){0.0}
            else{clip["offsetX"].as_f64().unwrap_or(0.0)};
        let mut key=|property:&str,from:f64,to:f64|{
            for (at,value) in [(0.0,from),(stop,to)]{
                edits.push(json!({"op":"setClipKey","clipId":id,
                    "property":property,"at":at,"value":if property=="scale"{(value*base).clamp(0.05,8.0)}
                          else{(value+offset).clamp(-3.0,3.0)}}));
            }
        };
        match options.animation.as_str() {
            "Zoom In"=>key("scale",1.0,1.12),
            "Zoom Out"=>key("scale",1.12,1.0),
            "Pan Left"=>{key("scale",1.10,1.10);key("offsetX",0.06,-0.06);},
            "Pan Right"=>{key("scale",1.10,1.10);key("offsetX",-0.06,0.06);},
            "Combo Zoom + Pan"=>{
                key("scale",1.03,1.12);
                key("offsetX",-0.045,0.045);
            },
            _=>{}
        }
    }
    if edits.is_empty() {
        return Err("No supported edits for this selection; choose a preset or another cut".into());
    }
    Ok(edits)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn clip(id:&str,kind:&str,start:f64,duration:f64)->Value{
        json!({"id":id,"kind":kind,"track":"t1",
          "start":start,"duration":duration,"end":start+duration})
    }
    fn sample()->Value {json!({"timeline":{"clips":[
        clip("a","image",0.0,3.0),
        clip("b","video",3.0,2.0),
        clip("c","image",5.0,4.0),
        clip("v","audio",0.0,9.0)
    ]}})}
    fn opts()->Options{Options{transition:"cross-fade".into(),transition_seconds:0.4,
        animation:"Zoom In".into(),animation_seconds:2.0,scale:"Fit".into()}}
    #[test] fn five_tab_edit_engine_uses_official_native_operations(){
        let edits=commands(&sample(),&opts(),None).unwrap();
        assert_eq!(edits.iter().filter(|x|x["op"]=="updateClip").count(),2);
        assert_eq!(edits.iter().filter(|x|x["op"]=="frameClip").count(),3);
        assert_eq!(edits.iter().filter(|x|x["op"]=="setClipKey").count(),4);
        assert!(!edits.iter().any(|x|x["clipId"]=="v"));
        assert_eq!(edits[0]["op"],"frameClip");
    }
    #[test] fn select_one_clip_does_not_modify_others(){
        let edits=commands(&sample(),&opts(),Some("b")).unwrap();
        assert!(edits.iter().all(|x|x["clipId"]=="b"));
        assert_eq!(edits.len(),2);
        assert!(commands(&sample(),&opts(),Some("missing")).is_err());
    }
    #[test] fn transitions_skip_gaps_and_other_tracks(){
        let bad=json!({"timeline":{"clips":[
            clip("a","image",0.0,2.0),
            clip("b","image",4.0,2.0)
        ]}});
        let mut o=opts();o.animation="None".into();o.scale="Keep".into();
        assert!(commands(&bad,&o,None).is_err());
    }
    #[test] fn selected_video_never_receives_image_motion(){
        let mut o=opts();o.transition="None".into();o.scale="Keep".into();
        assert!(commands(&sample(),&o,Some("b")).is_err());
    }
    #[test] fn interleaved_tracks_keep_real_lane_boundaries(){
        let mut overlay=clip("overlay","image",1.0,6.0);overlay["track"]=json!("t2");
        let timeline=json!({"timeline":{"clips":[clip("a","image",0.0,3.0),
            overlay,clip("b","image",3.0,2.0)]}});
        let mut o=opts();o.animation="None".into();o.scale="Keep".into();
        let edits=commands(&timeline,&o,None).unwrap();
        assert_eq!(edits.len(),1);assert_eq!(edits[0]["clipId"],"b");
    }
    #[test] fn image_motion_respects_requested_scale(){
        let mut o=opts();o.transition="None".into();o.scale="110%".into();
        let edits=commands(&sample(),&o,Some("a")).unwrap();
        let first=edits.iter().find(|e|e["op"]=="setClipKey").unwrap();
        assert!((first["value"].as_f64().unwrap()-1.1).abs()<0.0001);
    }
    #[test] fn reject_unrecognised_presets_or_bad_durations(){
        let mut o=opts();o.transition_seconds=f64::NAN;
        assert!(commands(&sample(),&o,None).is_err());
        o=opts();o.transition="nonexistent-wipe".into();
        assert!(commands(&sample(),&o,None).is_err());
    }
}

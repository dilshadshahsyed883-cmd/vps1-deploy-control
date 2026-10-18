from __future__ import annotations
import json, shutil
from pathlib import Path
from typing import Any
from .transition_engine import CANONICAL_TRANSITIONS, canonical_transition

TRANSITIONS = CANONICAL_TRANSITIONS
FILTERS = ('none','warm','cool','high_contrast','soft','bright','dark','vintage','documentary','cinematic','black_white','sepia','desaturated','vivid','sharpen','teal_orange','natural_pop','moody_green','clean_premium')
IMAGE_MOTIONS = ('none','zoom_in','zoom_out','pan_left','pan_right','ken_burns')
VIDEO_MOTIONS = ('none','slow_push','reframe_left','reframe_right','gentle_zoom')
ASSET_CATEGORIES = ('effects','overlays','transitions','transition_sfx','filters','motion_presets')

def clamp(value: Any, low: float, high: float, default: float) -> float:
    try: return max(low,min(high,float(value)))
    except Exception: return default

def normalize_scene_creative(scene: dict[str,Any], defaults: dict[str,Any]|None=None) -> dict[str,Any]:
    defaults=defaults or {}; out=dict(scene)
    mt=str(out.get('media_type','image') or 'image').lower(); mt=mt if mt in {'image','video'} else 'image'; out['media_type']=mt
    tr=out.get('transition_out', out.get('transition', defaults.get('transition_out','cross_dissolve')))
    out['transition_out']=canonical_transition(tr)
    transition_min_ms = 20 if out['transition_out'] in {'none','hard_cut'} else 100
    out['transition_duration_ms']=int(clamp(out.get('transition_duration_ms', defaults.get('transition_duration_ms',350)),transition_min_ms,800,350))
    flt=str(out.get('filter', defaults.get('filter','none')) or 'none').lower().replace(' ','_')
    out['filter']=flt if flt in FILTERS else 'none'; out['filter_intensity']=clamp(out.get('filter_intensity',defaults.get('filter_intensity',0.5)),0,1,0.5)
    motion=str(out.get('motion',defaults.get('motion','none')) or 'none').lower().replace(' ','_')
    allowed=VIDEO_MOTIONS if mt=='video' else IMAGE_MOTIONS
    out['motion']=motion if motion in allowed else 'none'; out['motion_intensity']=clamp(out.get('motion_intensity',defaults.get('motion_intensity',0.35)),0,1,0.35)
    out['effect']=str(out.get('effect','none') or 'none'); out['overlay']=str(out.get('overlay','none') or 'none'); out['transition_sfx']=str(out.get('transition_sfx','none') or 'none')
    return out

def import_creative_plan(path: str|Path, scenes:list[dict[str,Any]]) -> tuple[list[dict[str,Any]],int,list[str]]:
    data=json.loads(Path(path).read_text(encoding='utf-8')); items=data.get('scenes',[]) if isinstance(data,dict) else data
    if not isinstance(items,list): raise ValueError('Plan must be a list or contain a scenes list.')
    by_code={str(x.get('scene_code') or x.get('scene_id') or '').upper():x for x in items if isinstance(x,dict)}; updated=0; warnings=[]; result=[]
    for scene in scenes:
        code=str(scene.get('scene_code') or scene.get('scene_id') or '').upper(); item=by_code.get(code)
        merged=dict(scene)
        if item:
            for key in ('media_type','primary_keyword','keyword','fallback_keywords','transition_out','transition_duration_ms','filter','filter_intensity','motion','motion_intensity','effect','overlay','transition_sfx'):
                if key in item: merged[key]=item[key]
            updated+=1
        normalized=normalize_scene_creative(merged)
        if item and normalized.get('transition_out') != str(item.get('transition_out',normalized.get('transition_out'))).lower(): warnings.append(f'{code}: unsupported transition replaced')
        result.append(normalized)
    return result,updated,warnings

def export_project_package(target:str|Path, project_name:str, scenes:list[dict[str,Any]], captions:list[dict[str,Any]], audio_path:str='', portable:bool=False) -> Path:
    target=Path(target); target.mkdir(parents=True,exist_ok=True)
    media_dir=target/'media'; voice_dir=target/'voice'
    if portable: media_dir.mkdir(exist_ok=True); voice_dir.mkdir(exist_ok=True)
    exported=[]
    for scene in scenes:
        item=normalize_scene_creative(scene); source=Path(str(item.get('media_path') or ''))
        if portable and source.is_file():
            dest=media_dir/f"{item.get('scene_code','SCENE')}{source.suffix.lower()}"; shutil.copy2(source,dest); item['media_path']=str(Path('media')/dest.name)
        exported.append(item)
    audio_ref=audio_path
    audio=Path(audio_path) if audio_path else None
    if portable and audio and audio.is_file():
        dest=voice_dir/audio.name; shutil.copy2(audio,dest); audio_ref=str(Path('voice')/dest.name)
    (target/'project.json').write_text(json.dumps({'name':project_name,'schema':'2.1','portable':portable,'audio_path':audio_ref},ensure_ascii=False,indent=2),encoding='utf-8')
    (target/'scene-plan.json').write_text(json.dumps({'schema':'2.1','scenes':exported},ensure_ascii=False,indent=2),encoding='utf-8')
    (target/'captions.json').write_text(json.dumps(captions,ensure_ascii=False,indent=2),encoding='utf-8')
    return target

def scan_asset_library(root:str|Path) -> dict[str,list[dict[str,Any]]]:
    root=Path(root); result={category:[] for category in ASSET_CATEGORIES}
    for category in ASSET_CATEGORIES:
        folder=root/category; folder.mkdir(parents=True,exist_ok=True)
        for manifest in folder.rglob('manifest.json'):
            try:
                data=json.loads(manifest.read_text(encoding='utf-8'))
                if not isinstance(data,dict): continue
                file_name=str(data.get('file','')); asset_file=(manifest.parent/file_name) if file_name else None
                data['_manifest']=str(manifest); data['_available']=bool(asset_file and asset_file.is_file()); result[category].append(data)
            except Exception: continue
    return result

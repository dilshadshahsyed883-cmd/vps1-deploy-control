from __future__ import annotations
import json, shutil, hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .creative_v21 import *

SUPPORTED_EFFECTS=('none','subtle_pulse','soft_glow','edge_vignette','gentle_shake')
SAFE_MEDIA_EXT={'.mp4','.mov','.webm','.png','.jpg','.jpeg','.webp','.wav','.mp3','.m4a','.ogg'}


def _slug(v: Any)->str:
    return str(v or 'none').strip().lower().replace(' ','_')


def scan_asset_library_v23(root:str|Path)->dict[str,list[dict[str,Any]]]:
    root=Path(root)
    result=scan_asset_library(root)
    result.setdefault('overlays',[])
    result.setdefault('transition_sfx',[])
    result.setdefault('ambient_sfx',[])

    # Easy library mode: every loose media filename becomes its stable asset ID.
    loose_categories={
        'overlays': {'.mp4','.mov','.webm','.png','.jpg','.jpeg','.webp'},
        'transition_sfx': {'.wav','.mp3','.m4a','.ogg'},
        'ambient_sfx': {'.wav','.mp3','.m4a','.ogg'},
    }
    existing={(category,_slug(item.get('id'))) for category,items in result.items() for item in items}
    for category,extensions in loose_categories.items():
        folder=root/category
        folder.mkdir(parents=True,exist_ok=True)
        for file_path in sorted(folder.rglob('*')):
            if not file_path.is_file() or file_path.suffix.lower() not in extensions:
                continue
            asset_id=_slug(file_path.stem)
            if (category,asset_id) in existing:
                continue
            tags=[x for x in asset_id.split('_') if x]
            result[category].append({
                'id':asset_id,'name':file_path.stem.replace('_',' ').title(),'category':category,
                'file':file_path.name,'supported_media':['image','video'],'tags':tags,
                '_manifest':'','_available':True,'_loose_file':True,'_direct_path':str(file_path),
            })
            existing.add((category,asset_id))

    for category, items in result.items():
        valid=[]
        for raw in items:
            item=dict(raw); errors=[]
            if _slug(item.get('category',category)) != category: errors.append('category mismatch')
            if not item.get('id'): errors.append('missing id')
            if item.get('_direct_path'):
                file_path=Path(str(item['_direct_path']))
            else:
                file_name=str(item.get('file',''))
                file_path=Path(item.get('_manifest','')).parent/file_name if file_name and item.get('_manifest') else None
            if file_path and file_path.suffix.lower() not in SAFE_MEDIA_EXT: errors.append('unsupported file extension')
            available=bool(file_path and file_path.is_file())
            item['_available']=available
            item['_file_path']=str(file_path) if file_path else ''
            item['_valid']=not errors and available
            item['_errors']=errors
            item['supported_media']=[_slug(x) for x in item.get('supported_media',['image','video'])]
            item['tags']=[_slug(x) for x in item.get('tags',[])]
            valid.append(item)
        result[category]=valid
    return result


def asset_index(library:dict[str,list[dict[str,Any]]])->dict[str,dict[str,Any]]:
    out={}
    for category,items in library.items():
        for item in items:
            if item.get('_valid'): out[f'{category}:{_slug(item.get("id"))}']=item
    return out


def resolve_asset(asset_id:str, category:str, media_type:str, library:dict[str,list[dict[str,Any]]])->dict[str,Any]|None:
    aid=_slug(asset_id)
    if aid in {'','none'}: return None
    item=asset_index(library).get(f'{category}:{aid}')
    if not item: return None
    if media_type not in item.get('supported_media',['image','video']): return None
    return item


def normalize_scene_creative_v23(scene:dict[str,Any], library:dict[str,list[dict[str,Any]]]|None=None)->dict[str,Any]:
    out=normalize_scene_creative(scene)
    out['effect']=_slug(out.get('effect','none'))
    out['effect_intensity']=clamp(out.get('effect_intensity',.35),0,1,.35)
    out['overlay']=_slug(out.get('overlay','none'))
    out['overlay_intensity']=clamp(out.get('overlay_intensity',.35),0,1,.35)
    out['transition_sfx']=_slug(out.get('transition_sfx','none'))
    out['sfx_volume']=clamp(out.get('sfx_volume',.35),0,1,.35)
    out['creative_confidence']=clamp(out.get('creative_confidence',.75),0,1,.75)
    out['creative_fallbacks']=out.get('creative_fallbacks',{}) if isinstance(out.get('creative_fallbacks',{}),dict) else {}
    if out['effect'] not in SUPPORTED_EFFECTS and out['effect']!='none': out['effect']='none'
    if library:
        mt=out['media_type']; fallback=out['creative_fallbacks']
        for key,cat in [('overlay','overlays'),('transition_sfx','transition_sfx')]:
            if out[key]!='none' and not resolve_asset(out[key],cat,mt,library):
                fallback[key]=out[key]; out[key]='none'; out['creative_confidence']=min(out['creative_confidence'],.45)
        out['creative_fallbacks']=fallback
    return out


def recommend_scene_creative(scene:dict[str,Any], library:dict[str,list[dict[str,Any]]], style:str='balanced')->dict[str,Any]:
    out=normalize_scene_creative_v23(scene,library)
    text=(str(out.get('text') or out.get('scene_text') or '')+' '+str(out.get('primary_keyword') or '')).lower()
    energetic=any(x in text for x in ('explosion','race','fast','attack','breaking','urgent','crash','victory'))
    emotional=any(x in text for x in ('sad','alone','heart','memory','death','love','cry','emotional'))
    historic=any(x in text for x in ('history','ancient','old','war','king','century'))
    if energetic:
        out.update(transition_out='zoom',filter='vivid',motion='gentle_zoom' if out['media_type']=='video' else 'zoom_in',effect='subtle_pulse',creative_confidence=.82)
    elif emotional:
        out.update(transition_out='dissolve',filter='soft',motion='slow_push' if out['media_type']=='video' else 'zoom_in',effect='soft_glow',creative_confidence=.86)
    elif historic:
        out.update(transition_out='fade',filter='vintage',motion='none' if out['media_type']=='video' else 'ken_burns',effect='edge_vignette',creative_confidence=.84)
    else:
        out.update(transition_out='dissolve',filter='documentary',motion='none' if out['media_type']=='video' else 'zoom_in',effect='none',creative_confidence=.76)
    overlays=[x for x in library.get('overlays',[]) if x.get('_valid') and out['media_type'] in x.get('supported_media',[])]
    sfx=[x for x in library.get('transition_sfx',[]) if x.get('_valid')]
    tags=set(text.replace(',',' ').split())
    def score(item): return len(tags & set(item.get('tags',[])))
    if overlays:
        best=max(overlays,key=score)
        if score(best)>0: out['overlay']=_slug(best.get('id'))
    if sfx and out['transition_out'] not in {'none','cut','dissolve'}:
        best=max(sfx,key=score)
        if score(best)>0: out['transition_sfx']=_slug(best.get('id'))
    return normalize_scene_creative_v23(out,library)


def apply_ai_creative_batch(scenes:list[dict[str,Any]], library:dict[str,list[dict[str,Any]]], only_missing:bool=True)->tuple[list[dict[str,Any]],dict[str,int]]:
    stats={'updated':0,'skipped':0,'low_confidence':0}
    result=[]
    for scene in scenes:
        has=any(_slug(scene.get(k,'none'))!='none' for k in ('effect','overlay','transition_sfx'))
        if only_missing and has:
            item=normalize_scene_creative_v23(scene,library); stats['skipped']+=1
        else:
            item=recommend_scene_creative(scene,library); stats['updated']+=1
        if item.get('creative_confidence',0)<.6: stats['low_confidence']+=1
        result.append(item)
    return result,stats


def append_usage_history(root:str|Path, project:str, scenes:list[dict[str,Any]])->Path:
    root=Path(root); root.mkdir(parents=True,exist_ok=True); path=root/'creative_usage.jsonl'
    now=datetime.now(timezone.utc).isoformat()
    with path.open('a',encoding='utf-8') as f:
        for s in scenes:
            row={'time':now,'project':project,'scene_id':s.get('scene_code') or s.get('scene_id'),'effect':s.get('effect','none'),'overlay':s.get('overlay','none'),'transition_sfx':s.get('transition_sfx','none'),'confidence':s.get('creative_confidence',0)}
            f.write(json.dumps(row,ensure_ascii=False)+'\n')
    return path


def export_project_package_v23(target:str|Path, project_name:str, scenes:list[dict[str,Any]], captions:list[dict[str,Any]], audio_path:str='', portable:bool=False, asset_root:str|Path|None=None)->Path:
    target=export_project_package(target,project_name,[normalize_scene_creative_v23(x) for x in scenes],captions,audio_path,portable)
    meta=target/'project.json'; data=json.loads(meta.read_text(encoding='utf-8')); data['schema']='2.3'; meta.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    plan=target/'scene-plan.json'; pdata=json.loads(plan.read_text(encoding='utf-8')); pdata['schema']='2.3'; plan.write_text(json.dumps(pdata,ensure_ascii=False,indent=2),encoding='utf-8')
    if portable and asset_root:
        library=scan_asset_library_v23(asset_root); used=set()
        for s in scenes:
            for key,cat in [('overlay','overlays'),('transition_sfx','transition_sfx')]:
                item=resolve_asset(str(s.get(key,'none')),cat,str(s.get('media_type','image')),library)
                if item: used.add(Path(item['_manifest']).parent)
        packs=target/'asset_packs'; packs.mkdir(exist_ok=True)
        for folder in used:
            dest=packs/folder.name
            if not dest.exists(): shutil.copytree(folder,dest)
    return target

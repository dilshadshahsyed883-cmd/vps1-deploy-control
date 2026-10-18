from __future__ import annotations
import json, hashlib, shutil, urllib.parse, urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SAFE_PLUGIN_CAPABILITIES={"stock_provider","asset_catalog","transition_pack","recommendation_rules"}
SAFE_EXT={'.json','.png','.jpg','.jpeg','.webp','.mp4','.mov','.webm','.wav','.mp3','.m4a','.ogg'}

@dataclass
class StockResult:
    provider:str; asset_id:str; media_type:str; preview_url:str; download_url:str
    width:int=0; height:int=0; duration:float=0.0; page_url:str=''; author:str=''; tags:str=''
    variants:list[dict[str,Any]]=field(default_factory=list)
    def as_dict(self): return self.__dict__.copy()

def _get_json(url:str, headers:dict[str,str]|None=None, timeout:int=30)->dict:
    req=urllib.request.Request(url,headers=headers or {'User-Agent':'StockMediaSceneBuilder/3.0'})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def _pexels_video_size(target_width:int=0,target_height:int=0)->str:
    """Map the project canvas to Pexels video-search resolution classes."""
    w,h=max(0,int(target_width or 0)),max(0,int(target_height or 0))
    long_edge=max(w,h); short_edge=min(w,h)
    if long_edge>=3000 or short_edge>=1800:
        return 'large'
    if long_edge>=1600 or short_edge>=900:
        return 'medium'
    return 'small'

def search_pexels(query:str, media_type:str, api_key:str, per_page:int=12, target_width:int=0, target_height:int=0, prefer_landscape:bool=True)->list[StockResult]:
    media_type='video' if media_type=='video' else 'image'
    base='https://api.pexels.com/v1/videos/search' if media_type=='video' else 'https://api.pexels.com/v1/search'
    params={'query':query,'per_page':max(1,min(80,per_page))}
    if prefer_landscape: params['orientation']='landscape'
    if media_type=='video' and target_width and target_height:
        params['size']=_pexels_video_size(target_width,target_height)
    data=_get_json(base+'?'+urllib.parse.urlencode(params),{'Authorization':api_key,'User-Agent':'StockMediaSceneBuilder/3.0'})
    out=[]
    for x in data.get('videos' if media_type=='video' else 'photos',[]):
        if media_type=='video':
            files=[]
            for row in x.get('video_files',[]):
                w,h=int(row.get('width') or 0),int(row.get('height') or 0)
                ratio=(w/h) if w>0 and h>0 else 0.0
                if row.get('link') and (not prefer_landscape or ratio>=1.55):
                    files.append(row)
            if not files: continue
            variants=[{
                'download_url':str(row.get('link') or ''),
                'width':int(row.get('width') or 0),
                'height':int(row.get('height') or 0),
                'quality':str(row.get('quality') or ''),
                'file_type':str(row.get('file_type') or ''),
                'fps':float(row.get('fps') or 0.0),
            } for row in files if row.get('link')]
            seed={'provider':'pexels','asset_id':str(x.get('id')),'media_type':'video','variants':variants,'width':0,'height':0}
            chosen=optimize_video_candidate(seed,float(x.get('duration') or 0),target_width or 1280,target_height or 720,prefer_landscape)
            dl=str(chosen.get('download_url') or '')
            if not dl: continue
            out.append(StockResult('pexels',str(x.get('id')),media_type,str(x.get('image','')),dl,int(chosen.get('width') or 0),int(chosen.get('height') or 0),float(x.get('duration') or 0),str(x.get('url','')),str((x.get('user') or {}).get('name','')),variants=variants))
        else:
            w,h=int(x.get('width') or 0),int(x.get('height') or 0)
            ratio=(w/h) if w>0 and h>0 else 0.0
            if prefer_landscape and ratio<1.55: continue
            src=x.get('src') or {}
            # Images remain high-resolution first; the renderer performs the final exact canvas normalization.
            dl=src.get('original') or src.get('large2x') or src.get('large') or src.get('landscape')
            if dl: out.append(StockResult('pexels',str(x.get('id')),media_type,src.get('medium') or dl,dl,w,h,0,str(x.get('url','')),str(x.get('photographer',''))))
    # A strict Pexels size query can legitimately return no videos. Retry once
    # without the size filter so lower-resolution media remains a last-resort fallback.
    if media_type=='video' and not out and params.get('size'):
        fallback=dict(params); fallback.pop('size',None)
        data=_get_json(base+'?'+urllib.parse.urlencode(fallback),{'Authorization':api_key,'User-Agent':'StockMediaSceneBuilder/3.0'})
        for x in data.get('videos',[]):
            variants=[]
            for row in x.get('video_files',[]):
                w,h=int(row.get('width') or 0),int(row.get('height') or 0); ratio=(w/h) if w>0 and h>0 else 0.0
                if row.get('link') and (not prefer_landscape or ratio>=1.55):
                    variants.append({'download_url':str(row.get('link') or ''),'width':w,'height':h,'quality':str(row.get('quality') or ''),'file_type':str(row.get('file_type') or ''),'fps':float(row.get('fps') or 0.0)})
            if not variants: continue
            seed={'provider':'pexels','asset_id':str(x.get('id')),'media_type':'video','variants':variants}
            chosen=optimize_video_candidate(seed,float(x.get('duration') or 0),target_width or 1280,target_height or 720,prefer_landscape)
            if chosen.get('download_url'):
                out.append(StockResult('pexels',str(x.get('id')),media_type,str(x.get('image','')),str(chosen['download_url']),int(chosen.get('width') or 0),int(chosen.get('height') or 0),float(x.get('duration') or 0),str(x.get('url','')),str((x.get('user') or {}).get('name','')),variants=variants))
    return out
def search_pixabay(query:str, media_type:str, api_key:str, per_page:int=12)->list[StockResult]:
    media_type='video' if media_type=='video' else 'image'
    base='https://pixabay.com/api/videos/' if media_type=='video' else 'https://pixabay.com/api/'
    data=_get_json(base+'?'+urllib.parse.urlencode({'key':api_key,'q':query,'per_page':max(3,min(200,per_page)),'safesearch':'true'}))
    out=[]
    for x in data.get('hits',[]):
        if media_type=='video':
            vids=x.get('videos') or {}; f=vids.get('large') or vids.get('medium') or vids.get('small') or vids.get('tiny') or {}
            dl=f.get('url'); prev=(vids.get('tiny') or {}).get('url','')
            if dl: out.append(StockResult('pixabay',str(x.get('id')),media_type,prev,dl,int(f.get('width') or 0),int(f.get('height') or 0),float(x.get('duration') or 0),str(x.get('pageURL','')),str(x.get('user','')),str(x.get('tags',''))))
        else:
            dl=x.get('largeImageURL') or x.get('webformatURL')
            if dl: out.append(StockResult('pixabay',str(x.get('id')),media_type,str(x.get('previewURL') or dl),str(dl),int(x.get('imageWidth') or 0),int(x.get('imageHeight') or 0),0,str(x.get('pageURL','')),str(x.get('user','')),str(x.get('tags',''))))
    return out

def search_multi_provider(query:str, media_type:str, credentials:dict[str,list[str]], providers:list[str]|None=None, per_provider:int=8, target_width:int=0, target_height:int=0, prefer_landscape:bool=True, preserve_provider_order:bool=False)->list[dict[str,Any]]:
    providers=providers or ['pexels','pixabay']; out=[]; errors=[]
    for provider in providers:
        keys=[k.strip() for k in credentials.get(provider,[]) if k.strip()]
        for key in keys:
            try:
                rows=search_pexels(query,media_type,key,per_provider,target_width,target_height,prefer_landscape) if provider=='pexels' else search_pixabay(query,media_type,key,per_provider)
                out.extend(r.as_dict() for r in rows); break
            except Exception as exc: errors.append(f'{provider}: {exc}')
    tw,th=max(1,int(target_width or 1920)),max(1,int(target_height or 1080))
    target_ratio=tw/th
    def score(x):
        w,h=int(x.get('width') or 0),int(x.get('height') or 0)
        ratio=(w/h) if w>0 and h>0 else 0.0
        if prefer_landscape and ratio<1.55: return -1000.0
        ratio_score=max(0.0,45.0-abs(ratio-target_ratio)*80.0)
        meets=1.0 if w>=tw and h>=th else 0.0
        resolution=(min(w,tw)/tw*24)+(min(h,th)/th*16)+(25.0*meets)
        return ratio_score+resolution+5.0
    dedup={}
    for x in out:
        key=f"{x['provider']}:{x['asset_id']}"
        if key not in dedup: dedup[key]=x
    rows=list(dedup.values()) if preserve_provider_order else sorted(dedup.values(),key=score,reverse=True)
    for i,x in enumerate(rows): x['candidate_score']=round(score(x),1); x['rank']=i+1
    return rows

def optimize_video_candidate(result:dict[str,Any], scene_duration:float, target_width:int=1280, target_height:int=720, prefer_landscape:bool=True)->dict[str,Any]:
    """Prefer the smallest landscape variant that meets the project resolution.

    If no variant reaches the target, choose the largest available landscape
    fallback instead of silently preferring a low-bandwidth 720p file.
    """
    item=dict(result)
    if str(item.get('media_type') or '').lower()!='video': return item
    variants=[dict(v) for v in (item.get('variants') or []) if isinstance(v,dict) and v.get('download_url')]
    if not variants: return item
    landscape=[]
    for v in variants:
        w,h=int(v.get('width') or 0),int(v.get('height') or 0); ratio=(w/h) if w>0 and h>0 else 0.0
        if (not prefer_landscape) or ratio>=1.55: landscape.append(v)
    if not landscape: return item
    tw,th=max(1,int(target_width or 1280)),max(1,int(target_height or 720)); tr=tw/th
    adequate=[v for v in landscape if int(v.get('width') or 0)>=tw and int(v.get('height') or 0)>=th]
    if adequate:
        chosen=min(adequate,key=lambda v:(max(1,int(v.get('width') or 0))*max(1,int(v.get('height') or 0)),abs((int(v.get('width') or 0)/max(1,int(v.get('height') or 1)))-tr)))
        variant='target_resolution'
    else:
        chosen=max(landscape,key=lambda v:(max(1,int(v.get('width') or 0))*max(1,int(v.get('height') or 0)),-abs((int(v.get('width') or 0)/max(1,int(v.get('height') or 1)))-tr)))
        variant='lower_resolution_fallback'
    item['download_url']=str(chosen.get('download_url') or item.get('download_url') or '')
    item['width']=int(chosen.get('width') or item.get('width') or 0); item['height']=int(chosen.get('height') or item.get('height') or 0)
    item['acquisition_variant']=variant; item['target_width']=tw; item['target_height']=th
    return item

def find_cached_provider_media(cache_dir:str|Path, keyword:str, media_type:str)->tuple[Path|None,dict[str,Any]]:
    """Find an already-downloaded provider asset for the same keyword/type."""
    root=Path(cache_dir)
    if not root.is_dir():
        return None,{}
    wanted=' '.join(str(keyword or '').lower().split())
    sidecars=sorted(root.glob('*.json'),key=lambda p:p.stat().st_mtime if p.exists() else 0,reverse=True)
    for sidecar in sidecars[:500]:
        try:
            data=json.loads(sidecar.read_text(encoding='utf-8'))
        except Exception:
            continue
        if str(data.get('media_type') or '').lower()!=str(media_type or '').lower():
            continue
        saved=' '.join(str(data.get('search_keyword') or '').lower().split())
        if not saved or saved!=wanted:
            continue
        stem=sidecar.name[:-5]
        hits=[p for p in root.glob(stem+'.*') if p.is_file() and p.suffix.lower() in SAFE_EXT and p.suffix.lower()!='.json']
        if hits:
            return hits[0],data
    return None,{}

def download_result(result:dict[str,Any], target_dir:str|Path, scene_id:str='')->Path:
    target_dir=Path(target_dir); target_dir.mkdir(parents=True,exist_ok=True)
    ext=Path(urllib.parse.urlparse(result['download_url']).path).suffix.lower()
    if ext not in SAFE_EXT: ext='.mp4' if result.get('media_type')=='video' else '.jpg'
    stem=f"{scene_id+'_' if scene_id else ''}{result.get('provider')}_{result.get('asset_id')}"
    dest=target_dir/(stem+ext); tmp=dest.with_suffix(dest.suffix+'.part')
    req=urllib.request.Request(result['download_url'],headers={'User-Agent':'StockMediaSceneBuilder/3.0'})
    try:
        with urllib.request.urlopen(req,timeout=90) as r, tmp.open('wb') as f: shutil.copyfileobj(r,f)
        tmp.replace(dest)
        (dest.with_suffix(dest.suffix+'.json')).write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
        return dest
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

def scan_expandable_library(root:str|Path)->dict[str,Any]:
    root=Path(root); catalog={'assets':[],'packs':[],'plugins':[],'errors':[]}
    for manifest in root.rglob('manifest.json') if root.exists() else []:
        try:
            d=json.loads(manifest.read_text(encoding='utf-8')); d['_manifest']=str(manifest)
            typ=str(d.get('type') or d.get('category') or 'asset').lower()
            if typ=='plugin':
                caps=set(d.get('capabilities',[])); forbidden=caps-SAFE_PLUGIN_CAPABILITIES
                if forbidden or any(k in d for k in ('entrypoint','executable','script','command')): raise ValueError('unsafe plugin manifest')
                d['_enabled']=bool(d.get('enabled',True)); catalog['plugins'].append(d)
            elif typ in ('transition_pack','asset_pack','pack'): catalog['packs'].append(d)
            else: catalog['assets'].append(d)
        except Exception as exc: catalog['errors'].append({'manifest':str(manifest),'error':str(exc)})
    catalog['summary']={k:len(catalog[k]) for k in ('assets','packs','plugins','errors')}
    return catalog

def recommend_assets(scene:dict[str,Any], catalog:dict[str,Any], limit:int=5)->list[dict[str,Any]]:
    text=(' '.join(str(scene.get(k,'')) for k in ('text','scene_text','primary_keyword'))).lower()
    words=set(text.replace(',',' ').replace('.',' ').split()); media=str(scene.get('media_type','image'))
    rec=[]
    for item in catalog.get('assets',[])+catalog.get('packs',[]):
        supported=[str(x).lower() for x in item.get('supported_media',['image','video'])]
        if media not in supported: continue
        tags=set(str(x).lower() for x in item.get('tags',[])); overlap=len(words&tags)
        category=str(item.get('category') or item.get('type') or 'asset')
        score=45+overlap*12+(8 if item.get('recommended') else 0)
        rec.append({'id':item.get('id'),'name':item.get('name',item.get('id')),'category':category,'score':min(100,score),'reason':f'{overlap} matching tags' if overlap else 'compatible installed asset','manifest':item.get('_manifest','')})
    return sorted(rec,key=lambda x:x['score'],reverse=True)[:limit]

def install_pack_from_folder(source:str|Path, library_root:str|Path)->Path:
    source=Path(source); manifest=source/'manifest.json'
    if not manifest.is_file(): raise ValueError('manifest.json is required')
    d=json.loads(manifest.read_text(encoding='utf-8'))
    if any(p.suffix.lower() not in SAFE_EXT|{'.md'} and p.name!='manifest.json' for p in source.rglob('*') if p.is_file()): raise ValueError('pack contains unsupported files')
    pack_id=str(d.get('id') or source.name).strip().replace(' ','_'); dest=Path(library_root)/'packs'/pack_id
    if dest.exists(): shutil.rmtree(dest)
    shutil.copytree(source,dest); return dest

def append_v3_history(root:str|Path,event:str,payload:dict[str,Any])->Path:
    root=Path(root); root.mkdir(parents=True,exist_ok=True); p=root/'v3_history.jsonl'
    row={'time':datetime.now(timezone.utc).isoformat(),'event':event,**payload}
    with p.open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    return p

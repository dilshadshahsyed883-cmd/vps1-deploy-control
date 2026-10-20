"""FFmpeg scene compiler and durable app-owned render queue. No broadcast I/O."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import threading
import time
import uuid
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

PROFILES={'1080p':(1920,1080,4500),'720p':(1280,720,2500),'480p':(854,480,1200)}
EXTENSIONS={'.mp4','.mov','.mkv','.webm','.m4v','.png','.jpg','.jpeg','.webp','.mp3','.wav','.m4a','.aac','.ogg','.flac'}
router=APIRouter(tags=['Auto Stream compositor'])
_render_lock=threading.Lock()
_preview_lock=threading.Lock()
_process_lock=threading.RLock()
_processes={}


def asset(engine, relative):
    value=str(relative or '')
    base=engine.media.resolve(); path=engine.media/value
    if not value or value.startswith('/') or '\\' in value or any(x.is_symlink() for x in (path,*path.parents)):
        raise ValueError('Choose an app media file without symlinks')
    target=path.resolve()
    if not target.is_relative_to(base) or not target.is_file() or target.suffix.lower() not in EXTENSIONS:
        raise ValueError('Media asset is absent, unsupported or outside app storage')
    return target


def number(value, low, high):
    try:result=float(value)
    except (TypeError,ValueError):raise ValueError('Source property must be numeric') from None
    if not low<=result<=high:raise ValueError('Source property is outside supported limits')
    return result


def validate(engine, document):
    if not isinstance(document,dict):raise ValueError('Studio configuration must be an object')
    scenes=document.get('scenes')
    if not isinstance(scenes,list) or not 1<=len(scenes)<=30:raise ValueError('Studio requires 1–30 scenes')
    active=document.get('active_scene',0)
    if not isinstance(active,int) or not 0<=active<len(scenes):raise ValueError('Select an existing scene')
    profile=document.get('profile','1080p')
    if profile not in PROFILES:raise ValueError('Choose 1080p, 720p or 480p')
    result={'name':str(document.get('name','Live Studio'))[:100],'active_scene':active,'profile':profile,
            'duration':number(document.get('duration',60),1,86400),'scenes':[]}
    for scene in scenes:
        if not isinstance(scene,dict) or not str(scene.get('name','')).strip():raise ValueError('Each scene needs a name')
        sources=scene.get('sources',[])
        if not isinstance(sources,list) or len(sources)>20:raise ValueError('Maximum 20 sources per scene')
        clean=[]
        for source in sources:
            if not isinstance(source,dict):raise ValueError('Source must be an object')
            kind=source.get('type','video')
            if kind not in ('video','image','text','audio','playlist','background','logo'):raise ValueError('Unsupported source type')
            row={'type':kind,'name':str(source.get('name',kind))[:100],'visible':bool(source.get('visible',True)),
                 'muted':bool(source.get('muted',False)),'volume':number(source.get('volume',1),0,2),
                 'x':number(source.get('x',0),0,1),'y':number(source.get('y',0),0,1),
                 'width':number(source.get('width',1),0.02,1),'height':number(source.get('height',1),0.02,1),
                 'font_size':int(number(source.get('font_size',48),12,200)),
                 'text':str(source.get('text',''))[:2000]}
            if kind=='playlist':
                files=source.get('files',[])
                if not isinstance(files,list) or not 1<=len(files)<=100:raise ValueError('Playlist requires 1–100 videos')
                row['files']=[str(f) for f in files]
                for f in files:engine._source(str(f))
            elif kind!='text':
                row['media_path']=str(source.get('media_path',''))
                path=asset(engine,row['media_path'])
                if kind in ('video','background') and path.suffix.lower() not in {'.mp4','.mov','.mkv','.webm','.m4v'}:
                    raise ValueError('Video/background source requires a video file')
                if kind in ('image','logo') and path.suffix.lower() not in {'.png','.jpg','.jpeg','.webp'}:
                    raise ValueError('Image/logo source requires an image file')
                if kind=='audio' and path.suffix.lower() not in {'.mp3','.wav','.m4a','.aac','.ogg','.flac'}:
                    raise ValueError('Audio source requires an audio file')
            clean.append(row)
        result['scenes'].append({'name':str(scene['name'])[:100],'sources':clean})
    return result


def has_audio(path):
    p=subprocess.run([shutil.which('ffprobe') or 'ffprobe','-v','error','-select_streams','a',
        '-show_entries','stream=index','-of','csv=p=0',str(path)],capture_output=True,text=True,timeout=20)
    if p.returncode:raise ValueError('FFprobe could not read source media')
    return bool(p.stdout.strip())


def compile_scene(engine, document, workspace, output, *, preview=False):
    """Compile trusted arguments; text uses files and never filter interpolation."""
    doc=validate(engine,document);w,h,bitrate=PROFILES[doc['profile']]
    workspace=Path(workspace);workspace.mkdir(parents=True,exist_ok=True)
    duration=doc['duration'];filters=[];audio=[];input_index=1;layer='base'
    command=[shutil.which('ffmpeg') or 'ffmpeg','-y','-nostdin','-hide_banner','-loglevel','error',
             '-filter_complex_threads','1','-f','lavfi','-i',f'color=c=black:s={w}x{h}:r=30:d={duration}']
    filters.append('[0:v]setsar=1[base]')
    for n,src in enumerate(doc['scenes'][doc['active_scene']]['sources']):
        if not src['visible']:continue
        kind=src['type']
        if kind=='text':
            textfile=workspace/f'text-{n}.txt';textfile.write_text(src['text'],encoding='utf-8')
            # The workspace is generated by us. No user string appears in filter syntax.
            next_layer=f'layer{n}'
            filters.append(f'[{layer}]drawtext=textfile={textfile}:expansion=none:fontcolor=white:fontsize={src["font_size"]}:x={int(src["x"]*w)}:y={int(src["y"]*h)}[{next_layer}]')
            layer=next_layer;continue
        if kind=='playlist':
            files=[asset(engine,f) for f in src['files']]
            concat=workspace/f'playlist-{n}.txt'
            concat.write_text(''.join("file '"+str(p).replace("'","'\\''")+"'\n" for p in files))
            command+=['-stream_loop','-1','-f','concat','-safe','0','-i',str(concat)]
            path=files[0]
        else:
            path=asset(engine,src['media_path'])
            command+=(['-loop','1'] if kind in ('image','logo') else ['-stream_loop','-1'])+['-i',str(path)]
        idx=input_index;input_index+=1
        if not src['muted'] and kind not in ('image','logo') and has_audio(path):
            audio.append(f'a{n}')
            filters.append(f'[{idx}:a]aresample=44100,asetpts=PTS-STARTPTS,volume={src["volume"]}[a{n}]')
        if kind=='audio':continue
        sw=max(2,int(w*src['width'])//2*2);sh=max(2,int(h*src['height'])//2*2)
        filters.append(f'[{idx}:v]setpts=PTS-STARTPTS,scale={sw}:{sh}:force_original_aspect_ratio=decrease,format=rgba[video{n}]')
        next_layer=f'layer{n}'
        filters.append(f'[{layer}][video{n}]overlay=x={int(w*src["x"])}:y={int(h*src["y"])}:eof_action=repeat:shortest=0[{next_layer}]')
        layer=next_layer
    filters.append(f'[{layer}]format=yuv420p[outv]')
    if audio:
        filters.append(''.join(f'[{a}]' for a in audio)+f'amix=inputs={len(audio)}:duration=longest:normalize=0,alimiter=limit=0.95[outa]')
    else:
        filters.append(f'anullsrc=r=44100:cl=stereo,atrim=duration={duration}[outa]')
    if preview:filters.append('[outa]anullsink')
    command+=['-filter_complex',';'.join(filters),'-map','[outv]']
    if preview:
        command+=['-frames:v','1','-an','-threads','1',str(output)]
    else:
        command+=['-map','[outa]','-t',str(duration),'-r','30','-c:v','libx264','-preset','veryfast',
                  '-threads','2','-b:v',f'{bitrate}k','-maxrate',f'{bitrate}k','-bufsize',f'{bitrate*2}k',
                  '-g','60','-c:a','aac','-b:a','128k','-movflags','+faststart',str(output)]
    return command


def init(engine):
    with engine._connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS scene_renders(id TEXT PRIMARY KEY,document TEXT NOT NULL,
            state TEXT NOT NULL,media_path TEXT NOT NULL DEFAULT '',failure TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
        con.execute("UPDATE scene_renders SET state='FAILED',failure='Render interrupted by service restart; retry explicitly' WHERE state IN ('WAITING','PREPARING')")


def read_render(engine,rid):
    with engine._connect() as con:row=con.execute('SELECT id,state,media_path,failure,created_at,updated_at FROM scene_renders WHERE id=?',(rid,)).fetchone()
    if not row:raise KeyError('Scene render not found')
    return dict(row)


def render_worker(engine,rid,doc):
    from auto_stream_engine import utc_now
    folder=engine.media/'auto-stream'/'renders'/rid
    with _render_lock:
        with engine._connect() as con:
            if con.execute('SELECT state FROM scene_renders WHERE id=?',(rid,)).fetchone()[0]=='CANCELLED':return
            con.execute("UPDATE scene_renders SET state='PREPARING',updated_at=? WHERE id=?",(utc_now(),rid))
        folder.mkdir(parents=True,exist_ok=True)
        try:
            command=compile_scene(engine,doc,folder,folder/'composition.tmp.mp4')
            proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            with _process_lock:_processes[rid]=proc
            try:code=proc.wait(timeout=min(21600,max(120,doc['duration']*4)))
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.wait();raise ValueError('Scene render timed out; use a shorter scene or lower profile')
            with engine._connect() as con:state=con.execute('SELECT state FROM scene_renders WHERE id=?',(rid,)).fetchone()[0]
            if state=='CANCELLED':return
            if code:raise ValueError('FFmpeg could not compose these sources; check formats, playlist compatibility and media integrity')
            output=folder/'composition.mp4';os.replace(folder/'composition.tmp.mp4',output)
            with engine._connect() as con:con.execute("UPDATE scene_renders SET state='READY',media_path=?,updated_at=? WHERE id=?",(str(output.relative_to(engine.media)),utc_now(),rid))
        except Exception as exc:
            with engine._connect() as con:con.execute("UPDATE scene_renders SET state='FAILED',failure=?,updated_at=? WHERE id=? AND state!='CANCELLED'",(str(exc) if isinstance(exc,ValueError) else 'Scene render failed',utc_now(),rid))
        finally:
            with _process_lock:_processes.pop(rid,None)
            (folder/'composition.tmp.mp4').unlink(missing_ok=True)


def guard(fn):
    from auto_stream_engine import translate
    try:return fn()
    except Exception as exc:translate(exc)


@router.get('/assets')
def assets():
    from auto_stream_engine import service
    engine=service();items=[]
    if engine.media.exists():
        for path in sorted(engine.media.rglob('*')):
            if path.is_file() and path.suffix.lower() in EXTENSIONS and not path.is_symlink():
                try:asset(engine,str(path.relative_to(engine.media)))
                except ValueError:continue
                items.append({'media_path':str(path.relative_to(engine.media)),'name':path.name,'bytes':path.stat().st_size})
                if len(items)>=1000:break
    return {'media':items}


@router.get('/asset')
def asset_file(media_path:str):
    from auto_stream_engine import service
    return guard(lambda:FileResponse(asset(service(),media_path),headers={'Cache-Control':'private, no-store'}))


@router.post('/studio/preview')
def scene_preview(payload:dict=Body(...)):
    from auto_stream_engine import service
    engine=service()
    def run():
        doc=validate(engine,payload)
        digest=hashlib.sha256(json.dumps(doc,sort_keys=True).encode()).hexdigest()
        folder=engine.root/'previews'/digest;folder.mkdir(parents=True,exist_ok=True)
        output=folder/'preview.png'
        with _preview_lock:
            if not output.exists():
                try:
                    result=subprocess.run(compile_scene(engine,doc,folder,output,preview=True),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
                    if result.returncode:raise ValueError('FFmpeg scene preview failed; check source media')
                except subprocess.TimeoutExpired:raise ValueError('Preview timed out') from None
        return FileResponse(output,media_type='image/png',headers={'Cache-Control':'no-store'})
    return guard(run)


@router.post('/studio/render',status_code=202)
def render_scene(payload:dict=Body(...)):
    from auto_stream_engine import service,utc_now
    engine=service()
    def run():
        doc=validate(engine,payload);rid='render_'+uuid.uuid4().hex
        with engine._connect() as con:
            pending=con.execute("SELECT COUNT(*) FROM scene_renders WHERE state IN ('WAITING','PREPARING')").fetchone()[0]
            if pending>=5:raise ValueError('Render queue is full; wait or cancel a render')
            con.execute("INSERT INTO scene_renders(id,document,state,created_at,updated_at) VALUES(?,?,'WAITING',?,?)",(rid,json.dumps(doc),utc_now(),utc_now()))
        threading.Thread(target=render_worker,args=(engine,rid,doc),daemon=True,name='auto-stream-render').start()
        return read_render(engine,rid)
    return guard(run)


@router.get('/studio/renders/{render_id}')
def render_status(render_id:str):
    from auto_stream_engine import service
    return guard(lambda:read_render(service(),render_id))


@router.post('/studio/renders/{render_id}/cancel')
def cancel_render(render_id:str):
    from auto_stream_engine import service,utc_now
    engine=service()
    def run():
        row=read_render(engine,render_id)
        if row['state'] not in ('WAITING','PREPARING'):raise ValueError('Render is no longer running')
        with engine._connect() as con:con.execute("UPDATE scene_renders SET state='CANCELLED',updated_at=? WHERE id=?",(utc_now(),render_id))
        with _process_lock:
            proc=_processes.get(render_id)
            if proc:
                with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGTERM)
                threading.Thread(target=engine._terminate,args=(render_id,proc),daemon=True).start()
        return read_render(engine,render_id)
    return guard(run)


@router.get('/studio/renders')
def list_renders():
    from auto_stream_engine import service
    engine=service()
    with engine._connect() as con:
        rows=[dict(r) for r in con.execute('SELECT id,state,media_path,failure,created_at,updated_at FROM scene_renders ORDER BY created_at DESC LIMIT 30')]
    for row in rows:row['available']=bool(row['media_path'] and (engine.media/row['media_path']).is_file())
    return {'renders':rows}


@router.get('/assets/frame')
def media_frame(media_path:str):
    from auto_stream_engine import service
    engine=service()
    def run():
        source=asset(engine,media_path)
        identity=str(source)+':'+str(source.stat().st_mtime_ns)+':'+str(source.stat().st_size)
        folder=engine.root/'previews'/hashlib.sha256(identity.encode()).hexdigest();folder.mkdir(parents=True,exist_ok=True)
        output=folder/'frame.png'
        with _preview_lock:
            if not output.exists():
                try:
                    result=subprocess.run([shutil.which('ffmpeg') or 'ffmpeg','-nostdin','-v','error','-y','-i',str(source),'-vf','scale=854:-2','-frames:v','1','-threads','1',str(output)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
                    if result.returncode:raise ValueError('Video frame preview unavailable for this source')
                except subprocess.TimeoutExpired:raise ValueError('Video frame preview timed out') from None
        return FileResponse(output,media_type='image/png',headers={'Cache-Control':'private, max-age=60'})
    return guard(run)


@router.get('/assets/playback')
def browser_playback(media_path:str):
    """Codec fallback for browsers without AVC; the streaming master stays H.264."""
    from auto_stream_engine import service
    engine=service()
    def run():
        source=asset(engine,media_path)
        identity=str(source)+':'+str(source.stat().st_mtime_ns)+':'+str(source.stat().st_size)
        folder=engine.root/'previews'/hashlib.sha256(identity.encode()).hexdigest();folder.mkdir(parents=True,exist_ok=True)
        output=folder/'playback.webm'
        with _preview_lock:
            if not output.exists():
                temporary=folder/'playback.tmp.webm'
                try:
                    result=subprocess.run([shutil.which('ffmpeg') or 'ffmpeg','-nostdin','-v','error','-y','-i',str(source),'-t','30','-vf','scale=854:-2',
                        '-c:v','libvpx','-deadline','realtime','-cpu-used','5','-threads','2','-b:v','800k','-c:a','libopus','-b:a','64k',str(temporary)],
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
                    if result.returncode:raise ValueError('Browser playback preview is unavailable; composition frame remains available')
                    os.replace(temporary,output)
                except subprocess.TimeoutExpired:raise ValueError('Browser playback conversion timed out; composition frame remains available') from None
                finally:temporary.unlink(missing_ok=True)
        return FileResponse(output,media_type='video/webm',headers={'Cache-Control':'private, max-age=60'})
    return guard(run)

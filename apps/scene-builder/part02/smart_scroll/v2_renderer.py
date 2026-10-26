from __future__ import annotations
import os, subprocess, tempfile, threading, queue, time, signal, json, shutil, hashlib
from pathlib import Path
from typing import Any, Callable
from .ffmpeg_utils import require_ffmpeg, get_audio_duration
from .creative_v23 import normalize_scene_creative_v23 as normalize_scene_creative, scan_asset_library_v23, resolve_asset
from .scene_txt_pipeline import align_scenes_to_captions
from .persistence import user_data_dir
from .lut_engine import assign_scene_luts, lut_filter_for_scene
from .transition_engine import canonical_transition, resolve_xfade_transition, sanitize_xfade_transition, transition_uses_xfade

Progress=Callable[[int,str],None]|None; Cancel=Callable[[],bool]|None; Checkpoint=Callable[[dict[str,Any]],None]|None
VIDEO_EXTENSIONS={'.mp4','.mov','.mkv','.webm','.avi','.m4v'}; IMAGE_EXTENSIONS={'.png','.jpg','.jpeg','.webp','.bmp'}
class V2RenderCancelled(RuntimeError): pass


def _atempo_filters_for_speed(speed: float) -> list[str]:
    value=max(0.25,min(4.0,float(speed or 1.0)))
    parts=[]
    while value < 0.5:
        parts.append(0.5); value/=0.5
    while value > 2.0:
        parts.append(2.0); value/=2.0
    parts.append(value)
    return [f"atempo={part:.5f}" for part in parts if abs(part-1.0)>0.0001]


def _render_disk_guard(path: Path, label: str) -> None:
    minimum_gb=max(1.0,float(os.getenv('MIN_FREE_SPACE_GB','8') or 8))
    minimum=int(minimum_gb*1024*1024*1024)
    usage=shutil.disk_usage(path.parent if path.parent.exists() else Path('.'))
    if usage.free < minimum:
        raise RuntimeError(f'Storage safety guard blocked {label}: {usage.free/(1024**3):.1f} GB free; requires at least {minimum_gb:.1f} GB free.')


def _ffprobe_path(ffmpeg_path: str) -> str:
    ffmpeg_p = Path(ffmpeg_path)
    candidate = ffmpeg_p.with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
    if candidate.is_file():
        return str(candidate)
    found = shutil.which('ffprobe')
    if found:
        return found
    raise RuntimeError('FFprobe is required to validate the final MP4.')

def _validate_final_mp4(path: Path, ffmpeg_path: str, expected_duration: float = 0.0, expect_audio: bool = True) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size < 1024:
        raise RuntimeError('Final MP4 was not created or is too small.')
    probe = _ffprobe_path(ffmpeg_path)
    cmd = [probe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError('Final MP4 validation failed: ' + (result.stderr.strip() or 'FFprobe error'))
    try:
        data = json.loads(result.stdout or '{}')
    except Exception as exc:
        raise RuntimeError(f'Final MP4 validation returned invalid metadata: {exc}') from exc
    streams = data.get('streams') or []
    video = next((x for x in streams if x.get('codec_type') == 'video'), None)
    audio = next((x for x in streams if x.get('codec_type') == 'audio'), None)
    if not video:
        raise RuntimeError('Final MP4 has no readable video stream.')
    if str(video.get('codec_name') or '').lower() != 'h264':
        raise RuntimeError(f"Final MP4 video codec is {video.get('codec_name')}, expected H.264.")
    if str(video.get('pix_fmt') or '').lower() != 'yuv420p':
        raise RuntimeError(f"Final MP4 pixel format is {video.get('pix_fmt')}, expected yuv420p.")
    if int(video.get('width') or 0) <= 0 or int(video.get('height') or 0) <= 0:
        raise RuntimeError('Final MP4 has invalid video dimensions.')
    if expect_audio and not audio:
        raise RuntimeError('Final MP4 has no readable audio stream.')
    if audio and str(audio.get('codec_name') or '').lower() != 'aac':
        raise RuntimeError(f"Final MP4 audio codec is {audio.get('codec_name')}, expected AAC.")
    duration = float((data.get('format') or {}).get('duration') or video.get('duration') or 0.0)
    if duration <= 0:
        raise RuntimeError('Final MP4 has an invalid duration.')
    if expected_duration > 0 and abs(duration - expected_duration) > max(2.0, expected_duration * 0.02):
        raise RuntimeError(f'Final MP4 duration mismatch: {duration:.2f}s vs expected {expected_duration:.2f}s.')
    return {'duration': duration, 'width': int(video.get('width') or 0), 'height': int(video.get('height') or 0), 'video_codec': video.get('codec_name'), 'pixel_format': video.get('pix_fmt'), 'audio_codec': audio.get('codec_name') if audio else None}

def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

def _run(cmd:list[str], cancel:Cancel=None, *, timeout:float=0.0, stall_timeout:float=600.0, progress:Progress=None, progress_base:int=0, progress_span:int=1, expected_duration:float=0.0, label:str='FFmpeg')->None:
    # FFmpeg can deadlock if stderr is piped but not drained.  Use -progress on stdout
    # and drain both streams continuously in reader threads.
    run_cmd=list(cmd)
    if '-progress' not in run_cmd:
        run_cmd[1:1]=['-hide_banner','-nostdin','-nostats','-progress','pipe:1']
    creationflags=(0x08000000 | 0x00000200) if os.name=='nt' else 0
    proc=subprocess.Popen(
        run_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        bufsize=1, universal_newlines=True, creationflags=creationflags,
        start_new_session=(os.name!='nt')
    )
    events: queue.Queue[tuple[str,str]] = queue.Queue()
    stderr_tail:list[str]=[]

    def reader(stream, name):
        try:
            for line in iter(stream.readline, ''):
                events.put((name, line.rstrip()))
        finally:
            try: stream.close()
            except Exception: pass

    threading.Thread(target=reader,args=(proc.stdout,'stdout'),daemon=True).start()
    threading.Thread(target=reader,args=(proc.stderr,'stderr'),daemon=True).start()
    started=time.monotonic(); last_activity=started; last_heartbeat=0.0
    out_seconds=0.0
    while proc.poll() is None:
        if cancel and cancel():
            _kill_process_tree(proc)
            raise V2RenderCancelled('Render cancelled')
        now=time.monotonic()
        if timeout and now-started > timeout:
            _kill_process_tree(proc)
            raise RuntimeError(f'{label} exceeded its hard safety limit of {int(timeout)} seconds.')
        if stall_timeout and now-last_activity > stall_timeout:
            _kill_process_tree(proc)
            raise RuntimeError(f'{label} made no FFmpeg progress for {int(stall_timeout)} seconds.')
        try:
            source,line=events.get(timeout=.20)
            last_activity=now
            if source=='stderr' and line:
                stderr_tail.append(line); stderr_tail=stderr_tail[-80:]
            elif source=='stdout' and '=' in line:
                key,value=line.split('=',1)
                if key in {'out_time_ms','out_time_us'}:
                    try: out_seconds=float(value)/1_000_000.0
                    except Exception: pass
                elif key=='out_time':
                    try:
                        hh,mm,ss=value.split(':'); out_seconds=int(hh)*3600+int(mm)*60+float(ss)
                    except Exception: pass
        except queue.Empty:
            pass
        if progress and now-last_heartbeat >= 2.0:
            if expected_duration>0:
                fraction=max(0.0,min(.99,out_seconds/expected_duration))
                pct=progress_base+int(progress_span*fraction)
                progress(pct,f'{label}: {int(fraction*100)}%')
            else:
                progress(progress_base,f'{label}: working...')
            last_heartbeat=now
    # Drain any final queued output.
    while True:
        try:
            source,line=events.get_nowait()
            if source=='stderr' and line:
                stderr_tail.append(line); stderr_tail=stderr_tail[-80:]
        except queue.Empty:
            break
    if proc.returncode:
        err='\n'.join(stderr_tail[-50:])
        raise RuntimeError(err[-10000:] or f'FFmpeg failed ({proc.returncode})')
    if progress:
        progress(progress_base+progress_span,f'{label}: completed')



def _reconcile_scene_timeline(scenes:list[dict[str,Any]], caption_segments:list[dict[str,Any]], audio_duration:float, progress:Progress=None, narration_start:float=0.0)->list[dict[str,Any]]:
    """Make the narration audio the authoritative timeline before media rendering.

    Imported AI plans occasionally carry stale/estimated end times (for example a
    27-second scene timeline for a 120-second narration).  Rebuild timing from
    Whisper captions when available; otherwise scale the existing scene weights so
    the final scene ends exactly at the narration duration.
    """
    items=[dict(x) for x in scenes]
    total=max(.1,float(audio_duration or 0.0))
    if not items or total<=.1:
        return items
    current_end=max(float(x.get('end',0) or 0) for x in items)
    mismatch=abs(current_end-total)
    tolerance=max(1.0,total*.02)
    if mismatch<=tolerance:
        # Still force the final boundary to the exact audio end.
        items[-1]['end']=round(total,3)
        items[-1]['duration']=round(max(.01,total-float(items[-1].get('start',0) or 0)),3)
        return items
    if caption_segments:
        # R6: synthetic uploaded-audio pre-roll scenes are not narration and must
        # never be re-aligned to Whisper. Preserve them, then rebuild only the
        # documentary portion from the absolute speech start.
        preroll=[dict(x) for x in items if bool(x.get('embedded_audio_preroll'))]
        documentary=[dict(x) for x in items if not bool(x.get('embedded_audio_preroll'))]
        fixed=align_scenes_to_captions(documentary,caption_segments,total,timeline_start=max(0.0,float(narration_start or 0.0)))
        if progress:
            progress(0,f'Rebuilt scene timing from Whisper: {current_end:.2f}s -> {total:.2f}s')
        return preroll+fixed
    # No caption timing: preserve relative scene lengths, but expand the timeline to
    # the narration duration instead of ending early.
    weights=[]
    for item in items:
        d=max(.01,float(item.get('end',0) or 0)-float(item.get('start',0) or 0))
        if d<=.011:
            d=max(1.0,len(str(item.get('text','')).split()))
        weights.append(d)
    weight_total=sum(weights) or float(len(items))
    cursor=0.0
    for idx,(item,weight) in enumerate(zip(items,weights)):
        start=cursor
        end=total if idx==len(items)-1 else min(total,cursor+total*(weight/weight_total))
        item['start']=round(start,3); item['end']=round(end,3); item['duration']=round(max(.01,end-start),3)
        item['timing_source']='audio_duration_reconciled'
        cursor=end
    if progress:
        progress(0,f'Expanded scene timing to narration: {current_end:.2f}s -> {total:.2f}s')
    return items

def _duration(s): return max(.05,float(s.get('end',0) or 0)-float(s.get('start',0) or 0))

def _creative_library() -> dict[str, list[dict[str, Any]]]:
    """Merge bundled assets with persistent user assets for real render use."""
    roots = [
        Path(__file__).resolve().parent.parent / "assets",
        user_data_dir("Stock Media Scene Builder") / "assets",
    ]
    merged: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for root in roots:
        try:
            library = scan_asset_library_v23(root)
        except Exception:
            continue
        for category, items in (library or {}).items():
            bucket = merged.setdefault(str(category), [])
            for item in items or []:
                path = str(item.get("_file_path", "") or item.get("path", "") or "")
                key = (str(category), path.lower())
                if key in seen:
                    continue
                seen.add(key)
                bucket.append(dict(item))
    return merged

def _filter(name:str,intensity:float)->str:
    i=max(0,min(1,float(intensity))); name=(name or 'none').lower()
    return {
      'none':'null','warm':f'colorbalance=rs={.12*i}:gs={.035*i}:bs={-.08*i}',
      'cool':f'colorbalance=rs={-.08*i}:gs={.02*i}:bs={.12*i}',
      'high_contrast':f'eq=contrast={1+.55*i}:saturation={1+.15*i}', 'soft':f'gblur=sigma={1.8*i}',
      'bright':f'eq=brightness={.22*i}', 'dark':f'eq=brightness={-.22*i}',
      'vintage':f'curves=vintage,eq=saturation={1-.35*i}', 'documentary':f'eq=contrast={1+.25*i}:saturation={1-.18*i},unsharp=5:5:{.5*i}',
      'cinematic':f'eq=contrast={1+.3*i}:saturation={1+.08*i},colorbalance=bs={-.05*i}:rs={.05*i}',
      'black_white':f'eq=saturation={1-i}', 'sepia':f'colorchannelmixer={1-.607*i}:{.769*i}:{.189*i}:{.349*i}:{1-.314*i}:{.168*i}:{.272*i}:{.534*i}:{1-.869*i}',
      'desaturated':f'eq=saturation={1-.8*i}','vivid':f'eq=saturation={1+.75*i}:contrast={1+.18*i}',
      'sharpen':f'unsharp=5:5:{.8*i}:5:5:0',
      'teal_orange':f'eq=contrast={1+.18*i}:saturation={1+.12*i},colorbalance=bs={.06*i}:gs={.015*i}:rs={.08*i}',
      'natural_pop':f'eq=contrast={1+.16*i}:saturation={1+.22*i}:brightness={.015*i},unsharp=5:5:{.35*i}:5:5:0',
      'moody_green':f'eq=contrast={1+.20*i}:saturation={1-.12*i}:brightness={-.025*i},colorbalance=gs={.06*i}:bs={-.025*i}',
      'clean_premium':f'eq=contrast={1+.14*i}:saturation={1+.08*i}:brightness={.012*i},unsharp=5:5:{.45*i}:5:5:0'
    }.get(name,'null')


def _global_quality_filter(settings: Any) -> str:
    profile = str(getattr(settings, 'video_quality_preset', 'enhanced') or 'enhanced').lower()
    if profile == 'original':
        return 'null'
    if profile == 'cinematic':
        return 'hqdn3d=1.0:1.0:4.0:4.0,eq=contrast=1.07:saturation=1.04:brightness=0.006,unsharp=5:5:0.34:5:5:0'
    if profile == '4k_premium':
        return 'hqdn3d=1.15:1.15:5.0:5.0,eq=contrast=1.08:saturation=1.06:brightness=0.006,unsharp=5:5:0.48:5:5:0'
    return 'hqdn3d=0.7:0.7:3.0:3.0,eq=contrast=1.04:saturation=1.025,unsharp=5:5:0.24:5:5:0'



def _resolved_color_filters(scene: dict[str, Any], settings: Any, lut_chain: str) -> tuple[str, str]:
    """Deterministic grade order: LUT first, explicitly selected scene filter next.

    Global cleanup cannot overwrite the owner's selected colour grade. Legacy
    saved selections remain valid even when a LUT is enabled.
    """
    requested = str(scene.get('filter', 'none') or 'none').lower()
    scene_filter = _filter(requested, float(scene.get('filter_intensity', .5)))
    lut_active = bool(lut_chain and lut_chain != 'null')
    profile = str(getattr(settings, 'video_quality_preset', 'enhanced') or 'enhanced').lower()
    if profile == 'original':
        quality = 'null'
    elif lut_active:
        if profile == '4k_premium': quality = 'hqdn3d=1.15:1.15:5.0:5.0,unsharp=5:5:0.48:5:5:0'
        elif profile == 'cinematic': quality = 'hqdn3d=1.0:1.0:4.0:4.0,unsharp=5:5:0.34:5:5:0'
        else: quality = 'hqdn3d=0.7:0.7:3.0:3.0,unsharp=5:5:0.24:5:5:0'
    else:
        quality = _global_quality_filter(settings)
    return scene_filter, quality


def _ass_color(value: str, alpha: int = 0) -> str:
    raw = str(value or '#FFFFFF').strip().lstrip('#')
    if len(raw) != 6: raw = 'FFFFFF'
    r, g, b = raw[0:2], raw[2:4], raw[4:6]
    return f'&H{max(0,min(255,int(alpha))):02X}{b}{g}{r}&'


def _ass_escape(text: str) -> str:
    return str(text or '').replace('\\', r'\\').replace('{', r'\{').replace('}', r'\}').replace('\n', r'\N')


def _write_ass(segments: list[dict[str, Any]], path: Path, settings: Any, width: int, height: int) -> bool:
    def ts(v: float) -> str:
        cs=max(0,int(round(float(v)*100))); h,cs=divmod(cs,360000); m,cs=divmod(cs,6000); s,cs=divmod(cs,100)
        return f'{h:d}:{m:02d}:{s:02d}.{cs:02d}'
    events=[]
    karaoke=bool(getattr(settings,'caption_karaoke_enabled',False))
    for x in segments:
        text=str(x.get('text','') or '').strip()
        if text:
            start=float(x.get('start',0) or 0); end=float(x.get('end',0) or 0)
            event_text=_ass_escape(text)
            if karaoke:
                words=[w for w in text.split() if w]
                timings=[w for w in (x.get('words') or []) if isinstance(w,dict)]
                if words and len(timings)==len(words):
                    valid = all('start' in w and 'end' in w for w in timings)
                    starts = [max(start, min(end, float(w['start']))) for w in timings] if valid else []
                    valid = valid and all(starts[i] <= starts[i+1] for i in range(len(starts)-1))
                    if valid:
                        parts=[]
                        first_cs=max(0,int(round((starts[0]-start)*100)))
                        if first_cs:
                            parts.append(r'{\k%d}{\alpha&HFF&}\h{\alpha&H00&}' % first_cs)
                        for idx,word in enumerate(words):
                            next_start=starts[idx+1] if idx+1<len(starts) else end
                            centiseconds=max(1,int(round(max(.01,next_start-starts[idx])*100)))
                            parts.append(r'{\kf%d}%s' % (centiseconds,_ass_escape(word)))
                        event_text=' '.join(parts)
                elif len(words)>1:
                    # Explicit fallback only for older captions without word timestamps.
                    total_cs=max(len(words),int(round(max(.1,end-start)*100)))
                    base=max(1,total_cs//len(words)); remain=max(0,total_cs-base*len(words))
                    event_text=' '.join(r'{\kf%d}%s' % (base+(1 if idx<remain else 0),_ass_escape(word))
                                        for idx,word in enumerate(words))
            events.append(f"Dialogue: 0,{ts(start)},{ts(end)},Default,,0,0,0,,{event_text}")
    if not events: return False
    position=str(getattr(settings,'caption_position','bottom') or 'bottom').lower()
    align={'bottom':2,'center':5,'top':8}.get(position,2)
    font=str(getattr(settings,'font_family','Arial') or 'Arial').replace(',', ' ')
    size=max(10,int(getattr(settings,'font_size',48) or 48))
    bold=-1 if bool(getattr(settings,'bold',False)) else 0
    italic=-1 if bool(getattr(settings,'italic',False)) else 0
    underline=-1 if bool(getattr(settings,'underline',False)) else 0
    primary=_ass_color(str(getattr(settings,'text_color','#FFFFFF') or '#FFFFFF'))
    secondary=_ass_color(str(getattr(settings,'caption_highlight_color','#FFD54A') or '#FFD54A'))
    outline=_ass_color(str(getattr(settings,'outline_color','#000000') or '#000000'))
    box_enabled=bool(getattr(settings,'text_background_enabled',False))
    box_color=str(getattr(settings,'text_background_color','#000000') or '#000000')
    raw_opacity=getattr(settings,'text_background_opacity_percent',55)
    opacity=max(0,min(100,int(55 if raw_opacity is None else raw_opacity)))
    alpha=255-int(round(opacity*2.55))
    back=_ass_color(box_color, alpha=alpha)
    border_style=3 if box_enabled else 1
    outline_width=max(0,int(getattr(settings,'outline_width',2) or 0)) if bool(getattr(settings,'outline_enabled',False)) else 0
    shadow=2 if bool(getattr(settings,'shadow_enabled',True)) else 0
    margin_v=max(20,int(height*0.055))
    style=f"Style: Default,{font},{size},{primary},{secondary},{outline},{back},{bold},{italic},{underline},0,100,100,0,0,{border_style},{outline_width},{shadow},{align},60,60,{margin_v},1"
    body='\n'.join([
        '[Script Info]', 'ScriptType: v4.00+', f'PlayResX: {width}', f'PlayResY: {height}', 'ScaledBorderAndShadow: yes', '',
        '[V4+ Styles]', 'Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding', style, '',
        '[Events]', 'Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text', *events, ''
    ])
    path.write_text(body,encoding='utf-8-sig')
    return True


def _effect(name:str,intensity:float)->str:
    i=max(0,min(1,float(intensity))); name=(name or 'none').lower()
    return {
      'none':'null',
      'subtle_pulse':f'eq=brightness={.025*i}:contrast={1+.06*i}',
      'soft_glow':f'gblur=sigma={.7*i},eq=brightness={.035*i}',
      'edge_vignette':f'vignette=PI/5*{max(.1,i)}',
      'gentle_shake':f'crop=iw-{max(2,int(8*i))}:ih-{max(2,int(8*i))}:x={max(1,int(4*i))}*sin(n/3):y={max(1,int(4*i))}*cos(n/4),scale=iw+{max(2,int(8*i))}:ih+{max(2,int(8*i))}',
      'cinematic_grain':f'noise=alls={max(1.0,8.0*i):.2f}:allf=t+u',
      'bloom':f'gblur=sigma={max(.15,.55*i):.3f},eq=brightness={.022*i}:saturation={1+.06*i}'
    }.get(name,'null')

def _canvas_fill_chains(media_input: int, index: int, width: int, height: int,
                        mode: str, blur_strength: int, color: str) -> tuple[list[str], str]:
    """Normalize a scene's source into its target canvas before editorial effects.

    Legacy crop is intentionally unchanged for old saved projects. Blur uses one
    decoded input split into cover/background + contain/foreground, so videos
    stay synchronized without a second decode or a stretched foreground.
    """
    mode = str(mode or "crop").lower()
    source = f"[{media_input}:v]"
    target = f"[canvas{index}]"
    if mode == "crop":
        return [], source
    if mode == "stretch":
        return [f"{source}scale={width}:{height},setsar=1{target}"], target
    if mode in {"black", "solid"}:
        valid_color = str(color or "").lstrip("#")
        if len(valid_color) != 6 or any(c not in "0123456789abcdefABCDEF" for c in valid_color):
            valid_color = "101018"
        fill = "0x000000" if mode == "black" else "0x" + valid_color
        return [
            f"{source}scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"setsar=1,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={fill}{target}"
        ], target
    if mode == "blur":
        sigma = max(0, min(40, int(blur_strength)))
        bg = f"[canvasbg{index}]"
        fg = f"[canvasfg{index}]"
        return [
            f"{source}split=2[canvasbgsrc{index}][canvasfgsrc{index}]",
            f"[canvasbgsrc{index}]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1"
            + (f",gblur=sigma={sigma}:steps=1" if sigma else "") + bg,
            f"[canvasfgsrc{index}]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"setsar=1{fg}",
            f"{bg}{fg}overlay=x=(W-w)/2:y=(H-h)/2:shortest=1,setsar=1{target}",
        ], target
    raise ValueError(f"Unsupported Canvas Fill mode: {mode}")



def _capcut_selected_scene_indices(scenes: list[dict[str, Any]], percent: int,
                                  *, target: str) -> set[int]:
    """Pick whole scenes (never frames) with a reproducible exact coverage quota."""
    percent = max(0, min(100, int(percent)))
    if percent == 0:
        return set()
    target = str(target).lower()
    eligible = []
    for index, scene in enumerate(scenes):
        filename = str(scene.get('media_path') or scene.get('video_candidate_path') or '')
        ext = Path(filename).suffix.lower()
        if (target == 'videos' and ext in VIDEO_EXTENSIONS) or (
            target == 'images' and ext in IMAGE_EXTENSIONS
        ) or (target == 'both' and ext in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS):
            eligible.append(index)
    count = (len(eligible) * percent + 50) // 100
    ranked = sorted(
        eligible,
        key=lambda index: hashlib.sha256(
            (str(index) + ':' + str(scenes[index].get('scene_code') or '') + ':' +
             str(scenes[index].get('media_path') or '')).encode('utf-8')
        ).digest(),
    )
    return set(ranked[:count])


def _capcut_zoom_chains(source: str, index: int, width: int, height: int,
                       zoom_level: int, canvas_mode: str, fill_color: str,
                       blur_strength: int) -> tuple[list[str], str]:
    """Static 50–300% transform, before text/overlays, without stretching the canvas."""
    zoom = max(50, min(300, int(zoom_level)))
    if zoom == 100:
        return [], source
    output = f'[capcutzoom{index}]'
    scaled_w = max(2, (width * zoom // 100) // 2 * 2)
    scaled_h = max(2, (height * zoom // 100) // 2 * 2)
    if zoom > 100:
        return [f'{source}scale={scaled_w}:{scaled_h},crop={width}:{height},setsar=1{output}'], output

    color = str(fill_color or '#101018').lstrip('#')
    if len(color) != 6 or any(ch not in '0123456789abcdefABCDEF' for ch in color):
        color = '101018'
    if str(canvas_mode or 'crop') == 'blur':
        sigma = max(0, min(40, int(blur_strength)))
        background = f'[capcutzoombg{index}]'
        foreground = f'[capcutzoomfg{index}]'
        return [
            f'{source}split=2[capcutzoombgsrc{index}][capcutzoomfgsrc{index}]',
            f'[capcutzoombgsrc{index}]' +
            (f'gblur=sigma={sigma}:steps=1' if sigma else 'null') + background,
            f'[capcutzoomfgsrc{index}]scale={scaled_w}:{scaled_h}{foreground}',
            f'{background}{foreground}overlay=x=(W-w)/2:y=(H-h)/2:shortest=1,setsar=1{output}',
        ], output
    fill = '0x' + color if canvas_mode == 'solid' else '0x000000'
    return [
        f'{source}scale={scaled_w}:{scaled_h},'
        f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={fill},setsar=1{output}'
    ], output


def _capcut_grain_filter(effect: str, intensity: int) -> str:
    """Bounded FFmpeg source-media texture; captions and overlays remain clean."""
    amount = max(0, min(100, int(intensity)))
    if amount == 0:
        return 'null'
    effect = str(effect or 'film').lower()
    if effect == 'white':
        return f'noise=alls={max(1,int(amount*.28))}:allf=t+u,eq=brightness={amount*.0004:.4f}'
    if effect == 'black':
        return f'noise=alls={max(1,int(amount*.25))}:allf=t+u,eq=brightness={-amount*.00045:.4f}'
    if effect == 'film':
        return f'noise=alls={max(1,int(amount*.16))}:allf=t+u,eq=saturation={1-amount*.0009:.4f}'
    if effect == 'analog':
        return (
            f'noise=alls={max(1,int(amount*.42))}:allf=t+u,'
            f'drawgrid=w=iw:h=6:t=1:c=white@{amount*.0025:.3f}'
        )
    if effect == 'dust':
        return (
            f'noise=alls={max(1,int(amount*.13))}:allf=t+u,'
            f'drawbox=x=iw/5:y=0:w=1:h=ih:color=white@{amount*.004:.3f}:t=fill,'
            f'drawbox=x=iw*4/5:y=0:w=1:h=ih:color=black@{amount*.003:.3f}:t=fill'
        )
    raise ValueError(f'Unsupported Noise / Grain effect: {effect}')


from .capcut_animation import capcut_animation_filter, validate_animation


def _motion(scene:dict[str,Any],w:int,h:int,fps:int,dur:float,is_image:bool)->str:
    """Frame-animated pan/zoom for both stills and videos (no static substitution)."""
    m = str(scene.get('motion','none') or 'none').lower()
    intensity = max(0.0, min(1.0, float(scene.get('motion_intensity',.35))))
    base = f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fps}'
    if m == 'none' or intensity <= 0:
        return base
    frames = max(2, int(round(max(.1, dur) * fps)))
    t = f'min(1,on/{frames-1})'
    image_names = {'zoom_in', 'zoom_out', 'pan_left', 'pan_right', 'ken_burns'}
    video_names = {'slow_push', 'reframe_left', 'reframe_right', 'gentle_zoom'}
    if m not in (image_names if is_image else video_names):
        return base
    amount = 0.045 + intensity * 0.105
    if m == 'zoom_in':
        z = f'1+{amount:.6f}*{t}'; x = '(iw-iw/zoom)/2'; y = '(ih-ih/zoom)/2'
    elif m == 'zoom_out':
        z = f'{1+amount:.6f}-{amount:.6f}*{t}'; x = '(iw-iw/zoom)/2'; y = '(ih-ih/zoom)/2'
    elif m in {'pan_left', 'reframe_left'}:
        z = f'{1+amount:.6f}'; x = f'(iw-iw/zoom)*(1-{t})'; y = '(ih-ih/zoom)/2'
    elif m in {'pan_right', 'reframe_right'}:
        z = f'{1+amount:.6f}'; x = f'(iw-iw/zoom)*{t}'; y = '(ih-ih/zoom)/2'
    elif m == 'ken_burns':
        z = f'1+{amount:.6f}*{t}'; x = f'(iw-iw/zoom)*{t}'; y = f'(ih-ih/zoom)*(1-{t})'
    else:
        speed = .75 if m == 'gentle_zoom' else 1.0
        z = f'1+{amount*speed:.6f}*{t}'; x = '(iw-iw/zoom)/2'; y = '(ih-ih/zoom)/2'
    # d=1 is critical with -loop 1 image input: d=duration*fps would
    # duplicate every looped input frame and slow full-length renders enormously.
    return (
        f'fps={fps},scale={w*2}:{h*2}:force_original_aspect_ratio=increase,'
        f'crop={w*2}:{h*2},setsar=1,'
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps}"
    )


def _xfade_name(scene_or_name: Any, boundary_index: int = 0) -> str:
    if isinstance(scene_or_name, dict):
        logical = scene_or_name.get('transition_out') or scene_or_name.get('transition') or 'cross_dissolve'
        if not transition_uses_xfade(logical):
            raise ValueError(f"{canonical_transition(logical)} is a cut boundary and has no xfade name")
        # An old saved transition_xfade='fade' must never mask the real selected
        # visual. Re-resolve from the preserved logical name and valid variant.
        return resolve_xfade_transition(logical, boundary_index, str(scene_or_name.get('transition_variant') or ''))
    return resolve_xfade_transition(scene_or_name, boundary_index)


def _xfade_filter(scene: dict[str, Any], index: int, duration: float, offset: float) -> str:
    """Use the real named effect; custom spin and zoom-blur are frame-animated."""
    name = canonical_transition(scene.get('transition_out', 'cross_dissolve'))
    effect = _xfade_name(scene, index)
    options = f'duration={duration:.6f}:offset={offset:.6f}'
    if name == 'soft_spin':
        # Rotating source coordinates across the boundary, switching at halfway.
        expr = ("if(lt(P,0.5),"
                "a0(W/2+(X-W/2)*cos(PI*P)-(Y-H/2)*sin(PI*P),"
                "H/2+(X-W/2)*sin(PI*P)+(Y-H/2)*cos(PI*P)),"
                "b0(W/2+(X-W/2)*cos(PI*(1-P))+(Y-H/2)*sin(PI*(1-P)),"
                "H/2-(X-W/2)*sin(PI*(1-P))+(Y-H/2)*cos(PI*(1-P))))")
        return f"xfade=transition=custom:{options}:expr='{expr}'"
    if name == 'zoom_blur':
        expr = ("a0(X,Y)*(1-P)+"
                "b0(W/2+(X-W/2)*(1.2-0.2*P),H/2+(Y-H/2)*(1.2-0.2*P))*P")
        return f"xfade=transition=custom:{options}:expr='{expr}'"
    return f'xfade=transition={effect}:{options}'


def _write_srt(segments,path):
    def ts(v):
        ms=max(0,int(round(v*1000))); h,ms=divmod(ms,3600000); m,ms=divmod(ms,60000); s,ms=divmod(ms,1000); return f'{h:02}:{m:02}:{s:02},{ms:03}'
    lines=[]
    for i,x in enumerate(segments,1):
        text=str(x.get('text','')).strip()
        if text: lines += [str(i),f"{ts(float(x.get('start',0)))} --> {ts(float(x.get('end',0)))}",text,'']
    path.write_text('\n'.join(lines),encoding='utf-8'); return bool(lines)


def _transition_seconds(scene: dict[str, Any], current_duration: float, next_duration: float, fps: float = 30.0) -> float:
    if not transition_uses_xfade(scene.get("transition_out", "none")):
        return 0.0
    requested = max(0.001, float(scene.get("transition_duration_ms", 350) or 350) / 1000.0)
    return min(requested, max(0.001, current_duration / 2.0), max(0.001, next_duration / 2.0))


def _boundary_engine(scene: dict[str, Any]) -> str:
    """Return the frozen execution engine for one scene boundary."""
    declared = str(scene.get("transition_engine") or "").strip().lower()
    logical = canonical_transition(scene.get("transition_out", "none"))
    if logical in {"none", "hard_cut"}:
        return "cut"
    if declared == "cut":
        # Never let a stale/invalid resolved plan silently discard a real transition.
        return "xfade"
    return "xfade"

def _checkpoint_write(path:Path,data:dict[str,Any],callback:Checkpoint=None)->None:
    data['updated_at_epoch']=time.time()
    temporary=path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)
    if callback:
        callback(dict(data))


def _checkpoint_load(path:Path)->dict[str,Any]:
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        return dict(value) if isinstance(value,dict) else {}
    except Exception:
        return {}


def _render_fingerprint(scenes:list[dict[str,Any]],settings:Any)->tuple[str,list[str]]:
    scene_fingerprints=[]
    for scene in scenes:
        source=Path(str(scene.get('media_path') or scene.get('video_candidate_path') or ''))
        stat=source.stat() if source.is_file() else None
        payload={
            'scene':scene,
            'media_size':stat.st_size if stat else 0,
            'media_mtime_ns':stat.st_mtime_ns if stat else 0,
        }
        scene_fingerprints.append(hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest())
    global_payload={
        'renderer':'v21-capcut-combo-cam-motion',
        'capcut':{key:getattr(settings,key,None) for key in (
            'capcut_mirror_enabled','capcut_mirror_percent','capcut_zoom_level',
            'capcut_noise_enabled','capcut_noise_effect','capcut_noise_intensity',
            'capcut_noise_percent','capcut_noise_target',
            'capcut_animation_category','capcut_animation_effect',
            'capcut_animation_duration','capcut_animation_intensity','capcut_animation_target')},
        'canvas_fill_mode':str(getattr(settings,'canvas_fill_mode','crop')),
        'canvas_blur_strength':int(getattr(settings,'canvas_blur_strength',16)),
        'canvas_fill_color':str(getattr(settings,'canvas_fill_color','#101018')),
        'scenes':scene_fingerprints,
        'width':int(settings.width),'height':int(settings.height),'fps':int(settings.fps),
        'quality_crf':int(getattr(settings,'quality_crf',22) or 22),
        'lut_enabled':bool(getattr(settings,'lut_enabled',True)),
        'lut_strength':int(40 if getattr(settings,'lut_strength_percent',40) is None else getattr(settings,'lut_strength_percent',40)),
        'lut_pack':str(getattr(settings,'lut_pack','cinematic') or 'cinematic'),
        'lut_preset':str(getattr(settings,'lut_preset','filmic') or 'filmic'),
        'lut_mode':str(getattr(settings,'lut_mode','auto') or 'auto'),
        'lut_normalize':bool(getattr(settings,'lut_normalize_enabled',True)),
    }
    return hashlib.sha256(json.dumps(global_payload,sort_keys=True).encode()).hexdigest(),scene_fingerprints


def _valid_checkpoint_video(path:Path,ffmpeg:str,expected_duration:float)->bool:
    try:
        _validate_final_mp4(path,ffmpeg,expected_duration=expected_duration,expect_audio=False)
        return True
    except Exception:
        return False


def render_mixed_media(settings:Any,progress_callback:Progress=None,cancel_callback:Cancel=None,checkpoint_callback:Checkpoint=None)->str:
    ffmpeg=require_ffmpeg(); meta=dict(getattr(settings,'metadata',{}) or {}); scenes=[normalize_scene_creative(dict(x)) for x in meta.get('scene_sync_plan',[]) if isinstance(x,dict)]
    if not scenes: raise ValueError('No scene plan is available for V2.3 render.')
    audio=Path(str(getattr(settings,'render_audio_path','') or getattr(settings,'audio_path','') or ''))
    audio_duration=get_audio_duration(str(audio)) if audio.is_file() else 0.0
    captions=[dict(x) for x in meta.get('caption_segments',[]) if isinstance(x,dict)]
    scenes=_reconcile_scene_timeline(scenes,captions,audio_duration,progress_callback,float(meta.get('narration_start_seconds',0.0) or 0.0))
    meta['scene_sync_plan']=[dict(x) for x in scenes]
    try: settings.metadata=meta
    except Exception: pass

    out=Path(str(settings.output_path)); out.parent.mkdir(parents=True,exist_ok=True); w,h,fps=int(settings.width),int(settings.height),int(settings.fps)
    root=out.parent/'render_checkpoint'; manifest_path=root/'checkpoint.json'
    fingerprint,scene_fingerprints=_render_fingerprint(scenes,settings)
    checkpoint=_checkpoint_load(manifest_path)
    if checkpoint.get('fingerprint')!=fingerprint:
        shutil.rmtree(root,ignore_errors=True); root.mkdir(parents=True,exist_ok=True)
        out.unlink(missing_ok=True)
        checkpoint={
            'version':1,'fingerprint':fingerprint,'scene_count':len(scenes),
            'completed_scenes':[],'completed_chunks':[],'chunk_fingerprints':{},
            'last_completed_scene':0,'visual_ready':False,'final_ready':False,
        }
        _checkpoint_write(manifest_path,checkpoint,checkpoint_callback)
    else:
        root.mkdir(parents=True,exist_ok=True)

    audio_stat=audio.stat() if audio.is_file() else None
    final_payload={
        'audio_size':audio_stat.st_size if audio_stat else 0,
        'audio_mtime_ns':audio_stat.st_mtime_ns if audio_stat else 0,
        'captions':captions,
        'captions_enabled':bool(getattr(settings,'captions_enabled',False)),
        'caption_style':str(getattr(settings,'caption_style','karaoke') or 'karaoke'),
        'caption_position':str(getattr(settings,'caption_position','bottom') or 'bottom'),
        'caption_font':str(getattr(settings,'font_family','Arial') or 'Arial'),
        'caption_size':int(getattr(settings,'font_size',48) or 48),
        'caption_colors':[
            str(getattr(settings,'text_color','#FFFFFF') or '#FFFFFF'),
            str(getattr(settings,'caption_highlight_color','#FFD54A') or '#FFD54A'),
            str(getattr(settings,'outline_color','#000000') or '#000000'),
            str(getattr(settings,'text_background_color','#000000') or '#000000'),
        ],
        'caption_box':[
            bool(getattr(settings,'text_background_enabled',False)),
            int(getattr(settings,'text_background_opacity_percent',55) or 0),
            bool(getattr(settings,'outline_enabled',False)),
            int(getattr(settings,'outline_width',2) or 0),
            bool(getattr(settings,'shadow_enabled',True)),
            bool(getattr(settings,'caption_karaoke_enabled',False)),
        ],
    }
    final_fingerprint=hashlib.sha256(json.dumps(final_payload,sort_keys=True,default=str).encode()).hexdigest()
    if checkpoint.get('final_fingerprint')!=final_fingerprint:
        checkpoint['final_fingerprint']=final_fingerprint
        checkpoint['final_ready']=False
        out.unlink(missing_ok=True)
        _checkpoint_write(manifest_path,checkpoint,checkpoint_callback)

    chunk_root=root/'timeline_chunks'
    chunk_root.mkdir(exist_ok=True)
    library=_creative_library()
    lut_choices=assign_scene_luts(scenes,settings); lut_cache:dict[str,Path]={}
    mirror_indices = _capcut_selected_scene_indices(
        scenes, int(getattr(settings,'capcut_mirror_percent',0)), target='videos'
    ) if bool(getattr(settings,'capcut_mirror_enabled',False)) else set()
    noise_indices = _capcut_selected_scene_indices(
        scenes, int(getattr(settings,'capcut_noise_percent',40)),
        target=str(getattr(settings,'capcut_noise_target','both')),
    ) if bool(getattr(settings,'capcut_noise_enabled',False)) else set()
    durations=[_duration(scene) for scene in scenes]
    transition_durations=[
        _transition_seconds(scenes[i],durations[i],durations[i+1],fps) if i<len(scenes)-1 else 0.0
        for i in range(len(scenes))
    ]
    intermediate_preset=str(getattr(settings,'intermediate_render_preset','superfast') or 'superfast')
    if intermediate_preset not in {'ultrafast','superfast','veryfast'}: intermediate_preset='superfast'
    intermediate_crf=max(16,min(23,int(getattr(settings,'quality_crf',22) or 22)-2))
    chunk_size=max(4,min(30,int(getattr(settings,'render_chunk_scenes',12) or 12)))
    groups=[(start,min(len(scenes),start+chunk_size)) for start in range(0,len(scenes),chunk_size)]
    checkpoint['chunk_scene_count']=chunk_size; checkpoint['total_chunks']=len(groups)
    completed_chunks={int(value) for value in checkpoint.get('completed_chunks',[]) if str(value).isdigit()}
    timeline_chunks=[]; chunk_durations=[]
    for group_index,(start,end) in enumerate(groups):
        if cancel_callback and cancel_callback(): raise V2RenderCancelled('Render cancelled')
        inbound=transition_durations[start-1] if start>0 else 0.0
        outbound=transition_durations[end-1] if end<len(scenes) else 0.0
        expected=sum(durations[start:end])-inbound+outbound
        chunk_durations.append(expected)
        target=chunk_root/f'chunk_{group_index:04d}.mp4'; timeline_chunks.append(target)
        chunk_fp=hashlib.sha256(json.dumps({
            'algorithm':'v18-motion-transitions-colour-verified',
            'scenes':scene_fingerprints[start:end+1 if end<len(scenes) else end],
            'transitions':transition_durations[start:end],
            'inbound':inbound,'outbound':outbound,
            'preset':intermediate_preset,'crf':intermediate_crf,
            'capcut':{
                'flip':sorted(mirror_indices.intersection(range(start,end+1))),
                'grain':sorted(noise_indices.intersection(range(start,end+1))),
                'zoom':int(getattr(settings,'capcut_zoom_level',100)),
                'effect':str(getattr(settings,'capcut_noise_effect','film')),
                'intensity':int(getattr(settings,'capcut_noise_intensity',35)),
                'animation':{
                    key:getattr(settings,key,None) for key in (
                        'capcut_animation_category','capcut_animation_effect',
                        'capcut_animation_duration','capcut_animation_intensity',
                        'capcut_animation_target')},
            },
            'canvas':(str(getattr(settings,'canvas_fill_mode','crop')),
                      int(getattr(settings,'canvas_blur_strength',16)),
                      str(getattr(settings,'canvas_fill_color','#101018'))),
        },sort_keys=True).encode()).hexdigest()
        reusable=group_index in completed_chunks and checkpoint.get('chunk_fingerprints',{}).get(str(group_index))==chunk_fp and _valid_checkpoint_video(target,ffmpeg,expected)
        if reusable:
            if progress_callback: progress_callback(68+int((group_index+1)/len(groups)*14),f'Reusing render chunk {group_index+1}/{len(groups)}')
            continue
        target.unlink(missing_ok=True); partial=chunk_root/f'chunk_{group_index:04d}.partial.mp4'; partial.unlink(missing_ok=True)
        cmd=[ffmpeg,'-y']; chains=[]; member_count=end-start
        render_indices=list(range(start,end))+([end] if end<len(scenes) and outbound>0 else [])
        for local_index,global_index in enumerate(render_indices):
            scene=scenes[global_index]
            source=Path(str(scene.get('media_path') or scene.get('video_candidate_path') or ''))
            code=str(scene.get('scene_code',f'SCENE_{global_index+1:03d}'))
            if not source.is_file(): raise ValueError(f'{code}: media file is missing.')
            ext=source.suffix.lower(); is_image=ext in IMAGE_EXTENSIONS
            input_need=outbound if global_index==end else durations[global_index]+transition_durations[global_index]
            media_input=sum(1 for value in cmd if value=='-i')
            if is_image: cmd+=['-loop','1','-t',f'{max(.05,input_need):.6f}','-i',str(source)]
            elif ext in VIDEO_EXTENSIONS: cmd+=['-stream_loop','-1','-i',str(source)]
            else: raise ValueError(f'{code}: unsupported media format {ext}')
            lut_chain,lut_label=lut_filter_for_scene(scene,lut_choices[global_index],settings,root,lut_cache)
            if bool(getattr(settings,'lut_enabled',True)) and lut_label.startswith('Missing LUT:'): raise ValueError(f'{code}: {lut_label}')
            speed=min(1.20,max(.85,float(scene.get('playback_speed',1.0) or 1.0)))
            scene_filter_chain,quality_chain=_resolved_color_filters(scene,settings,lut_chain)
            canvas_chains,canvas_source=_canvas_fill_chains(
                media_input,local_index,w,h,
                getattr(settings,'canvas_fill_mode','crop'),
                getattr(settings,'canvas_blur_strength',16),
                getattr(settings,'canvas_fill_color','#101018'),
            )
            chains.extend(canvas_chains)
            base=('hflip,' if global_index in mirror_indices and not is_image else '') + _motion(scene,w,h,fps,max(.05,input_need),is_image)+','+lut_chain+','+scene_filter_chain+','+_effect(str(scene.get('effect','none')),float(scene.get('effect_intensity',.35)))+','+quality_chain+(f',setpts=PTS/{speed:.6f}' if not is_image else '')
            prepared=f'[capcutbase{local_index}]'
            chains.append(f'{canvas_source}{base}{prepared}')
            zoom_chains,zoom_source=_capcut_zoom_chains(
                prepared,local_index,w,h,int(getattr(settings,'capcut_zoom_level',100)),
                str(getattr(settings,'canvas_fill_mode','crop')),
                str(getattr(settings,'canvas_fill_color','#101018')),
                int(getattr(settings,'canvas_blur_strength',16)),
            )
            chains.extend(zoom_chains)
            # Optional source-only animation; captions and overlays remain stable.
            anim_category = str(scene.get('capcut_animation_category',
                                        getattr(settings,'capcut_animation_category','combo')))
            anim_effect = str(scene.get('capcut_animation_effect',
                                      getattr(settings,'capcut_animation_effect','none')))
            anim_target = str(scene.get('capcut_animation_target',
                                      getattr(settings,'capcut_animation_target','both')))
            if not validate_animation(anim_category, anim_effect):
                raise ValueError(f'{code}: invalid CapCut animation selection')
            if anim_target not in ('both','images','videos'):
                raise ValueError(f'{code}: invalid CapCut animation target')
            anim_applies = anim_target == 'both' or (is_image and anim_target == 'images') or (not is_image and anim_target == 'videos')
            if anim_effect != 'none' and anim_applies:
                anim_duration = float(scene.get('capcut_animation_duration',
                                              getattr(settings,'capcut_animation_duration',2.5)))
                anim_intensity = int(scene.get('capcut_animation_intensity',
                                            getattr(settings,'capcut_animation_intensity',65)))
                animated = f'[capcutanim{local_index}]'
                chains.append(f'{zoom_source}{capcut_animation_filter(anim_effect,anim_duration,anim_intensity,w,h,fps)}{animated}')
                zoom_source = animated
            grain = _capcut_grain_filter(
                str(getattr(settings,'capcut_noise_effect','film')),
                int(getattr(settings,'capcut_noise_intensity',35))
            ) if global_index in noise_indices else 'null'
            processed=f'[capcutmedia{local_index}]'
            chains.append(f'{zoom_source}{grain},format=rgba{processed}')
            overlay=resolve_asset(str(scene.get('overlay','none')),'overlays',str(scene.get('media_type','image')),library)
            creative=f'creative{local_index}'
            if overlay:
                overlay_input=media_input+1
                cmd+=['-stream_loop','-1','-i',str(overlay['_file_path'])]
                alpha=max(0,min(1,float(scene.get('overlay_intensity',.35))))
                chains.append(f'[{overlay_input}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,format=rgba,colorchannelmixer=aa={alpha:.3f}[ov{local_index}]')
                chains.append(f'{processed}[ov{local_index}]overlay=shortest=1,scale=in_range=auto:out_range=tv,format=yuv420p[{creative}]')
            else:
                chains.append(f'{processed}scale=in_range=auto:out_range=tv,format=yuv420p[{creative}]')
            trim_start=inbound if local_index==0 else 0.0
            available=(outbound if global_index==end else durations[global_index]+transition_durations[global_index]-trim_start)
            chains.append(f'[{creative}]trim=start={trim_start:.6f}:duration={available:.6f},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,format=yuv420p,setpts=PTS-STARTPTS,fps={fps},settb=AVTB[p{local_index}]')
        current='[p0]'; elapsed=max(0.001,durations[start]-inbound)
        for local_index in range(1,member_count):
            global_index=start+local_index
            boundary_scene=scenes[global_index-1]
            td=transition_durations[global_index-1]
            outlabel=f'[v{local_index}]'
            if _boundary_engine(boundary_scene) == 'xfade':
                chains.append(
                    f'{current}[p{local_index}]{_xfade_filter(boundary_scene,global_index-1,td,elapsed)}{outlabel}'
                )
            else:
                chains.append(f'{current}[p{local_index}]concat=n=2:v=1:a=0{outlabel}')
            current=outlabel
            elapsed+=durations[global_index]
        if outbound>0:
            boundary_scene=scenes[end-1]
            outlabel='[vout]'
            chains.append(
                f'{current}[p{member_count}]{_xfade_filter(boundary_scene,end-1,outbound,elapsed)}{outlabel}'
            )
            current=outlabel
        chains.append(f'{current}trim=duration={expected:.6f},setpts=PTS-STARTPTS[chunk]')
        cmd+=['-filter_complex',';'.join(chains),'-map','[chunk]','-an','-c:v','libx264','-preset',intermediate_preset,'-crf',str(intermediate_crf),'-pix_fmt','yuv420p','-color_range','tv','-r',str(fps),'-fps_mode','cfr',str(partial)]
        base=int(group_index/max(1,len(groups))*82); span=max(1,int(82/max(1,len(groups))))
        _run(cmd,cancel_callback,timeout=0.0,stall_timeout=900.0,progress=progress_callback,progress_base=base,progress_span=span,expected_duration=expected,label=f'Render chunk {group_index+1}/{len(groups)}')
        _validate_final_mp4(partial,ffmpeg,expected_duration=expected,expect_audio=False); partial.replace(target)
        completed_chunks.add(group_index); checkpoint['completed_chunks']=sorted(completed_chunks); checkpoint.setdefault('chunk_fingerprints',{})[str(group_index)]=chunk_fp
        completed_scene_set={int(value) for value in checkpoint.get('completed_scenes',[]) if str(value).isdigit()}; completed_scene_set.update(range(start,end)); checkpoint['completed_scenes']=sorted(completed_scene_set); checkpoint['last_completed_scene']=end
        checkpoint['visual_ready']=False; checkpoint['final_ready']=False
        _checkpoint_write(manifest_path,checkpoint,checkpoint_callback)

    visual=root/'visual.mp4'; visual_expected=sum(durations)
    _render_disk_guard(visual, 'visual chunk assembly')
    if not (checkpoint.get('visual_ready') and _valid_checkpoint_video(visual,ffmpeg,visual_expected)):
        visual.unlink(missing_ok=True); partial=root/'visual.partial.mp4'; partial.unlink(missing_ok=True)
        concat_file=root/'chunks.ffconcat'
        concat_file.write_text(
            'ffconcat version 1.0\n'+''.join(
                "file '"+str(path.resolve()).replace("'", "'\\''")+"'\n"
                for path in timeline_chunks
            ),
            encoding='utf-8',
        )
        cmd=[ffmpeg,'-y','-f','concat','-safe','0','-i',str(concat_file),'-map','0:v:0','-an','-c:v','copy','-movflags','+faststart',str(partial)]
        _run(cmd,cancel_callback,timeout=3600.0,stall_timeout=300.0,progress=progress_callback,progress_base=82,progress_span=3,expected_duration=visual_expected,label='Stream-copy joining validated chunks')
        _validate_final_mp4(partial,ffmpeg,expected_duration=visual_expected,expect_audio=False); partial.replace(visual); checkpoint['visual_ready']=True; checkpoint['assembly_mode']='stream_copy'
        _checkpoint_write(manifest_path,checkpoint,checkpoint_callback)
    elif progress_callback: progress_callback(85,'Reusing completed visual timeline')

    final_expected=audio_duration or visual_expected; expect_audio=audio.is_file()
    if checkpoint.get('final_ready') and out.is_file():
        try:
            _validate_final_mp4(out,ffmpeg,expected_duration=final_expected,expect_audio=expect_audio)
            if progress_callback: progress_callback(100,'Reusing completed final render')
            return str(out)
        except Exception: checkpoint['final_ready']=False

    _render_disk_guard(out, 'final MP4 assembly')
    cmd=[ffmpeg,'-y','-i',str(visual)]; audio_index=None
    if audio.is_file(): audio_index=1; cmd+=['-i',str(audio)]
    # V17.4 audio events: retain the legacy built-in transition SFX path for
    # backward compatibility, then add deterministic permanent-library events
    # resolved before rendering.  Permanent library paths are read-only inputs;
    # nothing is copied into the project solely for playback.
    sfx_entries=[]; elapsed=0.0
    next_audio_input=(2 if audio.is_file() else 1)
    for idx,s in enumerate(scenes[:-1]):
        elapsed+=durations[idx]; item=resolve_asset(str(s.get('transition_sfx','none')),'transition_sfx','video',library)
        if item:
            cmd+=['-i',str(item['_file_path'])]
            sfx_entries.append({'input_idx':next_audio_input,'start':max(0,elapsed-float(s.get('transition_duration_ms',350))/1000),'end':None,'volume':float(s.get('sfx_volume',.35)),'kind':'transition'})
            next_audio_input+=1
    resolved_sfx=meta.get('resolved_sfx_plan') if isinstance(meta.get('resolved_sfx_plan'),dict) else {}
    for event in list(resolved_sfx.get('environment',[]) or [])+list(resolved_sfx.get('transition',[]) or []):
        if not isinstance(event,dict):
            continue
        path=Path(str(event.get('path') or ''))
        if not path.is_file():
            continue
        kind=str(event.get('kind') or 'environment').lower()
        # Environmental ambience is loopable and trimmed to its continuity range.
        # Transition hits remain one-shot inputs.
        if kind=='environment': cmd+=['-stream_loop','-1','-i',str(path)]
        else: cmd+=['-i',str(path)]
        start=max(0.0,float(event.get('start',0.0) or 0.0))
        end=float(event.get('end',0.0) or 0.0)
        sfx_entries.append({'input_idx':next_audio_input,'start':start,'end':end if end>start else None,'volume':float(event.get('volume',0.18) or 0.18),'kind':kind})
        next_audio_input+=1
    # Environmental SFX gets real narration-priority ducking.  The narration is
    # added as a sidechain-only input; it is never mixed a second time.
    environment_event_count=sum(1 for event in sfx_entries if str(event.get('kind') or '')=='environment')
    narration_sidechain_idx=None
    narration_source=Path(str(getattr(settings,'audio_path','') or ''))
    if environment_event_count and narration_source.is_file():
        narration_sidechain_idx=next_audio_input
        cmd+=['-i',str(narration_source)]
        next_audio_input+=1
    ass=root/'captions.ass'; has_captions=bool(getattr(settings,'captions_enabled',False)) and _write_ass([dict(x) for x in meta.get('caption_segments',[]) if isinstance(x,dict)],ass,settings,w,h)
    video_filters=[]
    if has_captions:
        escaped=str(ass).replace('\\','/').replace(':','\\:').replace("'","\\'")
        subtitle_filter=f"subtitles='{escaped}'"
        font_path=Path(str(getattr(settings,'caption_font_path','') or ''))
        if font_path.is_file():
            fonts_dir=str(font_path.parent.resolve()).replace('\\','/').replace(':','\\:').replace("'","\\'")
            subtitle_filter+=f":fontsdir='{fonts_dir}'"
        video_filters.append(subtitle_filter)
    if video_filters: cmd+=['-vf',','.join(video_filters)]
    cmd+=['-map','0:v:0']
    if audio.is_file() or sfx_entries:
        chains=[]; labels=[]
        if audio.is_file(): chains.append(f'[{audio_index}:a]volume=1.0[voice]'); labels.append('[voice]')
        env_sidechain_labels=[]
        if narration_sidechain_idx is not None and environment_event_count:
            speed=max(0.25,min(4.0,float(getattr(settings,'voice_speed_percent',100) or 100)/100.0))
            voice_filters=_atempo_filters_for_speed(speed)+['asetpts=PTS-STARTPTS']
            intro_delay=float(max(0,int(getattr(settings,'intro_duration_seconds',0) or 0))) if bool(getattr(settings,'intro_enabled',False)) else 0.0
            if intro_delay>0:
                ms=int(round(intro_delay*1000)); voice_filters.append(f'adelay={ms}|{ms}')
            chains.append(f'[{narration_sidechain_idx}:a]'+','.join(voice_filters)+'[narr_sc_base]')
            if environment_event_count>1:
                split_labels=''.join(f'[nsc{i}]' for i in range(environment_event_count))
                chains.append(f'[narr_sc_base]asplit={environment_event_count}{split_labels}')
                env_sidechain_labels=[f'[nsc{i}]' for i in range(environment_event_count)]
            else:
                env_sidechain_labels=['[narr_sc_base]']
        env_index=0
        for n,event in enumerate(sfx_entries):
            input_idx=int(event['input_idx']); delay=max(0.0,float(event.get('start',0.0) or 0.0)); vol=max(0.0,min(0.60,float(event.get('volume',0.18) or 0.18)))
            end=event.get('end'); kind=str(event.get('kind') or 'transition')
            filters=[]
            if end is not None and float(end)>delay:
                event_duration=max(0.05,float(end)-delay)
                filters.append(f'atrim=start=0:duration={event_duration:.6f}')
                if kind=='environment':
                    fade=min(0.20,event_duration/4.0)
                    filters.append(f'afade=t=in:st=0:d={fade:.3f}')
                    filters.append(f'afade=t=out:st={max(0.0,event_duration-fade):.6f}:d={fade:.3f}')
            filters.append('asetpts=PTS-STARTPTS')
            ms=int(round(delay*1000)); filters.append(f'adelay={ms}|{ms}')
            filters.append(f'volume={vol:.3f}')
            raw_label=f'[sraw{n}]' if kind=='environment' and env_sidechain_labels else f'[s{n}]'
            chains.append(f'[{input_idx}:a]'+','.join(filters)+raw_label)
            if kind=='environment' and env_sidechain_labels:
                sc=env_sidechain_labels[min(env_index,len(env_sidechain_labels)-1)]
                chains.append(f'{raw_label}{sc}sidechaincompress=threshold=0.025:ratio=10:attack=8:release=260:makeup=1[s{n}]')
                env_index+=1
            labels.append(f'[s{n}]')
        if len(labels)>1: chains.append(''.join(labels)+f'amix=inputs={len(labels)}:duration=longest:dropout_transition=0[aout]'); amap='[aout]'
        else: amap=labels[0]
        cmd+=['-filter_complex',';'.join(chains),'-map',amap,'-c:a','aac','-b:a','192k']
    if audio_duration>0: cmd+=['-t',f'{audio_duration:.6f}']
    if has_captions:
        cmd+=['-c:v','libx264','-preset','veryfast','-crf',str(getattr(settings,'quality_crf',22)),'-pix_fmt','yuv420p','-color_range','tv','-profile:v','high','-r',str(int(getattr(settings,'fps',30) or 30)),'-fps_mode','cfr']
        final_video_mode='caption_encode'
    else:
        cmd+=['-c:v','copy']
        final_video_mode='stream_copy'
    cmd+=['-max_muxing_queue_size','4096','-fflags','+genpts','-avoid_negative_ts','make_zero']
    if audio.is_file() or sfx_entries: cmd+=['-c:a','aac','-ar','48000','-ac','2','-b:a','192k']
    partial=out.with_name(out.stem+'.partial.mp4'); partial.unlink(missing_ok=True); cmd+=['-movflags','+faststart',str(partial)]
    _run(cmd,cancel_callback,timeout=0.0,stall_timeout=900.0 if has_captions else 300.0,progress=progress_callback,progress_base=85,progress_span=14,expected_duration=final_expected,label='Final caption encode' if has_captions else 'Fast final audio mux')
    _validate_final_mp4(partial,ffmpeg,expected_duration=final_expected,expect_audio=(audio.is_file() or bool(sfx_entries))); partial.replace(out)
    checkpoint['final_ready']=True; checkpoint['final_video_mode']=final_video_mode; checkpoint['last_completed_scene']=len(scenes); _checkpoint_write(manifest_path,checkpoint,checkpoint_callback)
    if progress_callback: progress_callback(100,'Windows-compatible MP4 render complete')
    return str(out)

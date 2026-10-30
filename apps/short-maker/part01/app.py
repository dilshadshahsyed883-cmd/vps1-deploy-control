import json
import html
import hashlib
import os
import random
import sqlite3
import re
import shutil
import subprocess
import tempfile
import time
import threading
import fcntl
from contextlib import contextmanager
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PIL import Image, ImageOps, ImageFilter

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass

APP_TITLE = "Short Maker"
DATA_ROOT = Path(os.environ.get("SHORT_MAKER_DATA", "/var/lib/short-maker"))
JOBS_ROOT = DATA_ROOT / "jobs"
JOBS_ROOT.mkdir(parents=True, exist_ok=True)
SHORT_VPS_ROOT = Path(os.environ.get("SHORT_STUDIO_VPS_FOLDER_ROOT", str(DATA_ROOT / "vps_short_folders")))
SHORT_VPS_DEFAULT = "Default"
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
RCLONE = shutil.which("rclone") or "/usr/bin/rclone"
RCLONE_CONFIG = "/home/ubuntu/.config/masterctl/rclone.conf"
DEFAULT_DRIVE_INPUT = "gdrive:Short_Maker/Input"
DEFAULT_DRIVE_OUTPUT = "gdrive:Short_Maker/Outputs"
WIDTH, HEIGHT = 1080, 1920

app = FastAPI(title="Short Maker", version="1.4-multi-drive-audio-input")
executor = ThreadPoolExecutor(max_workers=1)
jobs = {}
jobs_lock = threading.Lock()

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def load_job_states():
    items=[]
    if not JOBS_ROOT.exists():
        return items
    for p in JOBS_ROOT.glob("*/state.json"):
        try:
            state=json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                continue
            state.setdefault("job_id", p.parent.name)
            state.setdefault("created_at", datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat())
            items.append(state)
        except Exception:
            continue
    items.sort(key=lambda x: str(x.get("created_at","")), reverse=True)
    return items


def safe_name(name: str) -> str:
    base = Path(name or "file").name
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .")
    return base[:180] or "file"


def short_vps_folder(name: str) -> Path:
    """Only direct named children of the shared Short Studio root are allowed."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}", name or ""):
        raise ValueError("Use a folder name (1–64 letters, numbers, spaces, _ or -).")
    if SHORT_VPS_ROOT.is_symlink():
        raise ValueError("Shared VPS root cannot be a symlink.")
    path = SHORT_VPS_ROOT / name
    if path.is_symlink():
        raise ValueError("VPS folder symlinks are not allowed.")
    return path


def short_vps_folder_names() -> list[str]:
    SHORT_VPS_ROOT.mkdir(parents=True, exist_ok=True)
    default = short_vps_folder(SHORT_VPS_DEFAULT)
    default.mkdir(exist_ok=True)
    return sorted(
        (p.name for p in SHORT_VPS_ROOT.iterdir()
         if p.is_dir() and not p.is_symlink()
         and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}", p.name)),
        key=str.casefold,
    )


def publish_short_to_vps(path: Path, folder_name: str, job_id: str, strict: bool = False) -> str:
    folder = short_vps_folder(folder_name)
    if not folder.is_dir():
        raise RuntimeError("Chosen VPS folder was deleted. Select an existing folder.")
    final = folder / path.name
    if (final.exists() or final.is_symlink()) and not strict:
        final = folder / f"{path.stem}_{job_id[:8]}{path.suffix}"
    if final.exists() or final.is_symlink():
        raise RuntimeError("Output filename already exists in VPS folder.")
    tmp = folder / f".{uuid.uuid4().hex}.partial"
    try:
        shutil.copyfile(path, tmp)
        if tmp.stat().st_size != path.stat().st_size or not tmp.stat().st_size:
            raise RuntimeError("VPS output copy failed validation.")
        # Atomic no-clobber publish; never overwrite another job/worker.
        os.link(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return final.name


def reserve_short_output_names(folder_name: str, count: int) -> list[str]:
    """Reserve strictly increasing per-folder output numbers across batches.

    SQLite's write transaction serializes submissions; numbering survives
    restarts and Short Uploader deletion, including Drive-only deliveries.
    """
    folder = short_vps_folder(folder_name)
    if not folder.is_dir():
        raise ValueError("Selected VPS folder does not exist.")
    if count < 1 or count > 999:
        raise ValueError("Output count must be 1–999.")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    database = DATA_ROOT / "short_output_sequences.sqlite3"
    if database.is_symlink():
        raise ValueError("Unsafe output naming database.")
    with sqlite3.connect(database, timeout=30) as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("""CREATE TABLE IF NOT EXISTS short_output_sequences (
            folder_name TEXT PRIMARY KEY,
            last_number INTEGER NOT NULL,
            last_tag TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS short_output_batches (
            folder_name TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY(folder_name,tag)
        )""")
        row = con.execute(
            "SELECT last_number,last_tag FROM short_output_sequences WHERE folder_name=?",
            (folder_name,),
        ).fetchone()
        highest, last_tag = (int(row[0]), str(row[1])) if row else (0, "")
        used_tags = set()
        pattern = re.compile(
            rf"^{re.escape(folder_name)}[ _](\d{{3,}})(?:_([a-f0-9]{{6}}))?\.mp4$",
            re.IGNORECASE,
        )
        for entry in folder.iterdir():
            if entry.is_symlink() or not entry.is_file():
                continue
            match = pattern.fullmatch(entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
                if match.group(2):
                    used_tags.add(match.group(2).lower())
        # Persist all batch tags, not just the last one. Deleted/published
        # batches must never free a tag for reuse.
        while True:
            tag = uuid.uuid4().hex[:6]
            if tag == last_tag or tag in used_tags:
                continue
            try:
                con.execute("INSERT INTO short_output_batches(folder_name,tag) VALUES(?,?)",
                            (folder_name, tag))
                break
            except sqlite3.IntegrityError:
                continue
        names = [f"{folder_name} {index:03d}_{tag}.mp4"
                 for index in range(highest + 1, highest + count + 1)]
        con.execute("""INSERT INTO short_output_sequences(folder_name,last_number,last_tag)
                       VALUES(?,?,?)
                       ON CONFLICT(folder_name) DO UPDATE SET
                       last_number=excluded.last_number,last_tag=excluded.last_tag""",
                    (folder_name, highest + count, tag))
    return names


def state_path(job_id: str) -> Path:
    return JOBS_ROOT / job_id / "state.json"


def save_state(job_id: str):
    with jobs_lock:
        state = dict(jobs[job_id])
    p = state_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def set_state(job_id: str, **updates):
    with jobs_lock:
        if job_id not in jobs:
            jobs[job_id] = {"job_id": job_id}
        jobs[job_id].update(updates)
    save_state(job_id)


def get_state(job_id: str):
    with jobs_lock:
        if job_id in jobs:
            return dict(jobs[job_id])
    p = state_path(job_id)
    if p.exists():
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
            with jobs_lock:
                jobs[job_id] = state
            return dict(state)
        except Exception:
            pass
    raise HTTPException(status_code=404, detail="Job not found")


def run_ffmpeg(cmd):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "FFmpeg failed")[-6000:])


def is_image(path: Path) -> bool:
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def render_frame(path: Path, fit_mode: str) -> Image.Image:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        if fit_mode == "fill_crop":
            return ImageOps.fit(img, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
        if fit_mode == "blur":
            bg = ImageOps.fit(img, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
            bg = bg.filter(ImageFilter.GaussianBlur(radius=28))
            fg = ImageOps.contain(img, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
            bg.paste(fg, ((WIDTH - fg.width)//2, (HEIGHT - fg.height)//2))
            return bg
        canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        fg = ImageOps.contain(img, (WIDTH, HEIGHT), method=Image.Resampling.LANCZOS)
        canvas.paste(fg, ((WIDTH - fg.width)//2, (HEIGHT - fg.height)//2))
        return canvas


def motion_filter(motion: str, frames: int, fps: int) -> str:
    frames = max(1, int(frames))
    denom = max(1, frames - 1)
    if motion == "none":
        return f"fps={fps},format=yuv420p"
    if motion == "random":
        motion = random.choice(["zoom_in", "zoom_out", "pan_lr", "pan_rl"])
    if motion == "zoom_in":
        return (
            f"zoompan=z='1+0.15*on/{denom}':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={WIDTH}x{HEIGHT}:fps={fps},format=yuv420p"
        )
    if motion == "zoom_out":
        return (
            f"zoompan=z='1.15-0.15*on/{denom}':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={WIDTH}x{HEIGHT}:fps={fps},format=yuv420p"
        )
    if motion == "pan_lr":
        return (
            f"zoompan=z='1.12':"
            f"x='(iw-iw/zoom)*on/{denom}':y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={WIDTH}x{HEIGHT}:fps={fps},format=yuv420p"
        )
    return (
        f"zoompan=z='1.12':"
        f"x='(iw-iw/zoom)*(1-on/{denom})':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={WIDTH}x{HEIGHT}:fps={fps},format=yuv420p"
    )


def build_timeline(images, total_duration: float, image_duration: float, order: str):
    remaining = total_duration
    timeline = []
    cycle = []
    pos = 0
    while remaining > 1e-6:
        if pos >= len(cycle):
            cycle = list(images)
            if order == "random":
                random.shuffle(cycle)
            pos = 0
        duration = min(image_duration, remaining)
        timeline.append((cycle[pos], duration))
        pos += 1
        remaining -= duration
    return timeline


def build_audio(job_id: str, audio_files, temp: Path):
    if not audio_files:
        return None
    normalized = []
    for idx, audio in enumerate(audio_files, 1):
        set_state(job_id, message=f"Preparing audio {idx}/{len(audio_files)}")
        out = temp / f"audio_{idx:04d}.m4a"
        run_ffmpeg([
            FFMPEG, "-y", "-i", str(audio), "-vn",
            "-ac", "2", "-ar", "48000", "-c:a", "aac", "-b:a", "192k", str(out)
        ])
        normalized.append(out)
    if len(normalized) == 1:
        return normalized[0]
    listing = temp / "audio_concat.txt"
    with listing.open("w", encoding="utf-8") as f:
        for p in normalized:
            escaped = str(p).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    combined = temp / "combined_audio.m4a"
    run_ffmpeg([
        FFMPEG, "-y", "-f", "concat", "-safe", "0",
        "-i", str(listing), "-c", "copy", str(combined)
    ])
    return combined



def normalize_drive_path(value: str) -> str:
    value=(value or "").strip()
    if not value.startswith("gdrive:"):
        raise RuntimeError("Drive path must start with gdrive:")
    if "\n" in value or "\r" in value:
        raise RuntimeError("Invalid Drive path.")
    return value.rstrip("/")

def parse_drive_folder_input(value: str):
    value=(value or "").strip()
    if not value:
        raise RuntimeError("Paste a Google Drive folder link or enter a gdrive: folder path.")
    if "\n" in value or "\r" in value:
        raise RuntimeError("Invalid Drive folder input.")
    if value.startswith("gdrive:"):
        return {"kind":"path","path":normalize_drive_path(value),"raw":value}
    patterns=(
        r"https?://drive\.google\.com/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)",
        r"https?://drive\.google\.com/open\?[^#]*\bid=([A-Za-z0-9_-]+)",
        r"https?://drive\.google\.com/drive/(?:u/\d+/)?[^?#]*[?&]id=([A-Za-z0-9_-]+)",
    )
    for pattern in patterns:
        m=re.search(pattern,value)
        if m:
            return {"kind":"folder_id","folder_id":m.group(1),"raw":value}
    raise RuntimeError("Use a Google Drive folder link (drive.google.com/drive/folders/...) or a gdrive: folder path.")

def parse_drive_folder_inputs(single_path: str = "", paths_json: str = ""):
    """Keep single-folder jobs backward compatible while accepting ordered folder lists."""
    if (paths_json or "").strip():
        try:
            paths = json.loads(paths_json)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Drive folders must be a valid list.") from exc
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise RuntimeError("Drive folders must be a list of links or gdrive: paths.")
    else:
        paths = [single_path]
    paths = [path.strip() for path in paths if path.strip()]
    if not 1 <= len(paths) <= 50:
        raise RuntimeError("Select between 1 and 50 Google Drive folders.")
    seen = set()
    for path in paths:
        source = parse_drive_folder_input(path)
        identity = (source["kind"], source.get("folder_id") or source.get("path"))
        if identity in seen:
            raise RuntimeError("The same Google Drive folder was added more than once.")
        seen.add(identity)
    return paths


def rclone_run(args, timeout=900, attempts=3):
    cmd=[RCLONE,"--config",RCLONE_CONFIG,*args]
    env=os.environ.copy()
    env.update({
        "HOME":"/home/ubuntu",
        "USER":"ubuntu",
        "LOGNAME":"ubuntu",
        "PATH":"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    })
    last_detail=""
    for attempt in range(1,max(1,int(attempts))+1):
        try:
            proc=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout,env=env)
            if proc.returncode == 0:
                return proc.stdout
            last_detail=(proc.stderr or proc.stdout or "").strip()
            if not last_detail:
                last_detail=f"empty stdout/stderr; returncode={proc.returncode}"
            else:
                last_detail=f"returncode={proc.returncode}; {last_detail[-3500:]}"
        except subprocess.TimeoutExpired as exc:
            stderr=exc.stderr.decode(errors="replace") if isinstance(exc.stderr,bytes) else (exc.stderr or "")
            stdout=exc.stdout.decode(errors="replace") if isinstance(exc.stdout,bytes) else (exc.stdout or "")
            last_detail=f"timeout={timeout}s; "+(stderr or stdout or "no subprocess output")[-3500:]
        if attempt < max(1,int(attempts)):
            time.sleep(min(15,3*attempt))
    raise RuntimeError(f"rclone failed after {attempts} attempt(s): {last_detail}")

def import_drive_images(job_id: str, remote_paths, job_dir: Path):
    if isinstance(remote_paths, str):
        remote_paths = [remote_paths]
    remote_paths = parse_drive_folder_inputs(paths_json=json.dumps(remote_paths))
    sources = [parse_drive_folder_input(path) for path in remote_paths]
    stage_root = job_dir / "drive_input"
    image_dir = job_dir / "input" / "images"
    shutil.rmtree(stage_root, ignore_errors=True)
    stage_root.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    # A recovered job may have partial imports; rebuild its drive-owned image list.
    for previous in image_dir.iterdir():
        if previous.is_file():
            previous.unlink()
    set_state(job_id, message="Copying images from Google Drive",
              input_source="drive", drive_input_path=remote_paths[0],
              drive_input_paths=remote_paths, drive_input_kind=sources[0]["kind"])
    common = ["--transfers", "4", "--checkers", "8", "--retries", "3",
              "--low-level-retries", "10"]
    found = []
    seq = 1
    for folder_idx, source in enumerate(sources, 1):
        stage = stage_root / f"folder_{folder_idx:03d}"
        stage.mkdir(parents=True, exist_ok=True)
        set_state(job_id, message=f"Importing Drive folder {folder_idx}/{len(sources)}")
        if source["kind"] == "folder_id":
            args = ["copy", "gdrive:", str(stage),
                    f"--drive-root-folder-id={source['folder_id']}", *common]
        else:
            args = ["copy", source["path"], str(stage), *common]
        rclone_run(args, timeout=900, attempts=3)
        for src in sorted(x for x in stage.rglob("*") if x.is_file()):
            if not is_image(src):
                continue
            target = image_dir / f"{seq:05d}_{safe_name(src.name)}"
            shutil.copy2(src, target)
            found.append(str(target))
            seq += 1
        shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(stage_root, ignore_errors=True)
    if not found:
        raise RuntimeError("Drive input folders contain no readable images.")
    set_state(job_id, message=f"Drive input ready · {len(found)} image(s) from {len(sources)} folder(s)",
              drive_input_count=len(found))
    return found

def local_md5(path: Path) -> str:
    h=hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def verify_drive_file(local_path: Path, remote_path: str):
    local_size=local_path.stat().st_size
    size_data=json.loads(rclone_run(["size",remote_path,"--json"]))
    remote_size=int(size_data.get("bytes",-1))
    if local_size != remote_size:
        raise RuntimeError(f"Drive verification size mismatch for {local_path.name}: local={local_size} remote={remote_size}")
    md5_out=rclone_run(["md5sum",remote_path]).strip()
    remote_md5=(md5_out.split() or [""])[0].lower()
    expected=local_md5(local_path).lower()
    if len(remote_md5)!=32 or remote_md5!=expected:
        raise RuntimeError(f"Drive verification MD5 mismatch for {local_path.name}.")
    return {"bytes":local_size,"md5":expected}

def deliver_outputs(job_id: str, created: list, cfg: dict, job_dir: Path):
    base=normalize_drive_path(cfg.get("drive_output_path") or DEFAULT_DRIVE_OUTPUT)
    stem=safe_name(Path(cfg.get("base_name") or "short_video.mp4").stem)
    remote_dir=f"{base}/{stem}_{job_id[:8]}"
    set_state(job_id,delivery_status="uploading",drive_folder=remote_dir,message="Uploading final output(s) to Google Drive")
    rclone_run(["mkdir",remote_dir],timeout=300,attempts=3)
    delivered=[]
    for idx,item in enumerate(created,1):
        local_path=Path(item["local_path"])
        remote_file=f"{remote_dir}/{item['name']}"
        set_state(job_id,message=f"Drive upload {idx}/{len(created)} · {item['name']}",delivery_status="uploading")
        rclone_run(["copyto",str(local_path),remote_file,"--checksum","--no-traverse","--retries","3","--low-level-retries","10"],timeout=900,attempts=3)
        verified=verify_drive_file(local_path,remote_file)
        row=dict(item)
        row.update({
            "drive_path":remote_file,
            "drive_verified":True,
            "drive_md5":verified["md5"],
            "bytes":verified["bytes"],
        })
        delivered.append(row)
    set_state(job_id,delivery_status="verified",drive_verified=True,drive_folder=remote_dir,message="Drive verified · preparing safe cleanup")
    if cfg.get("verified_clean",True):
        # Only this job's known disposable folders; never touch shared models,
        # application data, credentials or an unrelated user's project.
        cleanup_targets = ("input", "outputs", "drive_input", "temp", "tmp",
                           "cache", "work", "intermediate", "previews")
        for folder in cleanup_targets:
            target = job_dir / folder
            if target.is_symlink():
                raise RuntimeError("Unsafe symlink in job cleanup: " + folder)
            if target.is_dir():
                shutil.rmtree(target)
        for row in delivered:
            row["local_available"]=False
            row["url"]=""
            row.pop("local_path",None)
        set_state(job_id,delivery_status="verified_cleaned",local_cleaned=True,message="Drive verified · local input/output cleaned")
    else:
        for row in delivered:
            row["local_available"]=True
            row.pop("local_path",None)
        set_state(job_id,delivery_status="verified",local_cleaned=False,message="Drive verified · local files retained")
    return delivered


def output_name(base_name: str, index: int, count: int):
    stem = safe_name(Path(base_name).stem or "short")
    return f"{stem}.mp4" if count == 1 else f"{stem}_{index:03d}.mp4"


def render_job(job_id: str, cfg: dict):
    job_dir = JOBS_ROOT / job_id
    out_dir = job_dir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_paths = [Path(p) for p in cfg.get("audio",[])]

    try:
        input_mode=str(cfg.get("input_mode") or "upload")
        if input_mode=="drive":
            image_paths=[Path(p) for p in import_drive_images(job_id,cfg.get("drive_input_paths") or cfg.get("drive_input_path",""),job_dir)]
        else:
            image_paths=[Path(p) for p in cfg.get("images",[]) if is_image(Path(p))]
        if not image_paths:
            raise RuntimeError("No valid images were supplied.")
        total_duration = float(cfg["video_duration"])
        image_duration = float(cfg["image_duration"])
        fps = int(cfg["fps"])
        crf = int(cfg["crf"])
        count = int(cfg["output_count"])
        if total_duration <= 0 or image_duration <= 0:
            raise RuntimeError("Video and image duration must be greater than zero.")

        set_state(job_id, status="running", progress=1, message="Preparing render")
        with tempfile.TemporaryDirectory(prefix="short-maker-", dir=str(job_dir)) as td:
            temp_root = Path(td)
            combined_audio = build_audio(job_id, audio_paths, temp_root)
            created = []

            for out_idx in range(1, count + 1):
                per_out = temp_root / f"out_{out_idx:03d}"
                per_out.mkdir(parents=True, exist_ok=True)
                timeline = build_timeline(image_paths, total_duration, image_duration, cfg["order"])
                clips = []
                total_slots = len(timeline)

                for slot_idx, (img_path, slot_duration) in enumerate(timeline, 1):
                    set_state(
                        job_id,
                        message=f"Video {out_idx}/{count} · image {slot_idx}/{total_slots}",
                        progress=round(5 + 82 * ((out_idx - 1) + slot_idx/total_slots) / count, 1)
                    )
                    source_jpg = per_out / f"source_{slot_idx:05d}.jpg"
                    render_frame(img_path, cfg["fit"]).save(
                        source_jpg, "JPEG", quality=95, subsampling=0
                    )
                    clip = per_out / f"clip_{slot_idx:05d}.mp4"
                    frames = max(1, round(slot_duration * fps))
                    vf = motion_filter(cfg["motion"], frames, fps)
                    run_ffmpeg([
                        FFMPEG, "-y", "-loop", "1", "-i", str(source_jpg),
                        "-vf", vf, "-frames:v", str(frames),
                        "-an", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", str(crf), "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(clip)
                    ])
                    clips.append(clip)

                concat = per_out / "clips.txt"
                with concat.open("w", encoding="utf-8") as f:
                    for p in clips:
                        escaped = str(p).replace("'", "'\\''")
                        f.write(f"file '{escaped}'\n")

                joined = per_out / "joined.mp4"
                run_ffmpeg([
                    FFMPEG, "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-c", "copy", str(joined)
                ])

                reserved_names = cfg.get("output_names") or []
                final_name = reserved_names[out_idx - 1] if len(reserved_names) == count else output_name(cfg["base_name"], out_idx, count)
                final_path = out_dir / final_name
                if combined_audio:
                    set_state(job_id, message=f"Video {out_idx}/{count} · adding audio")
                    run_ffmpeg([
                        FFMPEG, "-y", "-i", str(joined), "-i", str(combined_audio),
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-af", f"apad=whole_dur={total_duration:.6f}",
                        "-t", f"{total_duration:.6f}",
                        "-movflags", "+faststart", str(final_path)
                    ])
                else:
                    run_ffmpeg([
                        FFMPEG, "-y", "-i", str(joined), "-an",
                        "-c:v", "copy", "-t", f"{total_duration:.6f}",
                        "-movflags", "+faststart", str(final_path)
                    ])
                created.append({
                    "name": final_name,
                    "url": f"outputs/{job_id}/{final_name}",
                    "bytes": final_path.stat().st_size,
                    "local_path": str(final_path),
                    "local_available": True,
                    "drive_verified": False,
                })

        folder_name=cfg.get("short_vps_folder") or ""
        if cfg.get("available_to_short_uploader", not cfg.get("upload_to_drive", True)):
            if not folder_name:
                raise RuntimeError("Short Uploader destination requires a VPS folder.")
            for item in created:
                item["short_vps_folder"] = folder_name
                item["short_vps_filename"] = publish_short_to_vps(
                    Path(item["local_path"]), folder_name, job_id,
                    strict=bool(cfg.get("output_names")),
                )
            set_state(job_id, short_vps_folder=folder_name,
                      message=f"Saved {len(created)} video(s) to Short Uploader folder {folder_name}")
        if cfg.get("upload_to_drive", True):
            created=deliver_outputs(job_id,created,cfg,job_dir)
            final_message=f"Completed {len(created)} video(s) · Drive verified"
            if cfg.get("verified_clean",True):
                final_message += " · local cleaned"
        else:
            for row in created:
                row.pop("local_path",None)
            set_state(job_id,delivery_status="short_uploader",drive_verified=False,local_cleaned=False)
            final_message=f"Completed {len(created)} video(s) · available to Short Uploader"

        set_state(
            job_id, status="completed", progress=100,
            message=final_message, outputs=created
        )
    except Exception as exc:
        set_state(job_id, status="failed", message=str(exc), error=str(exc))



@app.on_event("startup")
def recover_pending_jobs():
    pending=[]
    for state in load_job_states():
        if state.get("status") not in ("queued","running"):
            continue
        job_id=str(state.get("job_id") or "")
        job_dir=JOBS_ROOT / job_id
        image_dir=job_dir / "input" / "images"
        audio_dir=job_dir / "input" / "audio"
        cfg=dict(state.get("config") or {})
        required=("video_duration","image_duration","output_count","order","fit","motion","fps","crf","base_name")
        images=sorted(str(x) for x in image_dir.iterdir()) if image_dir.exists() else []
        audio=sorted(str(x) for x in audio_dir.iterdir()) if audio_dir.exists() else []
        has_input=bool(images) or (str(cfg.get("input_mode") or "upload")=="drive" and bool(cfg.get("drive_input_paths") or cfg.get("drive_input_path")))
        if not has_input or any(k not in cfg for k in required):
            with jobs_lock:
                jobs[job_id]=state
            set_state(job_id,status="failed",message="Queue recovery failed: missing input/config.",error="Queue recovery failed: missing input/config.")
            continue
        cfg["images"]=images
        cfg["audio"]=audio
        pending.append((str(state.get("created_at","")),job_id,state,cfg))
    pending.sort(key=lambda x:x[0])
    for _,job_id,state,cfg in pending:
        with jobs_lock:
            jobs[job_id]=state
        set_state(job_id,status="queued",message="Recovered into queue after service restart")
        executor.submit(render_job,job_id,cfg)

HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Short Maker</title>
<style>
:root{--bg:#f5f7fb;--panel:#fff;--text:#152033;--muted:#738098;--accent:#5b5ce2;--line:#e7eaf1;--good:#16a36a;--bad:#dc4c64}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font:14px/1.5 Inter,Segoe UI,Arial,sans-serif;color:var(--text)}
.sidebar{position:fixed;inset:0 auto 0 0;width:230px;background:#101526;color:#fff;padding:24px 18px;display:flex;flex-direction:column}
.brand{display:flex;gap:12px;align-items:center;margin-bottom:30px}.logo{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,#6c63ff,#8e57ed);display:grid;place-items:center;font-weight:900}
.brand strong,.brand small{display:block}.brand small{color:#8790a8}.sidebar nav{display:grid;gap:8px}.sidebar nav a{color:#aeb6ca;text-decoration:none;padding:11px 14px;border-radius:10px;font-weight:700}
.sidebar nav a.active,.sidebar nav a:hover{background:#262c42;color:#fff}.server{margin-top:auto;color:#aeb6ca;font-size:12px}.server span{display:inline-block;width:8px;height:8px;background:#2bc77b;border-radius:50%;margin-right:7px}
main{margin-left:230px;padding:28px 34px 50px;max-width:1500px}header{margin-bottom:20px}h1{margin:0;font-size:28px}header p{margin:5px 0;color:var(--muted)}
.grid{display:grid;grid-template-columns:1.1fr .9fr;gap:18px}.panel{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 5px 18px #2732530a;margin-bottom:18px}
.panel h2{font-size:18px;margin:0 0 4px}.hint{color:var(--muted);font-size:12px;margin-bottom:14px}.field{margin-top:13px}.field label{display:block;font-weight:800;margin-bottom:6px}
input,select{width:100%;border:1px solid #dfe3ec;border-radius:10px;padding:10px 12px;background:#fff;font:inherit}.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.filebox{border:1px dashed #cbd3e2;border-radius:12px;padding:16px;background:#fafbfe}.filebox input{border:0;padding:0}.count{font-weight:800;color:var(--accent);margin-top:8px}
button{border:0;border-radius:10px;padding:11px 15px;background:var(--accent);color:#fff;font-weight:800;cursor:pointer}button:disabled{opacity:.55;cursor:not-allowed}
.render{width:100%;font-size:15px;padding:14px}.progress{height:10px;background:#edf0f5;border-radius:20px;overflow:hidden;margin-top:14px}.progress span{display:block;height:100%;width:0;background:var(--accent)}
.status{margin-top:9px;color:var(--muted)}.outputs{display:grid;gap:8px;margin-top:12px}.outputs a{display:flex;justify-content:space-between;padding:11px 12px;background:#f0efff;color:#4748c9;text-decoration:none;border-radius:9px;font-weight:800}
.default{display:inline-block;padding:4px 8px;border-radius:20px;background:#efefff;color:#5354d9;font-size:11px;font-weight:900}.note{padding:10px 12px;background:#f8f9fc;border-radius:9px;color:var(--muted);margin-top:12px}
@media(max-width:950px){.sidebar{width:76px;padding:20px 10px}.brand>div:last-child,.sidebar nav a{font-size:0}.sidebar nav a:before{content:"•";font-size:22px}main{margin-left:76px;padding:20px}.grid{grid-template-columns:1fr}}
@media(max-width:600px){.sidebar{display:none}main{margin-left:0;padding:14px}.two{grid-template-columns:1fr}}

.sm-tabs{display:flex;gap:7px;padding:6px;background:#e9eef7;border:1px solid #dce4ef;border-radius:13px;margin-bottom:18px;max-width:430px}
.sm-tabs button{flex:1;background:transparent;color:#596579}.sm-tabs button.active{background:#fff;color:#2457d6;box-shadow:0 3px 10px rgba(31,45,74,.11)}
.queue-head{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:14px}.queue-head h2{margin:0}.queue-actions{display:flex;gap:8px;flex-wrap:wrap}
button.secondary{background:#f0efff;color:#4d4fc7}button.danger{background:#fff0f2;color:#dc4c64}
.queue-metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px}.queue-metrics>div{border:1px solid var(--line);border-radius:12px;padding:14px}.queue-metrics b{display:block;font-size:24px}.queue-metrics span{color:var(--muted);font-weight:700}
.queue-table-wrap{overflow:auto}.queue-table{width:100%;border-collapse:collapse;min-width:900px}.queue-table th,.queue-table td{text-align:left;padding:11px;border-bottom:1px solid var(--line);vertical-align:middle}.queue-table th{font-size:12px;color:var(--muted)}
.qbadge{display:inline-block;padding:5px 8px;border-radius:20px;font-size:11px;font-weight:900;background:#eef0f5}.qbadge.uploading{background:#e7f1ff;color:#2463b0}.qbadge.queued{background:#efefff;color:#5354d9}.qbadge.running{background:#fff3de;color:#a86400}.qbadge.completed{background:#e7f8f0;color:#11734c}.qbadge.failed{background:#ffecef;color:#b9344c}
.qbar{width:140px;height:7px;background:#edf0f5;border-radius:20px;overflow:hidden}.qbar span{display:block;height:100%;background:var(--accent)}.qmsg{max-width:260px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted);font-size:12px}
.queue-pager{display:flex;justify-content:flex-end;align-items:center;gap:9px;margin-top:13px}.queue-pager span{color:var(--muted);font-weight:700}
.drive-delivery{margin:14px 0;padding:14px;border:1px solid var(--line);border-radius:12px;background:#fafbfe}.checkline{display:block;margin:7px 0;color:var(--text)}.checkline input{width:auto;margin-right:6px}.driveok{color:var(--good);font-weight:800}.drivewait{color:var(--muted);font-size:12px}
@media(max-width:700px){.queue-head{align-items:flex-start;flex-direction:column}.queue-metrics{grid-template-columns:1fr 1fr}}


.drive-link-row{display:flex;gap:8px;align-items:center;margin:8px 0}
.drive-link-row input{flex:1;min-width:0;border:1px solid #dfe3ec;padding:10px 12px;background:#fff;border-radius:10px}
.drive-link-row button{flex:0 0 auto;white-space:nowrap}
.source-action{margin-top:9px;background:#eeeefe;color:#4945bc}
.song-item{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid #e7eaf1;overflow-wrap:anywhere}
.song-item span{min-width:0;flex:1}
.song-item button{padding:7px 10px;flex-shrink:0}
@media(max-width:600px){.drive-link-row{align-items:stretch}.drive-link-row button{padding:8px}}

/* VIDEO_STUDIO_UI_UNIFIED_V1 */
main{max-width:none!important;padding:26px 34px 42px!important}
main>[data-video-studio-subnav="1"],main>header,main>.sm-tabs,main>form,main>#queuePanel{width:100%!important;max-width:1400px!important;margin-left:auto!important;margin-right:auto!important;box-sizing:border-box!important}
main>header{margin-bottom:18px!important}.brand{margin-bottom:34px!important}.sidebar nav a{padding:12px 14px!important;font-weight:600!important}
.sm-tabs{display:flex!important;gap:5px!important;padding:0!important;background:transparent!important;border:0!important;border-radius:0!important;max-width:none!important;margin-bottom:18px!important}
.sm-tabs button{flex:0 0 auto!important;background:transparent!important;color:#667085!important;padding:9px 12px!important;border:0!important;border-radius:9px!important;box-shadow:none!important;font-weight:700!important}
.sm-tabs button:hover{background:#eef0fb!important;color:#3f43a9!important}.sm-tabs button.active{background:#ececff!important;color:#4e46c7!important;box-shadow:none!important}
.panel{border-radius:16px!important;border-color:#e7eaf1!important;box-shadow:0 5px 18px #2732530a!important}
@media(max-width:950px){main{margin-left:76px!important;padding:20px!important}}
@media(max-width:600px){main{margin-left:0!important;padding:14px!important}.sm-tabs{overflow-x:auto!important;white-space:nowrap!important}}

</style>
<link rel="stylesheet" href="/&#115;tatic/vps1_native_site_shell.css?v=20261020-experiment-v1&layout=video-v2&song=20261029">
<link rel="stylesheet" href="/&#115;tatic/vps1_video_studio_geometry.css?v=20261020-geometry-v1&layout=video-v2">
<script defer src="/&#115;tatic/vps1_native_site_shell.js?v=20261020-experiment-v1&layout=video-v2&song=20261029" data-unify-sidebar="1"></script>
</head>
<body>
<button type="button" class="vps1-video-mobile-menu" aria-controls="app-sidebar" aria-expanded="false">☰ Menu</button>
<div class="vps1-video-mobile-overlay" hidden></div>
<aside id="app-sidebar" class="sidebar">
 <div class="brand"><div class="logo">S</div><div><strong>Scene Builder</strong><small>Team Studio</small></div></div>
 <nav>
  <a href="/">Dashboard</a>
  <a href="/song-studio/">Song Studio</a>
  <a class="active" href="/short-maker" aria-current="page">Short Studio</a>
  <a href="/experiment-studio/">Experiment Studio</a>

<!-- Auto Script stays OFFLINE -->
 </nav>
 <div class="server"><span></span> VPS Connected</div>
</aside>
<main>
<nav data-video-studio-subnav="1" class="vps1-native-video-tabs vps1-native-short-tabs" aria-label="Short Studio"><a class="active" aria-current="page" href="/short-maker">Short Maker</a><a href="/short-uploader">Short Uploader</a></nav>

<header><h1>Short Maker</h1><p>Every render becomes a queue job. Keep adding new jobs while one job renders.</p></header>
<div class="sm-tabs" role="tablist" aria-label="Short Maker">
 <button id="makerTabBtn" class="active" type="button" role="tab" aria-controls="makerPanel" aria-selected="true">Maker</button>
 <button id="queueTabBtn" type="button" role="tab" aria-controls="queuePanel" aria-selected="false">Queue <span id="queueTotal"></span></button>
 <button id="settingsTabBtn" type="button" role="tab" aria-controls="settingsPanel" aria-selected="false">Settings</button>
</div>
<form id="form">
<section id="makerPanel">
<section class="panel" aria-label="Short Maker output folder">
 <h2>VPS Output Folder</h2>
 <div class="two">
  <div class="field"><label for="shortVpsFolder">Folder</label><select id="shortVpsFolder" name="short_vps_folder"><option value="Default">Default</option></select></div>
  <div class="field"><label>Manage folders</label><div class="queue-actions"><button type="button" class="secondary" id="createShortFolder">+ Create</button><button type="button" class="danger" id="deleteShortFolder">Delete empty folder</button></div></div>
 </div>
 <div class="hint" id="shortFolderMessage">Automatic names: Folder 001_abcdef.mp4, Folder 002_abcdef.mp4. Every batch uses a different tag.</div>
</section>
<div class="grid">
 <div>
  <section class="panel">
   <h2>Images</h2><div class="hint">Use a PC folder or import an image folder directly from Google Drive.</div>
   <div class="field"><label>Image input source</label><select id="inputMode" name="input_mode"><option value="upload" selected>PC / Browser Upload</option><option value="drive">Google Drive Folder</option></select></div>
   <div id="localImageBox" class="filebox" style="margin-top:10px">
    <input id="images" name="images" type="file" multiple webkitdirectory directory accept="image/*,.jfif,.heic,.heif">
    <div id="imageCount" class="count">0 images selected</div>
   </div>
   <div id="driveImageBox" class="filebox" style="margin-top:10px" hidden>
    <label for="driveInputPath"><b>Google Drive image folders / links</b></label>
    <div id="driveInputRows">
     <div class="drive-link-row"><input id="driveInputPath" class="drive-path-input" type="text" placeholder="Drive folder link or gdrive:Short_Maker/Input" aria-label="Google Drive folder link 1"><button class="danger remove-drive" type="button" disabled>Remove</button></div>
    </div>
    <button id="addDriveLink" type="button" class="source-action">+ Add another Drive link</button>
    <div id="driveLinkCount" class="count">0 Drive folders entered</div>
    <div class="hint" style="margin:8px 0 0">Add multiple Google Drive folder links or gdrive: paths. Images from all folders are combined into this job.</div>
   </div>
  </section>
  <section class="panel">
   <h2>Audio <span class="default">Optional</span></h2><div class="hint">Select one or more songs/voice files. They play sequentially. Audio is trimmed or padded to the video duration.</div>
   <div class="filebox">
    <input id="audio" name="audio" type="file" multiple accept="audio/*,.mp3,.wav,.m4a,.aac,.flac,.ogg,.opus,.wma,.aiff,.aif">
    <button id="addAudioBtn" type="button" class="source-action">+ Add more songs</button>
    <div id="audioCount" class="count">No audio selected</div>
    <div id="songList" aria-live="polite"></div>
   </div>
  </section>
 </div>
 <div>
  <section class="panel">
   <h2>Timing & Batch</h2>
   <div class="two">
    <div class="field"><label>Video duration (sec)</label><input name="video_duration" type="number" min=".1" step=".1" value="16"></div>
    <div class="field"><label>Image duration (sec)</label><input name="image_duration" type="number" min=".1" step=".1" value="3"></div>
   </div>
   <div class="field"><label>Number of output videos</label><input name="output_count" type="number" min="1" max="999" value="1"></div>
   <div class="note" id="slotNote">16 sec ÷ 3 sec/image ≈ 5.33 image slots per video.</div>
  </section>
  <section class="panel">
   <h2>Visual Settings</h2>
   <div class="field"><label>Image order</label><select name="order"><option value="random" selected>Random</option><option value="filename">File name</option></select></div>
   <div class="field"><label>9:16 fit mode</label><select name="fit"><option value="fill_crop" selected>Fill / crop</option><option value="black">Fit / black bars</option><option value="blur">Fit / blurred background</option></select></div>
   <div class="field"><label>Image motion</label><select name="motion"><option value="random" selected>Random Ken Burns</option><option value="zoom_in">Zoom in</option><option value="zoom_out">Zoom out</option><option value="pan_lr">Pan left → right</option><option value="pan_rl">Pan right → left</option><option value="none">None</option></select></div>
   <div class="two">
    <div class="field"><label>FPS</label><select name="fps"><option>24</option><option selected>30</option><option>50</option><option>60</option></select></div>
    <div class="field"><label>Quality (CRF)</label><input name="crf" type="number" min="16" max="28" value="20"></div>
   </div>
  </section>
 </div>
</div>
<section class="panel" aria-label="Render controls">
 <h2>Render</h2>
 <button id="render" class="render" type="submit">Start Rendering</button>
 <div class="progress" id="progressTrack" role="progressbar" aria-label="Upload progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span id="bar"></span></div>
 <div id="progressDetail" class="status" aria-live="polite">Upload: 0% · Rendering: 0%</div>
 <div id="status" class="status">Ready.</div>
 <div id="outputs" class="outputs"></div>
</section>
</section>
<section id="settingsPanel" role="tabpanel" aria-labelledby="settingsTabBtn" hidden>
<section class="panel">
 <h2>Output</h2>
 <input type="hidden" name="base_name" value="short_video.mp4">
 <div class="field"><label>Aspect ratio / resolution</label><input value="9:16 · 1080 × 1920 (vertical)" readonly aria-label="Output aspect ratio and resolution"></div>
 <div class="drive-delivery">
  <h3>Output destination</h3>
  <div class="hint">Choose one destination. Short Uploader is selected by default.</div>
  <label class="checkline"><input id="availableToUploader" name="available_to_short_uploader" type="checkbox" checked> <b>Available to Short Uploader</b></label>
  <label class="checkline"><input id="uploadToDrive" name="upload_to_drive" type="checkbox"> <b>Upload final output(s) to Google Drive</b></label>
  <div id="driveDeliveryOptions" hidden>
   <div class="field"><label>Drive output folder</label><input id="driveOutputPath" name="drive_output_path" value="gdrive:Short_Maker/Outputs"></div>
   <label class="checkline"><input id="verifiedClean" name="verified_clean" type="checkbox" checked disabled> <b>Drive verified clean</b> — delete this job's local inputs/outputs only after every final MP4 matches Drive size + MD5.</label>
  </div>
 </div>
</section>
</section>
</form>
<section id="queuePanel" hidden>
<section class="panel">
  <div class="queue-head"><div><h2>Render Queue</h2><div class="hint">One render runs at a time. New jobs wait automatically.</div></div>
  <div class="queue-actions"><button id="newJobBtn" type="button">+ New Job</button><button id="clearDoneBtn" class="secondary" type="button">Clear completed</button><button id="clearFailedBtn" class="danger" type="button">Clear failed</button></div></div>
  <div class="queue-metrics">
    <div><b id="mUploading">0</b><span>Uploading</span></div>
    <div><b id="mQueued">0</b><span>Queued</span></div><div><b id="mRunning">0</b><span>Running</span></div><div><b id="mCompleted">0</b><span>Completed</span></div><div><b id="mFailed">0</b><span>Failed</span></div>
  </div>
  <div class="queue-table-wrap"><table class="queue-table"><thead><tr><th>Job</th><th>Status</th><th>Progress</th><th>Message</th><th>Outputs</th><th>Action</th></tr></thead><tbody id="queueRows"><tr><td colspan="6">Loading…</td></tr></tbody></table></div>
  <div class="queue-pager"><button id="prevPage" class="secondary" type="button">Previous</button><span id="pageInfo">Page 1</span><button id="nextPage" class="secondary" type="button">Next</button></div>
</section>
</section>
</main>
<script>
const form=document.getElementById('form'), images=document.getElementById('images'), audio=document.getElementById('audio'),inputMode=document.getElementById('inputMode'),localImageBox=document.getElementById('localImageBox'),driveImageBox=document.getElementById('driveImageBox');
const imageCount=document.getElementById('imageCount'),audioCount=document.getElementById('audioCount'),statusEl=document.getElementById('status'),bar=document.getElementById('bar'),outputs=document.getElementById('outputs'),render=document.getElementById('render');
const driveInputRows=document.getElementById('driveInputRows'),driveLinkCount=document.getElementById('driveLinkCount'),songList=document.getElementById('songList');
const selectedAudioFiles=[];
const shortFolderSelect=document.getElementById('shortVpsFolder');
const shortFolderMessage=document.getElementById('shortFolderMessage');
const uploaderDestination=document.getElementById('availableToUploader');
const driveDestination=document.getElementById('uploadToDrive');
const driveDeliveryOptions=document.getElementById('driveDeliveryOptions');
function selectOutputDestination(destination){
 uploaderDestination.checked=destination==='uploader';
 driveDestination.checked=destination==='drive';
 driveDeliveryOptions.hidden=destination!=='drive';
}
uploaderDestination.addEventListener('change',()=>selectOutputDestination('uploader'));
driveDestination.addEventListener('change',()=>selectOutputDestination('drive'));
selectOutputDestination('uploader');
async function loadShortFolders(preferred){
 const r=await fetch('api/vps-folders',{cache:'no-store'});
 const data=await r.json();if(!r.ok)throw new Error(data.detail||'Could not list VPS folders');
 const chosen=preferred||shortFolderSelect.value||'Default';
 shortFolderSelect.replaceChildren();
 (data.folders||[]).forEach(name=>{const opt=document.createElement('option');opt.value=name;opt.textContent=name;shortFolderSelect.appendChild(opt);});
 if((data.folders||[]).includes(chosen))shortFolderSelect.value=chosen;
}
document.getElementById('createShortFolder').onclick=async()=>{
 const name=prompt('New VPS folder name (letters, digits, spaces, _ or -):');
 if(name===null)return;
 try{
 const r=await fetch('api/vps-folders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim()})});
 const data=await r.json();if(!r.ok)throw new Error(data.detail||'Create failed');
 await loadShortFolders(name.trim());shortFolderMessage.textContent='Folder created: '+name.trim();
 }catch(e){shortFolderMessage.textContent='Folder error: '+e.message;}
};
document.getElementById('deleteShortFolder').onclick=async()=>{
 const name=shortFolderSelect.value;
 if(!name||!confirm('Delete EMPTY VPS folder '+name+'? Videos cannot be deleted here.'))return;
 try{
 const r=await fetch('api/vps-folders/'+encodeURIComponent(name),{method:'DELETE'});
 const data=await r.json();if(!r.ok)throw new Error(data.detail||'Delete failed');
 await loadShortFolders('Default');shortFolderMessage.textContent='Deleted empty folder '+name;
 }catch(e){shortFolderMessage.textContent='Folder error: '+e.message;}
};
loadShortFolders().catch(e=>{shortFolderMessage.textContent='Folder error: '+e.message;});

images.addEventListener('change',()=>imageCount.textContent=images.files.length+' image(s) selected');
function syncInputMode(){const d=inputMode.value==='drive';localImageBox.hidden=d;driveImageBox.hidden=!d;}
inputMode.addEventListener('change',syncInputMode);syncInputMode();
function drivePaths(){return Array.from(driveInputRows.querySelectorAll('.drive-path-input')).map(input=>input.value.trim()).filter(Boolean);}
function syncDriveRows(){
 const rows=Array.from(driveInputRows.querySelectorAll('.drive-link-row'));
 rows.forEach((row,index)=>{
  row.querySelector('.remove-drive').disabled=rows.length===1;
  row.querySelector('input').setAttribute('aria-label','Google Drive folder link '+(index+1));
 });
 const total=drivePaths().length;
 driveLinkCount.textContent=total+' Drive folder(s) entered';
}
driveInputRows.addEventListener('input',syncDriveRows);
driveInputRows.addEventListener('click',event=>{
 if(!event.target.classList.contains('remove-drive'))return;
 event.target.closest('.drive-link-row').remove();syncDriveRows();
});
document.getElementById('addDriveLink').addEventListener('click',()=>{
 const row=driveInputRows.firstElementChild.cloneNode(true);
 row.querySelector('input').value='';
 row.querySelector('input').removeAttribute('id');
 driveInputRows.appendChild(row);syncDriveRows();row.querySelector('input').focus();
});
syncDriveRows();
function renderSelectedAudio(){
 audioCount.textContent=selectedAudioFiles.length?selectedAudioFiles.length+' song(s) selected':'No audio selected';
 songList.replaceChildren();
 selectedAudioFiles.forEach((file,index)=>{
  const row=document.createElement('div');row.className='song-item';
  const label=document.createElement('span');label.textContent=(index+1)+'. '+file.name;
  const remove=document.createElement('button');remove.type='button';remove.className='danger';remove.textContent='Remove';
  remove.addEventListener('click',()=>{selectedAudioFiles.splice(index,1);renderSelectedAudio();});
  row.append(label,remove);songList.appendChild(row);
 });
}
audio.addEventListener('change',()=>{
 Array.from(audio.files).forEach(file=>{
  if(!selectedAudioFiles.some(existing=>existing.name===file.name&&existing.size===file.size&&existing.lastModified===file.lastModified)){
   selectedAudioFiles.push(file);
  }
 });
 audio.value='';renderSelectedAudio();
});
document.getElementById('addAudioBtn').addEventListener('click',()=>audio.click());

function slots(){const v=Number(form.video_duration.value||0),i=Number(form.image_duration.value||0);document.getElementById('slotNote').textContent=(v>0&&i>0)?`${v} sec ÷ ${i} sec/image ≈ ${(v/i).toFixed(2)} image slots per video.`:'Enter valid durations.';}
form.video_duration.addEventListener('input',slots);form.image_duration.addEventListener('input',slots);
async function poll(id){
 try{
  const r=await fetch(`api/jobs/${id}`); const s=await r.json();
  bar.style.width=`${Number(s.progress||0)}%`; statusEl.textContent=s.message||s.status||'Working...';
  if(s.status==='completed'){
   render.disabled=false; outputs.innerHTML='';
   (s.outputs||[]).filter(o=>o.local_available&&o.url).forEach(o=>{const a=document.createElement('a');a.href=o.url;a.innerHTML=`<span>${o.name}</span><span>Download</span>`;outputs.appendChild(a);});
   return;
  }
  if(s.status==='failed'){render.disabled=false;statusEl.textContent=`Failed: ${s.error||s.message||'Unknown error'}`;return;}
  setTimeout(()=>poll(id),1500);
 }catch(e){statusEl.textContent='Status check failed; retrying...';setTimeout(()=>poll(id),2500);}
}

const makerPanel=document.getElementById('makerPanel'),queuePanel=document.getElementById('queuePanel'),settingsPanel=document.getElementById('settingsPanel');
const makerTabBtn=document.getElementById('makerTabBtn'),queueTabBtn=document.getElementById('queueTabBtn'),settingsTabBtn=document.getElementById('settingsTabBtn');
let queueItems=[],queuePage=1;const queuePageSize=20;
function showShortTab(name){
 const panels={maker:makerPanel,queue:queuePanel,settings:settingsPanel};
 const buttons={maker:makerTabBtn,queue:queueTabBtn,settings:settingsTabBtn};
 for(const key of Object.keys(panels)){
  const active=key===name;panels[key].hidden=!active;
  buttons[key].classList.toggle('active',active);
  buttons[key].setAttribute('aria-selected',String(active));
 }
 if(name==='queue')loadQueue();
}
makerTabBtn.onclick=()=>showShortTab('maker');
queueTabBtn.onclick=()=>showShortTab('queue');
settingsTabBtn.onclick=()=>showShortTab('settings');
document.getElementById('newJobBtn').onclick=()=>showShortTab('maker');
function escQ(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function renderQueue(){
 const pages=Math.max(1,Math.ceil(queueItems.length/queuePageSize));if(queuePage>pages)queuePage=pages;
 const rows=queueItems.slice((queuePage-1)*queuePageSize,queuePage*queuePageSize);const body=document.getElementById('queueRows');
 document.getElementById('pageInfo').textContent=`Page ${queuePage} of ${pages}`;document.getElementById('prevPage').disabled=queuePage<=1;document.getElementById('nextPage').disabled=queuePage>=pages;
 if(!rows.length){body.innerHTML='<tr><td colspan="6">No jobs yet.</td></tr>';return;}
 body.innerHTML=rows.map(j=>{const st=String(j.status||'unknown'),p=Math.max(0,Math.min(100,Number(j.progress||0))),outs=(j.outputs||[]).map(o=>o.local_available&&o.url?`<a href="${escQ(o.url)}">${escQ(o.name)}</a>`:`<span class="driveok" title="${escQ(o.drive_path||'')}">Drive ✓ ${escQ(o.name)}</span>`).join(' · '),terminal=st==='completed'||st==='failed';return `<tr><td><b>${escQ((j.job_id||'').slice(0,8))}</b></td><td><span class="qbadge ${escQ(st)}">${escQ(st)}</span></td><td><div class="qbar"><span style="width:${p}%"></span></div><small>${p.toFixed(0)}%</small></td><td><div class="qmsg" title="${escQ(j.message||'')}">${escQ(j.message||'')}</div><div class="drivewait">${escQ(j.delivery_status||'')}</div></td><td>${outs||'—'}</td><td>${terminal?`<button class="danger" type="button" onclick="deleteQueueJob('${escQ(j.job_id)}')">Delete</button>`:'—'}</td></tr>`;}).join('');
}
async function loadQueue(){try{const r=await fetch('api/jobs',{cache:'no-store'}),d=await r.json();queueItems=d.jobs||[];const c=d.counts||{};mUploading.textContent=c.uploading||0;mQueued.textContent=c.queued||0;mRunning.textContent=c.running||0;mCompleted.textContent=c.completed||0;mFailed.textContent=c.failed||0;queueTotal.textContent=`(${queueItems.length})`;renderQueue();}catch(e){}}
async function deleteQueueJob(id){if(!confirm('Delete this completed/failed job and its local files?'))return;const r=await fetch(`api/jobs/${id}`,{method:'DELETE'});if(!r.ok){const d=await r.json();alert(d.detail||'Delete failed');}loadQueue();}
async function clearQueueStatus(st){if(!confirm(`Clear all ${st} jobs?`))return;await fetch(`api/jobs/clear/${st}`,{method:'POST'});queuePage=1;loadQueue();}
document.getElementById('clearDoneBtn').onclick=()=>clearQueueStatus('completed');document.getElementById('clearFailedBtn').onclick=()=>clearQueueStatus('failed');
document.getElementById('prevPage').onclick=()=>{if(queuePage>1){queuePage--;renderQueue();}};document.getElementById('nextPage').onclick=()=>{const pages=Math.max(1,Math.ceil(queueItems.length/queuePageSize));if(queuePage<pages){queuePage++;renderQueue();}};
loadQueue();setInterval(loadQueue,2000);

// The refresh-safe uploader is loaded from resumable_client.js.


</script>
<script src="resumable_client.js"></script>
<script>
(() => {
  const toggle=document.querySelector(".vps1-video-mobile-menu");
  const sidebar=document.getElementById("app-sidebar");
  const backdrop=document.querySelector(".vps1-video-mobile-overlay");
  if(!toggle||!sidebar||!backdrop)return;
  const close=()=>{sidebar.classList.remove("mobile-open");backdrop.hidden=true;toggle.setAttribute("aria-expanded","false");};
  toggle.addEventListener("click",()=>{
    const opened=!sidebar.classList.contains("mobile-open");
    sidebar.classList.toggle("mobile-open",opened);
    backdrop.hidden=!opened;
    toggle.setAttribute("aria-expanded",String(opened));
  });
  backdrop.addEventListener("click",close);
  sidebar.querySelectorAll("nav a").forEach(a=>a.addEventListener("click",close));
  document.addEventListener("keydown",e=>{if(e.key==="Escape")close();});
  window.addEventListener("pageshow",close);
})();
</script>
</body></html>'''


@app.get("/health")
def health():
    return {"ok": True, "app": "short-maker", "ffmpeg": FFMPEG, "data_root": str(DATA_ROOT)}


def render_verified_sidebar(username: str, is_admin: bool) -> str:
    """Server-side fallback when shared browser JS fails to hydrate the shell.

    Only Scene SSO's reverse-proxy supplied identity may set this visible role.
    Admin pages independently enforce their own server-side authorization.
    """
    name = (username or "").strip()[:120]
    if name:
        safe_name = html.escape(name, quote=True)
        footer = (
            '<div class="sidebar-account" data-vps1-server-identity="1">'
            '<div class="sidebar-account-name">' + safe_name + '</div>'
            '<form action="/logout" method="post">'
            '<button type="submit">Log out</button></form></div>'
        )
    else:
        footer = (
            '<div class="sidebar-account" data-vps1-server-identity="0">'
            '<div class="sidebar-account-name">Account verification needed</div>'
            '<a class="vps1-session-login" href="/login">Sign in</a></div>'
        )
    marker = '<div class="server"><span></span> VPS Connected</div>'
    if HTML.count(marker) != 1:
        raise RuntimeError("Short Maker sidebar template anchor changed")
    page = HTML.replace(marker, footer + "\n " + marker, 1)
    if name and is_admin:
        anchor = "<!-- Auto Script stays OFFLINE -->"
        if page.count(anchor) != 1:
            raise RuntimeError("Short Maker Admin placement anchor changed")
        page = page.replace(
            anchor,
            '<a class="vps1-server-admin" href="/admin">Admin</a>\n' + anchor,
            1,
        )
    return page


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    # Scene SSO Nginx overwrites these headers after session authentication.
    # Never infer administrator status from cookies, localStorage or JS flags.
    username = request.headers.get("x-auth-user", "")
    is_admin = bool(username.strip()) and request.headers.get("x-is-admin") == "1"
    return HTMLResponse(
        render_verified_sidebar(username, is_admin),
        headers={"Cache-Control": "private, no-store", "Vary": "Cookie"},
    )


@app.get("/api/vps-folders")
def list_short_vps_folders():
    return {"folders": short_vps_folder_names()}


@app.post("/api/vps-folders")
async def create_short_vps_folder(request: Request):
    payload = await request.json()
    try:
        name = str(payload.get("name") or "").strip()
        folder = short_vps_folder(name)
        SHORT_VPS_ROOT.mkdir(parents=True, exist_ok=True)
        folder.mkdir(exist_ok=False)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Folder already exists.")
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "name": name}


@app.delete("/api/vps-folders/{folder_name}")
def delete_short_vps_folder(folder_name: str):
    try:
        if folder_name == SHORT_VPS_DEFAULT:
            raise ValueError("Default folder is protected.")
        folder = short_vps_folder(folder_name)
        if not folder.is_dir():
            raise HTTPException(status_code=404, detail="Folder not found.")
        folder.rmdir()  # Fails safely if any video, hidden file or worker temp exists.
    except OSError:
        raise HTTPException(status_code=409, detail="Folder is not empty or is in use.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "name": folder_name}


@app.post("/api/jobs")
async def create_job(
    images: List[UploadFile] = File(default=[]),
    audio: List[UploadFile] = File(default=[]),
    video_duration: float = Form(16.0),
    image_duration: float = Form(3.0),
    output_count: int = Form(1),
    order: str = Form("random"),
    fit: str = Form("fill_crop"),
    motion: str = Form("random"),
    fps: int = Form(30),
    crf: int = Form(20),
    base_name: str = Form("short_video.mp4"),
    input_mode: str = Form("upload"),
    drive_input_path: str = Form(""),
    drive_input_paths_json: str = Form(""),
    upload_to_drive: bool = Form(False),
    available_to_short_uploader: bool | None = Form(None),
    drive_output_path: str = Form(DEFAULT_DRIVE_OUTPUT),
    verified_clean: bool = Form(True),
    short_vps_folder_name: str = Form("", alias="short_vps_folder"),
):
    input_mode=input_mode if input_mode in ("upload","drive") else "upload"
    # Backward-compatible API inference; explicit checkbox states must be exclusive.
    if available_to_short_uploader is None:
        available_to_short_uploader = not upload_to_drive
    if bool(upload_to_drive) == bool(available_to_short_uploader):
        raise HTTPException(status_code=400, detail="Select exactly one output destination.")
    short_vps_folder_name = short_vps_folder_name or SHORT_VPS_DEFAULT
    try:
        short_vps_folder_names()  # Ensure protected default folder exists.
        short_target = short_vps_folder(short_vps_folder_name) if short_vps_folder_name else None
        if short_target is not None and not short_target.is_dir():
            raise ValueError("Selected VPS folder does not exist.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if input_mode=="upload" and not images:
        raise HTTPException(status_code=400, detail="At least one uploaded image is required.")
    drive_input_paths = []
    if input_mode=="drive":
        try:
            drive_input_paths = parse_drive_folder_inputs(drive_input_path, drive_input_paths_json)
            drive_input_path = drive_input_paths[0]
        except Exception as exc:
            raise HTTPException(status_code=400,detail=str(exc))
    if upload_to_drive:
        try:
            drive_output_path=normalize_drive_path(drive_output_path)
        except Exception as exc:
            raise HTTPException(status_code=400,detail=str(exc))
    else:
        verified_clean=False
    if upload_to_drive:
        verified_clean=True  # Master rule: verified Drive output always cleans job-local data.
    if video_duration <= 0 or image_duration <= 0:
        raise HTTPException(status_code=400, detail="Durations must be greater than zero.")
    if output_count < 1 or output_count > 999:
        raise HTTPException(status_code=400, detail="Output count must be 1–999.")
    if fps not in (24, 25, 30, 50, 60):
        raise HTTPException(status_code=400, detail="Unsupported FPS.")
    if not 16 <= crf <= 28:
        raise HTTPException(status_code=400, detail="CRF must be 16–28.")

    job_id = uuid.uuid4().hex[:16]
    job_dir = JOBS_ROOT / job_id
    image_dir = job_dir / "input" / "images"
    audio_dir = job_dir / "input" / "audio"
    image_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    image_paths = []
    for idx, upload in enumerate(images, 1):
        target = image_dir / f"{idx:05d}_{safe_name(upload.filename)}"
        with target.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        image_paths.append(str(target))

    audio_paths = []
    for idx, upload in enumerate(audio or [], 1):
        target = audio_dir / f"{idx:04d}_{safe_name(upload.filename)}"
        with target.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        audio_paths.append(str(target))

    try:
        reserved_names = reserve_short_output_names(short_vps_folder_name, output_count)
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise HTTPException(status_code=400, detail="Output naming reservation failed: " + str(exc))

    cfg = {
        "images": image_paths,
        "audio": audio_paths,
        "video_duration": video_duration,
        "image_duration": image_duration,
        "output_count": output_count,
        "order": order if order in ("random", "filename") else "random",
        "fit": fit if fit in ("fill_crop", "black", "blur") else "fill_crop",
        "motion": motion if motion in ("random", "zoom_in", "zoom_out", "pan_lr", "pan_rl", "none") else "random",
        "fps": fps,
        "crf": crf,
        "base_name": safe_name(base_name),  # Legacy job compatibility.
        "output_names": reserved_names,
        "batch_tag": reserved_names[0].rsplit("_", 1)[-1].removesuffix(".mp4"),
        "input_mode": input_mode,
        "drive_input_path": drive_input_path,
        "drive_input_paths": drive_input_paths,
        "upload_to_drive": bool(upload_to_drive),
        "available_to_short_uploader": bool(available_to_short_uploader),
        "drive_output_path": drive_output_path,
        "verified_clean": bool(verified_clean),
        "short_vps_folder": short_vps_folder_name,
    }

    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id, "status": "queued", "progress": 0,
            "created_at": utc_now(),
            "message": "Queued", "outputs": [],
            "delivery_status": "waiting_render" if upload_to_drive else "short_uploader",
            "drive_verified": False,
            "local_cleaned": False,
            "config": {k: v for k, v in cfg.items() if k not in ("images", "audio")}
        }
    save_state(job_id)
    executor.submit(render_job, job_id, cfg)
    return {"job_id": job_id, "status": "queued"}



@app.get("/api/jobs")
def list_jobs():
    items=load_job_states()
    counts={"uploading":0,"queued":0,"running":0,"completed":0,"failed":0}
    for item in items:
        st=str(item.get("status") or "unknown")
        if st in counts:
            counts[st]+=1
    return {
        "jobs":items,
        "counts":counts,
        "single_concurrency":True,
        "max_workers":1,
        "multiple_submission":True,
        "persistent_recovery":True,
    }

@app.delete("/api/jobs/{job_id}")
def delete_terminal_job(job_id: str):
    state=get_state(job_id)
    if state.get("status") not in ("completed","failed"):
        raise HTTPException(status_code=409,detail="Only completed or failed jobs can be deleted.")
    shutil.rmtree(JOBS_ROOT / job_id,ignore_errors=True)
    with jobs_lock:
        jobs.pop(job_id,None)
    return {"ok":True,"job_id":job_id}

@app.post("/api/jobs/clear/{status_name}")
def clear_terminal_jobs(status_name: str):
    if status_name not in ("completed","failed"):
        raise HTTPException(status_code=400,detail="Only completed or failed jobs can be cleared.")
    removed=0
    for state in load_job_states():
        if state.get("status")!=status_name:
            continue
        job_id=str(state.get("job_id") or "")
        if not job_id:
            continue
        shutil.rmtree(JOBS_ROOT / job_id,ignore_errors=True)
        with jobs_lock:
            jobs.pop(job_id,None)
        removed+=1
    return {"ok":True,"status":status_name,"removed":removed}

@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    return JSONResponse(get_state(job_id))


@app.get("/outputs/{job_id}/{filename}")
def download_output(job_id: str, filename: str):
    target = JOBS_ROOT / job_id / "outputs" / safe_name(filename)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Output not found")
    return FileResponse(target, media_type="video/mp4", filename=target.name)


# SHORT_MAKER_RESUMABLE_UPLOAD_V1
# The old multipart /api/jobs endpoint remains available for existing clients.
# New browser jobs are allocated before any bytes are sent; each file is appended
# in bounded chunks and can resume from its server-recorded offset after refresh.
UPLOAD_CHUNK_LIMIT = 8 * 1024 * 1024
UPLOAD_FILE_LIMIT = 1024 * 1024 * 1024
UPLOAD_TOTAL_LIMIT = 8 * 1024 * 1024 * 1024


def upload_manifest_path(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", job_id or ""):
        raise HTTPException(status_code=404, detail="Upload session not found.")
    return JOBS_ROOT / job_id / "upload_manifest.json"


def upload_manifest(job_id: str, request: Request) -> dict:
    path = upload_manifest_path(job_id)
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail="Upload session not found.")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise HTTPException(status_code=500, detail="Upload manifest is unreadable.")
    if manifest.get("owner", "") != request.headers.get("x-auth-user", "").strip():
        raise HTTPException(status_code=403, detail="Upload belongs to another account.")
    return manifest


@contextmanager
def upload_session_lock(job_id: str):
    # Interprocess flock protects against double finalize or concurrent chunks.
    lock_path = upload_manifest_path(job_id).parent / ".upload.lock"
    if lock_path.parent.is_symlink():
        raise HTTPException(status_code=400, detail="Unsafe upload folder.")
    with lock_path.open("a+b") as file:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def upload_target(job_id: str, kind: str, index: int, entry: dict) -> Path:
    if kind not in ("images", "audio") or index < 0:
        raise HTTPException(status_code=400, detail="Invalid upload file.")
    prefix = f"{index + 1:05d}" if kind == "images" else f"{index + 1:04d}"
    return JOBS_ROOT / job_id / "input" / kind / f"{prefix}_{safe_name(entry['name'])}"


def upload_snapshot(job_id: str, manifest: dict) -> dict:
    result = {"images": [], "audio": []}
    received = total = 0
    for kind in ("images", "audio"):
        for index, entry in enumerate(manifest["files"][kind]):
            path = upload_target(job_id, kind, index, entry)
            size = int(entry["size"])
            final_size = path.stat().st_size if path.is_file() and not path.is_symlink() else 0
            partial = path.with_name(path.name + ".part")
            part_size = partial.stat().st_size if partial.is_file() and not partial.is_symlink() else 0
            offset = size if final_size == size else min(size, part_size)
            result[kind].append({"index": index, "size": size, "uploaded": offset, "name": entry["name"]})
            received += offset
            total += size
    return {"files": result, "uploaded_bytes": received, "total_bytes": total,
            "upload_progress": round(100 * received / total, 1) if total else 100}


def upload_config(payload: dict) -> dict:
    try:
        duration = float(payload.get("video_duration", 16))
        img_duration = float(payload.get("image_duration", 3))
        count = int(payload.get("output_count", 1))
        fps = int(payload.get("fps", 30))
        crf = int(payload.get("crf", 20))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid render settings.")
    if not (0 < duration <= 86400 and 0 < img_duration <= 86400):
        raise HTTPException(status_code=400, detail="Durations must be positive.")
    if not 1 <= count <= 999 or fps not in (24, 25, 30, 50, 60) or not 16 <= crf <= 28:
        raise HTTPException(status_code=400, detail="Invalid output count, FPS or CRF.")
    folder_name = str(payload.get("short_vps_folder") or SHORT_VPS_DEFAULT)
    short_vps_folder_names()
    try:
        if not short_vps_folder(folder_name).is_dir():
            raise ValueError("Selected VPS output folder no longer exists.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    to_drive = payload.get("upload_to_drive") is True
    to_uploader = payload.get("available_to_short_uploader") is True
    if to_drive == to_uploader:
        raise HTTPException(status_code=400, detail="Select exactly one output destination.")
    try:
        drive_output = normalize_drive_path(str(payload.get("drive_output_path") or DEFAULT_DRIVE_OUTPUT)) if to_drive else DEFAULT_DRIVE_OUTPUT
        drive_paths = parse_drive_folder_inputs(
            str(payload.get("drive_input_path") or ""),
            str(payload.get("drive_input_paths_json") or "")
        ) if payload.get("input_mode") == "drive" else []
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    mode = "drive" if payload.get("input_mode") == "drive" else "upload"
    return {
        "video_duration": duration, "image_duration": img_duration,
        "output_count": count,
        "fps": fps, "crf": crf,
        "order": payload.get("order") if payload.get("order") in ("random", "filename") else "random",
        "fit": payload.get("fit") if payload.get("fit") in ("fill_crop", "black", "blur") else "fill_crop",
        "motion": payload.get("motion") if payload.get("motion") in ("random", "zoom_in", "zoom_out", "pan_lr", "pan_rl", "none") else "random",
        "base_name": safe_name(str(payload.get("base_name") or "short_video.mp4")),
        "input_mode": mode, "drive_input_path": drive_paths[0] if drive_paths else "",
        "drive_input_paths": drive_paths,
        "upload_to_drive": to_drive, "available_to_short_uploader": to_uploader,
        "drive_output_path": drive_output, "verified_clean": to_drive,
        "short_vps_folder": folder_name,
    }


@app.post("/api/upload-sessions")
async def begin_resumable_upload(request: Request):
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload request.")
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
        raise HTTPException(status_code=400, detail="Missing upload manifest.")
    if not isinstance(payload.get("config"), dict):
        raise HTTPException(status_code=400, detail="Missing render settings.")
    cfg = upload_config(payload["config"])
    file_groups = payload["files"]
    manifest_files = {}
    grand_total = 0
    for kind in ("images", "audio"):
        group = file_groups.get(kind, [])
        if not isinstance(group, list) or len(group) > 5000:
            raise HTTPException(status_code=400, detail="Too many files.")
        clean_files = []
        for entry in group:
            if not isinstance(entry, dict):
                raise HTTPException(status_code=400, detail="Invalid file metadata.")
            name = safe_name(str(entry.get("name") or ""))
            try:
                size = int(entry.get("size"))
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="Invalid file size.")
            if size < 1 or size > UPLOAD_FILE_LIMIT:
                raise HTTPException(status_code=400, detail="File size outside supported range.")
            if kind == "images" and not is_image(Path(name)):
                raise HTTPException(status_code=400, detail="Unsupported image: " + name)
            grand_total += size
            clean_files.append({"name": name, "size": size})
        manifest_files[kind] = clean_files
    if grand_total > UPLOAD_TOTAL_LIMIT:
        raise HTTPException(status_code=413, detail="Total upload exceeds 8 GiB.")
    if cfg["input_mode"] == "upload" and not manifest_files["images"]:
        raise HTTPException(status_code=400, detail="Select at least one image.")
    if cfg["input_mode"] == "drive" and manifest_files["images"]:
        raise HTTPException(status_code=400, detail="Drive input cannot include PC images.")
    job_id = uuid.uuid4().hex[:16]
    root = JOBS_ROOT / job_id
    for kind in ("images", "audio"):
        (root / "input" / kind).mkdir(parents=True, exist_ok=True)
    manifest = {"owner": request.headers.get("x-auth-user", "").strip(),
                "config": cfg, "files": manifest_files, "created_at": utc_now()}
    upload_manifest_path(job_id).write_text(json.dumps(manifest), encoding="utf-8")
    set_state(job_id, job_id=job_id, status="uploading", progress=0, upload_progress=0,
              created_at=utc_now(), message="Uploading PC files (resumable)",
              outputs=[], config=cfg, delivery_status="waiting_upload",
              drive_verified=False, local_cleaned=False)
    return {"job_id": job_id, "status": "uploading"}


@app.get("/api/upload-sessions/{job_id}")
def get_resumable_upload(job_id: str, request: Request):
    manifest = upload_manifest(job_id, request)
    state = get_state(job_id)
    snapshot = upload_snapshot(job_id, manifest)
    return {"job_id": job_id, "status": state.get("status"), **snapshot}


@app.put("/api/upload-sessions/{job_id}/files/{kind}/{index}")
async def append_resumable_chunk(job_id: str, kind: str, index: int, request: Request):
    manifest = upload_manifest(job_id, request)
    if kind not in ("images", "audio") or not 0 <= index < len(manifest["files"][kind]):
        raise HTTPException(status_code=404, detail="Upload file not found.")
    try:
        offset = int(request.headers.get("x-upload-offset", "-1"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid chunk offset.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > UPLOAD_CHUNK_LIMIT:
            raise HTTPException(status_code=413, detail="Upload chunk exceeds 8 MiB.")
    if not body:
        raise HTTPException(status_code=400, detail="Empty chunk.")
    with upload_session_lock(job_id):
        if get_state(job_id).get("status") != "uploading":
            raise HTTPException(status_code=409, detail="Upload is already finalized.")
        entry = manifest["files"][kind][index]
        target = upload_target(job_id, kind, index, entry)
        partial = target.with_name(target.name + ".part")
        size = int(entry["size"])
        if target.exists():
            if target.stat().st_size == size:
                return {"uploaded": size, "complete": True}
            raise HTTPException(status_code=409, detail="Uploaded file size mismatch.")
        current = partial.stat().st_size if partial.exists() else 0
        if offset != current or current + len(body) > size:
            raise HTTPException(status_code=409, detail="Chunk offset mismatch; refresh status.")
        with partial.open("ab") as dst:
            dst.write(body)
            dst.flush()
            os.fsync(dst.fileno())
        current += len(body)
        if current == size:
            os.replace(partial, target)
        snapshot = upload_snapshot(job_id, manifest)
        set_state(job_id, progress=snapshot["upload_progress"],
                  upload_progress=snapshot["upload_progress"],
                  message=f"Uploading PC files · {snapshot['upload_progress']}%")
        return {"uploaded": current, "complete": current == size,
                "upload_progress": snapshot["upload_progress"]}


@app.post("/api/upload-sessions/{job_id}/finalize")
def finalize_resumable_upload(job_id: str, request: Request):
    manifest = upload_manifest(job_id, request)
    with upload_session_lock(job_id):
        state = get_state(job_id)
        if state.get("status") != "uploading":
            # Idempotent: refreshes and retries never submit a second render.
            return {"job_id": job_id, "status": state.get("status")}
        snapshot = upload_snapshot(job_id, manifest)
        if snapshot["uploaded_bytes"] != snapshot["total_bytes"]:
            raise HTTPException(status_code=409, detail="Upload incomplete; resume remaining files.")
        cfg = dict(manifest["config"])
        cfg["images"] = [str(upload_target(job_id, "images", i, entry))
                         for i, entry in enumerate(manifest["files"]["images"])]
        cfg["audio"] = [str(upload_target(job_id, "audio", i, entry))
                        for i, entry in enumerate(manifest["files"]["audio"])]
        if any(not Path(p).is_file() for p in cfg["images"] + cfg["audio"]):
            raise HTTPException(status_code=409, detail="Some uploaded files are missing.")
        try:
            names = reserve_short_output_names(cfg["short_vps_folder"], cfg["output_count"])
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(status_code=400, detail="Output reservation failed: " + str(exc))
        cfg["output_names"] = names
        cfg["batch_tag"] = names[0].rsplit("_", 1)[-1].removesuffix(".mp4")
        set_state(job_id, status="queued", progress=0, upload_progress=100,
                  message="Uploaded · queued for VPS rendering",
                  delivery_status="waiting_render" if cfg["upload_to_drive"] else "short_uploader",
                  config={k: v for k, v in cfg.items() if k not in ("images", "audio")})
        executor.submit(render_job, job_id, cfg)
        return {"job_id": job_id, "status": "queued"}


@app.get("/resumable_client.js")
def resumable_client_script():
    return FileResponse(Path(__file__).with_name("resumable_client.js"), media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})

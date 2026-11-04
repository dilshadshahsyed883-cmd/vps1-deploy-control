"""Strict CUDA/NVENC candidate for a deliberately narrow Concat v0.2.8 timeline.

Video *per-frame* scale/pan/compositing/encoding is GPU-only. Still images are
decoded and converted to a single planar frame ONCE at input ingestion (CPU).
This exception is explicitly reported; it is not a CPU video-filter fallback.
Real NVIDIA acceptance remains mandatory before production promotion.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

MAX_DURATION = 1800
MAX_CLIPS = 120
ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
RENDERER_VERSION = "cuda-nvenc-candidate-v1"
VIDEO_FILTERS = ("hwupload_cuda", "scale_cuda", "overlay_cuda")


class CUDACompatibilityError(RuntimeError):
    """Unsupported required timeline content; never downgrade to CPU."""


def _run(args: list[str], *, timeout=7200) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CUDACompatibilityError(f"{name}: numeric value required")
    value = float(value)
    if not math.isfinite(value):
        raise CUDACompatibilityError(f"{name}: non-finite value")
    return value


def probe_cuda() -> dict:
    """Truthful capabilities; native Vulkan availability is unrelated."""
    result = {
        "backend": "cuda_nvenc", "renderer_version": RENDERER_VERSION,
        "cuda_nvenc_ready": False, "nvidia_visible": False,
        "missing": [], "hardware_render_tested": False,
    }
    for binary in ("ffmpeg", "ffprobe", "nvidia-smi"):
        if not shutil.which(binary):
            result["missing"].append(binary)
    if result["missing"]:
        return result
    try:
        gpu = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=8)
        result["nvidia_visible"] = gpu.returncode == 0 and bool(gpu.stdout.strip())
        if not result["nvidia_visible"]:
            result["missing"].append("NVIDIA GPU")
        filters = _run(["ffmpeg", "-hide_banner", "-filters"], timeout=8)
        encoders = _run(["ffmpeg", "-hide_banner", "-encoders"], timeout=8)
        for name in VIDEO_FILTERS:
            if filters.returncode != 0 or name not in filters.stdout:
                result["missing"].append(name)
        if encoders.returncode != 0 or "h264_nvenc" not in encoders.stdout:
            result["missing"].append("h264_nvenc")
        result["cuda_nvenc_ready"] = not result["missing"]
    except (OSError, subprocess.TimeoutExpired):
        result["missing"].append("NVIDIA/FFmpeg diagnostics failed")
    return result


def _dimensions(path: Path) -> tuple[int, int]:
    proc = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "json", str(path)], timeout=15)
    if proc.returncode:
        raise CUDACompatibilityError(f"Image probe failed: {path.name}")
    try:
        video = json.loads(proc.stdout)["streams"][0]
        w, h = int(video["width"]), int(video["height"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise CUDACompatibilityError(f"Cannot determine dimensions: {path.name}") from None
    if not 16 <= w <= 8192 or not 16 <= h <= 8192:
        raise CUDACompatibilityError(f"Image dimensions unsupported: {path.name}")
    return w, h


def _event(clip: dict, effect: str, reason: str, status="skipped") -> dict:
    return {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "renderer": RENDERER_VERSION, "clip_id": str(clip.get("id", "")),
            "effect_id": effect, "position_s": clip.get("start", 0),
            "decision": status, "reason_code": reason}


def _pan(keys, property_name: str, initial: float) -> tuple[float, float]:
    entries = [k for k in keys if k.get("property") == property_name]
    if not entries:
        return initial, initial
    entries.sort(key=lambda key: key.get("at", 0))
    if (len(entries) != 2 or abs(_finite(entries[0].get("at", -1), "key at")) > 1e-6
            or abs(_finite(entries[1].get("at", -1), "key at") - 1.0) > 1e-6):
        raise CUDACompatibilityError(f"{property_name} keyframes require exactly two endpoint keys")
    for k in entries:
        ease = k.get("ease", "linear")
        if ease not in ("linear", None):
            raise CUDACompatibilityError(f"{property_name} easing not yet supported on CUDA")
    return (_finite(entries[0]["value"], "key value"),
            _finite(entries[1]["value"], "key value"))


def parse_project(project_dir: Path) -> dict:
    """Fail closed on essential unsupported Concat semantics, log safe skips."""
    project = project_dir / "concat.json"
    if not project.is_file():
        raise CUDACompatibilityError("CUDA requires concat.json (bundle version 1)")
    data = json.loads(project.read_text(encoding="utf-8-sig"))
    if data.get("version", 1) != 1:
        raise CUDACompatibilityError("Concat project document version unsupported")
    timelines = data.get("timelines")
    if timelines:
        matches = [t for t in timelines if t.get("id") == data.get("activeTimelineId")]
        timeline = matches[0] if matches else timelines[0]
    else:
        timeline = data
    settings = timeline.get("video") or data.get("video") or {}
    width = int(settings.get("width", 1920))
    height = int(settings.get("height", 1080))
    rate_num = int(settings.get("rateNum", 30))
    rate_den = int(settings.get("rateDen", 1))
    fps = rate_num / rate_den if rate_den else 0
    if (not 64 <= width <= 3840 or width % 2 or
            not 64 <= height <= 2160 or height % 2 or
            not 1 <= fps <= 60 or settings.get("colorSpace", "sdr") != "sdr"):
        raise CUDACompatibilityError("GPU candidate requires even SDR frame <=3840x2160, 1-60 FPS")
    tracks = {t.get("id"): t for t in timeline.get("tracks", [])}
    media = {m.get("id"): m for m in data.get("media", [])}
    clips = timeline.get("clips", [])
    if not isinstance(clips, list) or not clips:
        raise CUDACompatibilityError("Concat timeline is empty")
    skipped: list[dict] = []
    image_clips: list[dict] = []
    audio_clips: list[dict] = []
    for clip in clips:
        kind = clip.get("kind")
        cid = clip.get("id", "?")
        if clip.get("trackId") not in tracks:
            raise CUDACompatibilityError(f"Clip {cid}: unknown track")
        track = tracks[clip["trackId"]]
        if kind in ("text", "shape", "layer", "video"):
            raise CUDACompatibilityError(f"Clip {cid}: essential {kind} rendering not supported in CUDA v1")
        if kind not in ("image", "audio"):
            raise CUDACompatibilityError(f"Clip {cid}: unknown clip kind {kind}")
        if kind == "image" and track.get("visible") is False:
            skipped.append(_event(clip, "hidden_track", "TRACK_NOT_VISIBLE"))
            continue
        if kind == "audio" and track.get("muted") is True:
            skipped.append(_event(clip, "muted_track", "TRACK_MUTED"))
            continue
        m = media.get(clip.get("mediaId"))
        if not m or not isinstance(m.get("path"), str) or not m["path"]:
            raise CUDACompatibilityError(f"Clip {cid}: missing required media")
        path = Path(m["path"]).resolve()
        if not path.is_file() or not path.is_relative_to(project_dir.resolve()):
            raise CUDACompatibilityError(f"Clip {cid}: media missing or outside verified bundle")
        start = _finite(clip.get("start", 0), "clip start")
        duration = _finite(clip.get("duration", 0), "clip duration")
        if start < 0 or duration <= 0:
            raise CUDACompatibilityError(f"Clip {cid}: invalid placement")
        c = dict(clip, _path=path, _start=start, _duration=duration)
        if kind == "image":
            if path.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
                raise CUDACompatibilityError(f"Clip {cid}: only still images supported by CUDA v1")
            if abs(_finite(clip.get("sourceStart", 0), "source start")) > 1e-6:
                raise CUDACompatibilityError(f"Clip {cid}: trimmed image not supported")
            if clip.get("background") or timeline.get("background"):
                raise CUDACompatibilityError(f"Clip {cid}: custom backgrounds require GPU implementation")
            for field in ("rotation", "flipH", "flipV", "crop", "cutout",
                          "mask", "stabilize", "enhanced", "speedCurve"):
                if clip.get(field) not in (None, False, 0, [], {}):
                    skipped.append(_event(clip, field, "GPU_EFFECT_UNSUPPORTED"))
            if clip.get("blend", "normal") not in ("normal", ""):
                skipped.append(_event(clip, "blend", "GPU_EFFECT_UNSUPPORTED"))
            if _finite(clip.get("opacity", 1), "opacity") != 1:
                skipped.append(_event(clip, "opacity", "GPU_EFFECT_UNSUPPORTED"))
            if clip.get("transitionIn"):
                skipped.append(_event(clip, clip["transitionIn"].get("id", "transition"),
                                      "GPU_TRANSITION_UNSUPPORTED"))
            if clip.get("videoEffects"):
                for effect in clip["videoEffects"]:
                    if effect.get("enabled", True):
                        skipped.append(_event(clip, effect.get("id", "unknown"), "GPU_EFFECT_UNSUPPORTED"))
            keys = clip.get("keys", [])
            for key in keys:
                if key.get("property") not in ("offsetX", "offsetY"):
                    skipped.append(_event(clip, "key:" + str(key.get("property")),
                                          "GPU_ANIMATION_UNSUPPORTED"))
            scale = _finite(clip.get("scale", 1), "scale")
            if not 1.0 <= scale <= 1.8:
                raise CUDACompatibilityError(f"Clip {cid}: supported fixed GPU scale is 1.0-1.8")
            x0, x1 = _pan(keys, "offsetX", _finite(clip.get("offsetX", 0), "offsetX"))
            y0, y1 = _pan(keys, "offsetY", _finite(clip.get("offsetY", 0), "offsetY"))
            if any(abs(v) > (scale - 1) / 2 + 1e-8 for v in (x0, x1, y0, y1)):
                raise CUDACompatibilityError(f"Clip {cid}: pan exceeds zoom safe crop")
            c.update(scale=scale, pan=(x0, x1, y0, y1))
            image_clips.append(c)
        else:
            audio_clips.append(c)
    if not 1 <= len(image_clips) <= MAX_CLIPS:
        raise CUDACompatibilityError("CUDA v1 needs 1-120 image clips")
    image_clips.sort(key=lambda c: c["_start"])
    cursor = 0
    for c in image_clips:
        if abs(c["_start"] - cursor) > 0.001:
            raise CUDACompatibilityError("GPU v1 requires gap-free, nonoverlapping images; unsafe transition skip")
        cursor += c["_duration"]
        frame_end = round(cursor * fps)
        if abs(frame_end / fps - cursor) > 0.002:
            raise CUDACompatibilityError("Clip boundaries must be frame aligned for stream-copy concat")
    if not 0 < cursor <= MAX_DURATION:
        raise CUDACompatibilityError("Timeline longer than supported CUDA v1 limit")
    if len(audio_clips) > 1:
        raise CUDACompatibilityError("Multiple audio clips/mixing not supported; audio cannot be silently dropped")
    if audio_clips:
        a = audio_clips[0]
        if (abs(a["_start"]) > 0.001 or abs(a["_duration"] - cursor) > 0.05
                or abs(_finite(a.get("sourceStart", 0), "audio trim")) > 1e-6
                or a.get("filters") or a.get("fadeIn") or a.get("fadeOut")
                or _finite(a.get("volume", 1), "volume") != 1.0
                or a.get("speedCurve") or _finite(a.get("speed", 1), "speed") != 1.0):
            raise CUDACompatibilityError("Audio v1 requires single unmodified AAC track starting at zero")
    return {"width": width, "height": height, "fps": fps, "duration": cursor,
            "images": image_clips, "audio": audio_clips[0] if audio_clips else None,
            "skipped": skipped}


def segment_command(raw: Path, dimensions: tuple[int, int], clip: dict,
                    target: Path, *, width: int, height: int, fps: float, frames: int) -> list[str]:
    source_w, source_h = dimensions
    if abs(source_w / source_h - width / height) > 0.002:
        raise CUDACompatibilityError("GPU v1 refuses aspect-ratio mismatch; no silent stretching")
    zoom = clip["scale"]
    zoom_w = max(width, math.ceil(width * zoom / 2) * 2)
    zoom_h = max(height, math.ceil(height * zoom / 2) * 2)
    x0, x1, y0, y1 = clip["pan"]
    dt = clip["_duration"]
    # All video filters after one-time media intake are CUDA-only.
    xpos = f"{-(zoom_w-width)/2:.6f}+{width*x0:.6f}+({width*(x1-x0):.6f})*min(max(t/{dt:.6f},0),1)"
    ypos = f"{-(zoom_h-height)/2:.6f}+{height*y0:.6f}+({height*(y1-y0):.6f})*min(max(t/{dt:.6f},0),1)"
    graph = (
        f"[0:v]hwupload_cuda,scale_cuda={width}:{height}:interp_algo=bilinear,"
        f"split=2[base][source];"
        f"[source]scale_cuda={zoom_w}:{zoom_h}:interp_algo=bilinear[zoom];"
        f"[base][zoom]overlay_cuda=x='{xpos}':y='{ypos}':eval=frame[v]"
    )
    assert "format=" not in graph and "zoompan" not in graph and "xfade" not in graph
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-init_hw_device", "cuda=gpu:0", "-filter_hw_device", "gpu",
        "-stream_loop", "-1", "-f", "rawvideo", "-pixel_format", "yuv420p",
        "-video_size", f"{source_w}x{source_h}", "-framerate", str(fps), "-i", str(raw),
        "-filter_complex", graph, "-map", "[v]",
        "-c:v", "h264_nvenc", "-preset", "p4", "-b:v", "5M",
        "-pix_fmt", "cuda", "-an", "-frames:v", str(frames),
        "-movflags", "+faststart", str(target)
    ]


def _image_to_one_frame(source: Path, target: Path) -> tuple[int, int]:
    dims = _dimensions(source)
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-i", str(source), "-frames:v", "1", "-pix_fmt", "yuv420p",
            "-f", "rawvideo", str(target)]
    proc = _run(args, timeout=90)
    if proc.returncode or not target.is_file() or target.stat().st_size != dims[0]*dims[1]*3//2:
        raise CUDACompatibilityError(f"One-time still-image ingestion failed: {source.name}; {proc.stderr[-300:]}")
    return dims


def _concat_segments(segments: list[Path], destination: Path) -> None:
    concat_file = destination.with_suffix(".ffconcat")
    concat_file.write_text("ffconcat version 1.0\n" +
                           "".join("file '" + p.name + "'\n" for p in segments), encoding="utf-8")
    proc = _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                 "-f", "concat", "-safe", "1", "-i", str(concat_file),
                 "-map", "0:v:0", "-c:v", "copy", "-an", "-movflags", "+faststart",
                 str(destination)], timeout=7200)
    if proc.returncode:
        raise CUDACompatibilityError("CUDA segments could not be combined via stream copy: " + proc.stderr[-350:])


def _aac_media(source: Path):
    proc = _run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name", "-of", "json", str(source)], timeout=30)
    if proc.returncode:
        raise CUDACompatibilityError("Audio probe failed")
    streams = json.loads(proc.stdout).get("streams", [])
    if not streams or streams[0].get("codec_name") != "aac":
        raise CUDACompatibilityError("Audio v1 requires AAC stream-copy (no CPU audio encoder)")


def render(project: Path, output: Path, *, job_id: str = "") -> dict:
    """Render verified Concat project subset, never fallback to CPU video filters."""
    info = parse_project(project)
    if not probe_cuda()["cuda_nvenc_ready"]:
        raise CUDACompatibilityError("CUDA/NVENC unavailable; CPU fallback forbidden")
    report = project.parent / "engine-events.jsonl"
    events = [
        {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "job_id": job_id, "renderer": RENDERER_VERSION,
         "decision": "info", "reason_code": "ONE_TIME_CPU_IMAGE_DECODE",
         "message": "Still-media ingestion converts each image once before GPU frame processing"}
    ] + [dict(e, job_id=job_id) for e in info["skipped"]]
    report.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in events),
                      encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="concat-cuda-", dir=project.parent) as tmp:
        scratch = Path(tmp)
        segments = []
        previous = 0
        for index, clip in enumerate(info["images"]):
            raw = scratch / f"image-{index:04d}.yuv"
            dimensions = _image_to_one_frame(clip["_path"], raw)
            end = round((clip["_start"] + clip["_duration"]) * info["fps"])
            frames = end - previous
            previous = end
            if frames <= 0:
                raise CUDACompatibilityError("Clip has no output frame")
            seg = scratch / f"segment-{index:04d}.mp4"
            cmd = segment_command(raw, dimensions, clip, seg, width=info["width"],
                                  height=info["height"], fps=info["fps"], frames=frames)
            proc = _run(cmd)
            if proc.returncode:
                raise CUDACompatibilityError("CUDA/NVENC segment render failed; no CPU fallback: " +
                                             proc.stderr[-500:])
            if not seg.is_file() or seg.stat().st_size < 100:
                raise CUDACompatibilityError("CUDA did not generate video segment")
            segments.append(seg)
        silent = scratch / "visual.mp4"
        _concat_segments(segments, silent)
        audio = info["audio"]
        if audio:
            _aac_media(audio["_path"])
            proc = _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                         "-y", "-i", str(silent), "-i", str(audio["_path"]),
                         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                         "-c:a", "copy", "-t", f"{info['duration']:.6f}",
                         "-movflags", "+faststart", str(output)])
            if proc.returncode:
                raise CUDACompatibilityError("AAC mux failed (no CPU audio re-encode): " + proc.stderr[-500:])
        else:
            shutil.copyfile(silent, output)
    probe = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=nk=1:nw=1", str(output)], timeout=15)
    if probe.returncode:
        raise CUDACompatibilityError("Output ffprobe failed")
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        raise CUDACompatibilityError("Output duration unavailable") from None
    if abs(duration-info["duration"]) > 0.25:
        raise CUDACompatibilityError("Output timeline duration mismatch; file retained")
    return {"renderer": RENDERER_VERSION, "skipped_effects": len(info["skipped"]),
            "event_log": report.name, "duration": duration,
            "note": "GPU-only per-frame video; one-time CPU still-media ingestion"}

"""Post-render video compression helpers.

This module contains the compression logic extracted from Video Folder Compressor
v1.2.0, without any Tkinter UI. It is intended for one-file post-render
compression inside Auto Caption Studio.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ffmpeg_utils import FFmpegError, find_tool, require_ffmpeg

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".m4v", ".webm"}

QUALITY_PRESETS = {
    "Balanced Fast": {
        "mode": "crf",
        "crf": "28",
        "preset": "veryfast",
        "audio": "128k",
        "estimate_factor": 0.35,
        "desc": "Recommended fast default",
    },
    "Fast Small": {
        "mode": "crf",
        "crf": "30",
        "preset": "veryfast",
        "audio": "96k",
        "estimate_factor": 0.27,
        "desc": "Faster, smaller output, more quality loss",
    },
    "High Quality Fast": {
        "mode": "crf",
        "crf": "24",
        "preset": "fast",
        "audio": "128k",
        "estimate_factor": 0.52,
        "desc": "Better quality, still faster than old build",
    },
    "Ultra Fast Draft": {
        "mode": "crf",
        "crf": "32",
        "preset": "ultrafast",
        "audio": "96k",
        "estimate_factor": 0.40,
        "desc": "Maximum speed, less predictable size",
    },
    # Legacy aliases accepted for old saved settings.
    "Balanced": {
        "mode": "crf",
        "crf": "28",
        "preset": "veryfast",
        "audio": "128k",
        "estimate_factor": 0.35,
        "desc": "Legacy alias for Balanced Fast",
    },
    "High Quality": {
        "mode": "crf",
        "crf": "24",
        "preset": "fast",
        "audio": "128k",
        "estimate_factor": 0.52,
        "desc": "Legacy alias for High Quality Fast",
    },
    "Small Size": {
        "mode": "crf",
        "crf": "30",
        "preset": "veryfast",
        "audio": "96k",
        "estimate_factor": 0.27,
        "desc": "Legacy alias for Fast Small",
    },
}

VISIBLE_QUALITY_PRESETS = [
    "Balanced Fast",
    "Fast Small",
    "High Quality Fast",
    "Ultra Fast Draft",
]

ENCODER_CHOICES = [
    "CPU Fast (Reliable)",
    "Auto Hardware + CPU Fallback",
    "Intel Quick Sync (QSV)",
    "NVIDIA NVENC",
    "AMD AMF",
]

ENCODER_KEYS = {
    "Intel Quick Sync (QSV)": "h264_qsv",
    "NVIDIA NVENC": "h264_nvenc",
    "AMD AMF": "h264_amf",
}

ProgressCallback = Callable[[int, str], None]
LogCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]


class CompressionCancelled(RuntimeError):
    """Raised when the caller cancels post-render compression."""


@dataclass
class CompressionResult:
    success: bool
    compressed: bool
    skipped: bool
    source_path: str
    original_size: int
    final_size: int
    message: str
    used_encoder: str = ""


def fmt_size(num: int | float) -> str:
    try:
        num = float(num)
    except Exception:
        return "-"
    if num <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while num >= 1024 and idx < len(units) - 1:
        num /= 1024.0
        idx += 1
    return f"{num:.1f} {units[idx]}" if idx else f"{int(num)} {units[idx]}"


def fmt_time(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return "-"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_audio_kbps(audio: str) -> int:
    try:
        return int(str(audio).lower().replace("k", "").strip())
    except Exception:
        return 128


def _creationflags() -> int:
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return 0


def _startupinfo():
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    return si


def _temp_path_for(src: Path) -> Path:
    return src.with_name(f"{src.stem}.__compressing__{src.suffix}")


def _backup_path_for(src: Path) -> Path:
    return src.with_name(f"{src.stem}.__backup_before_compress__{src.suffix}")


def _ffprobe_path() -> str | None:
    return find_tool("ffprobe")


def get_duration(path: str) -> float:
    ffprobe = _ffprobe_path()
    if not ffprobe:
        return 0.0
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_creationflags(),
            startupinfo=_startupinfo(),
        )
        return float((result.stdout or "").strip() or 0)
    except Exception:
        return 0.0


def available_hw_encoders(ffmpeg_path: str | None = None) -> set[str]:
    found: set[str] = set()
    try:
        ffmpeg = ffmpeg_path or require_ffmpeg()
    except FFmpegError:
        return found
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=_creationflags(),
            startupinfo=_startupinfo(),
        )
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        for enc in ("h264_qsv", "h264_nvenc", "h264_amf"):
            if enc in text:
                found.add(enc)
    except Exception:
        pass
    return found


def _encoder_attempts(choice: str, ffmpeg_path: str, log_callback: LogCallback | None = None) -> list[tuple[str, str | None]]:
    choice = (choice or "CPU Fast (Reliable)").strip()
    available = available_hw_encoders(ffmpeg_path)
    attempts: list[tuple[str, str | None]] = []

    if choice == "Auto Hardware + CPU Fallback":
        for label, key in (
            ("Intel Quick Sync", "h264_qsv"),
            ("NVIDIA NVENC", "h264_nvenc"),
            ("AMD AMF", "h264_amf"),
        ):
            if key in available:
                attempts.append((label, key))
        attempts.append(("CPU Fast fallback", None))
        return attempts

    key = ENCODER_KEYS.get(choice)
    if key:
        if key in available:
            attempts.append((choice, key))
        else:
            if log_callback:
                log_callback(f"Selected hardware encoder not listed by FFmpeg: {choice}. Falling back to CPU Fast.")
        attempts.append(("CPU Fast fallback", None))
        return attempts

    return [("CPU Fast", None)]


def build_ffmpeg_cmd(
    *,
    ffmpeg_path: str,
    src: Path,
    temp: Path,
    duration: float,
    quality: str = "Balanced Fast",
    encoder_key: str | None = None,
) -> list[str]:
    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["Balanced Fast"])
    suffix = src.suffix.lower()
    webm_output = suffix == ".webm"

    # WebM should stay WebM; most hardware H.264 encoders cannot write a valid WebM output.
    if webm_output:
        encoder_key = None

    cmd = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(src),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-max_muxing_queue_size", "1024",
    ]

    crf = int(str(preset.get("crf", "28")))
    audio = str(preset.get("audio", "128k"))
    audio_kbps = parse_audio_kbps(audio)

    if webm_output:
        cmd += ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "6", "-row-mt", "1"]
    elif encoder_key == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "fast", "-pix_fmt", "yuv420p"]
    elif encoder_key == "h264_qsv":
        cmd += ["-c:v", "h264_qsv", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    elif encoder_key == "h264_amf":
        cmd += ["-c:v", "h264_amf", "-quality", "speed", "-usage", "transcoding", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "libx264", "-preset", str(preset.get("preset", "veryfast")), "-pix_fmt", "yuv420p", "-threads", "0"]

    if suffix in {".mp4", ".mov", ".m4v"}:
        cmd += ["-movflags", "+faststart"]

    if webm_output:
        vp9_crf = min(50, max(18, crf + 8))
        cmd += ["-crf", str(vp9_crf), "-b:v", "0"]
    elif encoder_key == "h264_nvenc":
        cmd += ["-rc", "vbr", "-cq:v", str(crf), "-b:v", "0"]
    elif encoder_key == "h264_qsv":
        cmd += ["-global_quality", str(crf)]
    elif encoder_key == "h264_amf":
        qp_b = str(min(51, crf + 2))
        cmd += ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-qp_b", qp_b]
    else:
        cmd += ["-crf", str(crf)]

    if webm_output:
        cmd += ["-c:a", "libopus", "-b:a", audio]
    else:
        cmd += ["-c:a", "aac", "-b:a", audio]

    cmd += ["-progress", "pipe:1", "-nostats", str(temp)]
    return cmd


def _run_ffmpeg_process(
    *,
    cmd: list[str],
    duration: float,
    src_name: str,
    progress_callback: ProgressCallback | None,
    log_callback: LogCallback | None,
    cancel_callback: CancelCallback | None,
) -> tuple[int, list[str]]:
    ffmpeg_output_tail: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        universal_newlines=True,
        bufsize=1,
        creationflags=_creationflags(),
        startupinfo=_startupinfo(),
    )
    if progress_callback:
        progress_callback(1, f"Compression started: {src_name}")
    if log_callback:
        log_callback(f"FFmpeg compression started for: {src_name}")
    last_progress = 0.0

    if proc.stdout:
        for line in proc.stdout:
            if cancel_callback and cancel_callback():
                try:
                    proc.terminate()
                except Exception:
                    pass
                raise CompressionCancelled("Compression cancelled.")
            line = line.strip()
            if not line:
                continue
            if line.startswith("out_time_ms=") and duration > 0:
                try:
                    out_ms = int(line.split("=", 1)[1])
                    pct = max(1.0, min(99.0, (out_ms / 1_000_000.0) / duration * 100))
                    if pct - last_progress >= 0.5:
                        if progress_callback:
                            progress_callback(int(pct), f"Compressing {src_name}: {pct:.0f}%")
                        last_progress = pct
                except Exception:
                    pass
            elif line == "progress=end":
                if progress_callback:
                    progress_callback(100, f"Compression finishing: {src_name}")
            elif not line.startswith((
                "frame=", "fps=", "stream_", "bitrate=", "total_size=",
                "out_time_us=", "out_time_ms=", "out_time=", "dup_frames=",
                "drop_frames=", "speed=", "progress="
            )):
                ffmpeg_output_tail.append(line)
                ffmpeg_output_tail = ffmpeg_output_tail[-8:]

    try:
        return_code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return_code = proc.wait()
    return return_code, ffmpeg_output_tail


def compress_video_file(
    video_path: str | Path,
    *,
    quality: str = "Balanced Fast",
    encoder: str = "CPU Fast (Reliable)",
    overwrite_only_smaller: bool = True,
    keep_failed_temp: bool = False,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> CompressionResult:
    """Compress one rendered video in-place using safe temp + backup replacement.

    If the compressed output is not smaller and overwrite_only_smaller is True,
    the original file is kept and the result is reported as a safe skip.
    """
    ffmpeg_path = require_ffmpeg()
    src = Path(video_path)
    if not src.exists():
        raise FileNotFoundError(f"Source video missing: {src}")
    if src.suffix.lower() not in VIDEO_EXTS:
        raise ValueError(f"Unsupported video type for compression: {src.name}")

    temp = _temp_path_for(src)
    backup = _backup_path_for(src)
    for cleanup in (temp, backup):
        if cleanup.exists():
            try:
                cleanup.unlink()
            except Exception as exc:
                raise RuntimeError(f"Could not remove old temp/backup file: {cleanup.name}") from exc

    original_size = src.stat().st_size
    min_valid_output_size = min(256 * 1024, max(4096, int(original_size * 0.05)))
    duration = get_duration(str(src))
    attempts = _encoder_attempts(encoder, ffmpeg_path, log_callback)
    if src.suffix.lower() == ".webm":
        attempts = [("WebM Fast VP9", None)]

    last_failure = ""
    compressed_size = 0
    used_label = attempts[-1][0] if attempts else "CPU Fast"

    for attempt_num, (label, encoder_key) in enumerate(attempts, 1):
        if cancel_callback and cancel_callback():
            raise CompressionCancelled("Compression cancelled.")
        if temp.exists():
            try:
                temp.unlink()
            except Exception as exc:
                raise RuntimeError(f"Could not remove temp before retry: {temp.name}") from exc
        if log_callback:
            log_callback(f"Compressing: {src.name} | Encoder: {label}")
        cmd = build_ffmpeg_cmd(
            ffmpeg_path=ffmpeg_path,
            src=src,
            temp=temp,
            duration=duration,
            quality=quality,
            encoder_key=encoder_key,
        )
        try:
            return_code, tail = _run_ffmpeg_process(
                cmd=cmd,
                duration=duration,
                src_name=src.name,
                progress_callback=progress_callback,
                log_callback=log_callback,
                cancel_callback=cancel_callback,
            )
        except CompressionCancelled:
            if temp.exists() and not keep_failed_temp:
                try:
                    temp.unlink()
                except Exception:
                    pass
            raise
        except Exception as exc:
            return_code, tail = 1, [str(exc)]

        if return_code == 0 and temp.exists() and temp.stat().st_size >= min_valid_output_size:
            compressed_size = temp.stat().st_size
            used_label = label
            break

        if temp.exists() and not keep_failed_temp:
            try:
                temp.unlink()
            except Exception:
                pass
        last_failure = "FFmpeg failed" + (": " + " | ".join(tail[-5:]) if tail else "")
        if attempt_num < len(attempts) and log_callback:
            log_callback(f"{label} failed. Retrying with {attempts[attempt_num][0]}...")
    else:
        raise RuntimeError(last_failure or "FFmpeg failed")

    if not temp.exists() or compressed_size < min_valid_output_size:
        if temp.exists() and not keep_failed_temp:
            try:
                temp.unlink()
            except Exception:
                pass
        raise RuntimeError("Output file is missing or too small")

    if overwrite_only_smaller and compressed_size >= original_size:
        if not keep_failed_temp:
            try:
                temp.unlink()
            except Exception:
                pass
        message = f"Skipped because output is not smaller ({fmt_size(compressed_size)} >= {fmt_size(original_size)})"
        if progress_callback:
            progress_callback(100, message)
        return CompressionResult(
            success=True,
            compressed=False,
            skipped=True,
            source_path=str(src),
            original_size=original_size,
            final_size=original_size,
            message=message,
            used_encoder=used_label,
        )

    # Verify duration if ffprobe is available. Keep tolerance broad because trimming/metadata can differ slightly.
    if _ffprobe_path():
        out_dur = get_duration(str(temp))
        if duration > 2 and out_dur > 0:
            if abs(out_dur - duration) > max(3.0, duration * 0.05):
                if not keep_failed_temp:
                    try:
                        temp.unlink()
                    except Exception:
                        pass
                raise RuntimeError(f"Verification failed: duration changed too much ({fmt_time(duration)} -> {fmt_time(out_dur)})")

    try:
        src.rename(backup)
        temp.rename(src)
        backup.unlink()
    except Exception as exc:
        try:
            if backup.exists() and not src.exists():
                backup.rename(src)
        except Exception:
            pass
        raise RuntimeError(f"Replace failed, original recovery attempted: {exc}") from exc

    saved = original_size - compressed_size
    message = f"{fmt_size(original_size)} -> {fmt_size(compressed_size)} | saved {fmt_size(saved)} | {used_label}"
    if progress_callback:
        progress_callback(100, f"Compression complete: {message}")
    return CompressionResult(
        success=True,
        compressed=True,
        skipped=False,
        source_path=str(src),
        original_size=original_size,
        final_size=compressed_size,
        message=message,
        used_encoder=used_label,
    )

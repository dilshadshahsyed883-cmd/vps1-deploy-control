"""FFmpeg helpers."""
from __future__ import annotations

import os
import sys
import shutil
import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def _candidate_names(name: str) -> list[str]:
    if os.name == "nt":
        return [name + ".exe", name]
    return [name]


def find_tool(name: str) -> str | None:
    """Find ffmpeg/ffprobe in PATH, beside the exe, or local ./ffmpeg/bin."""
    found = shutil.which(name)
    if found:
        return found

    source_base = Path(__file__).resolve().parent.parent
    search_bases = [source_base, Path.cwd()]
    if getattr(sys, "frozen", False):
        search_bases.insert(0, Path(sys.executable).resolve().parent)

    rel_folders = (
        Path("ffmpeg") / "bin",
        Path("bin"),
        Path("tools") / "ffmpeg" / "bin",
        Path("."),
    )
    for root in search_bases:
        for rel in rel_folders:
            folder = root / rel
            for candidate in _candidate_names(name):
                path = folder / candidate
                if path.exists():
                    return str(path)
    return None


def require_ffmpeg() -> str:
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise FFmpegError(
            "FFmpeg was not found. Install FFmpeg and add it to PATH, "
            "or place ffmpeg.exe inside this app's ffmpeg/bin folder."
        )
    return ffmpeg


def available_video_encoders() -> set[str]:
    """Return encoder names reported by the installed FFmpeg build."""
    ffmpeg = require_ffmpeg()
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=12)
    except Exception:
        return set()
    if result.returncode != 0:
        return set()
    encoders: set[str] = set()
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            encoders.add(parts[1].strip())
    return encoders


def resolve_video_encoder(requested: str | None) -> str:
    """Choose a supported video encoder.

    This keeps CPU x264 as the safe fallback while allowing users with NVIDIA,
    Intel, or AMD hardware to render faster through FFmpeg hardware encoders.
    """
    requested = (requested or "cpu_x264").strip().lower()
    aliases = {
        "cpu": "cpu_x264",
        "libx264": "cpu_x264",
        "x264": "cpu_x264",
        "nvidia": "nvidia_nvenc",
        "nvenc": "nvidia_nvenc",
        "intel": "intel_qsv",
        "qsv": "intel_qsv",
        "amd": "amd_amf",
        "amf": "amd_amf",
    }
    requested = aliases.get(requested, requested)
    encoders = available_video_encoders()
    wanted = {
        "nvidia_nvenc": "h264_nvenc",
        "intel_qsv": "h264_qsv",
        "amd_amf": "h264_amf",
    }
    if requested == "cpu_x264":
        return "libx264"
    if requested in wanted:
        codec = wanted[requested]
        if codec not in encoders:
            raise FFmpegError(f"Requested GPU encoder '{codec}' is not available in this FFmpeg build. Choose CPU x264 or install a full FFmpeg build with that encoder.")
        return codec
    for codec in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if codec in encoders:
            return codec
    return "libx264"


def _video_encoder_args(codec: str, crf: int, preset: str) -> list[str]:
    """Build codec-specific FFmpeg args for H.264 output."""
    crf = int(max(14, min(35, int(crf or 20))))
    preset = (preset or "veryfast").strip()
    if codec == "h264_nvenc":
        # NVENC uses constant quality rather than x264 CRF.
        return ["-c:v", codec, "-preset", "fast", "-cq", str(crf), "-b:v", "0", "-pix_fmt", "yuv420p"]
    if codec == "h264_qsv":
        return ["-c:v", codec, "-global_quality", str(crf), "-look_ahead", "0", "-pix_fmt", "yuv420p"]
    if codec == "h264_amf":
        return ["-c:v", codec, "-quality", "speed", "-qp_i", str(crf), "-qp_p", str(crf), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-threads", "0"]


def get_audio_duration(audio_path: str) -> float:
    """Return audio duration in seconds using ffprobe."""
    if not audio_path:
        return 0.0
    path = Path(audio_path)
    if not path.exists():
        raise FFmpegError(f"Audio file not found: {audio_path}")

    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        # ffmpeg fallback is less elegant, so we require ffprobe for accuracy.
        raise FFmpegError(
            "FFprobe was not found. It normally comes with FFmpeg. "
            "Add ffprobe.exe to PATH or ffmpeg/bin."
        )

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FFmpegError(result.stderr.strip() or "Could not read audio duration.")
    try:
        return max(0.1, float(result.stdout.strip()))
    except ValueError as exc:
        raise FFmpegError("Could not parse audio duration.") from exc



def _atempo_filter(speed: float) -> str:
    """Build a safe FFmpeg atempo chain for voice speed changes."""
    speed = max(0.25, min(4.0, float(speed or 1.0)))
    factors: list[float] = []
    while speed > 2.0:
        factors.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        factors.append(0.5)
        speed /= 0.5
    factors.append(speed)
    return ",".join(f"atempo={factor:.4f}" for factor in factors)


def _audio_filter(volume: float, speed: float) -> str | None:
    filters: list[str] = []
    volume = max(0.0, min(3.0, float(volume or 1.0)))
    speed = max(0.25, min(4.0, float(speed or 1.0)))
    if abs(volume - 1.0) > 0.001:
        filters.append(f"volume={volume:.4f}")
    if abs(speed - 1.0) > 0.001:
        filters.append(_atempo_filter(speed))
    return ",".join(filters) if filters else None

def start_ffmpeg_rawvideo(
    *,
    width: int,
    height: int,
    fps: int,
    output_path: str,
    audio_path: str | None = None,
    crf: int = 20,
    preset: str = "medium",
    audio_volume: float = 1.0,
    audio_speed: float = 1.0,
    video_encoder: str = "cpu_x264",
) -> subprocess.Popen:
    """Start ffmpeg process receiving RGB24 frames on stdin."""
    ffmpeg = require_ffmpeg()
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
    ]

    if audio_path:
        cmd.extend(["-i", audio_path, "-map", "0:v:0", "-map", "1:a:0"])

    resolved_encoder = resolve_video_encoder(video_encoder)
    cmd.extend(_video_encoder_args(resolved_encoder, crf, preset))

    if audio_path:
        audio_filter = _audio_filter(audio_volume, audio_speed)
        if audio_filter:
            cmd.extend(["-filter:a", audio_filter])
        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-shortest"])

    cmd.extend(["-movflags", "+faststart", output_path])

    # Use an unbuffered stdin pipe. On Windows, very large buffered writes to
    # FFmpeg can raise OSError(22, "Invalid argument") or hide the real FFmpeg
    # encoder error until the GUI crashes. Renderer writes frames in smaller
    # chunks, so keeping this pipe unbuffered makes that protection effective.
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=False,
        bufsize=0,
    )

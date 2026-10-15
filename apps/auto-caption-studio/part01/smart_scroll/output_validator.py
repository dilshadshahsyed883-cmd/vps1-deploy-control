"""Lightweight final media validation using ffprobe."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .ffmpeg_utils import FFmpegError, find_tool


@dataclass(frozen=True)
class ValidationResult:
    path: str
    duration: float
    size_bytes: int
    has_video: bool
    has_audio: bool


def validate_video_file(
    path: str | Path,
    *,
    expected_duration: float | None = None,
    require_audio: bool = False,
    min_size_bytes: int = 4096,
) -> ValidationResult:
    media = Path(path)
    if not media.exists() or not media.is_file():
        raise FFmpegError(f"Rendered output was not created: {media}")
    size = media.stat().st_size
    if size < max(1, int(min_size_bytes)):
        raise FFmpegError(f"Rendered output is too small or incomplete ({size} bytes): {media.name}")

    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        raise FFmpegError("FFprobe is required to verify the completed video.")
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json",
        str(media),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"Output verification timed out: {media.name}") from exc
    if result.returncode != 0:
        raise FFmpegError(result.stderr.strip() or f"FFprobe could not read {media.name}")
    try:
        data = json.loads(result.stdout or "{}")
        duration = float((data.get("format") or {}).get("duration") or 0.0)
        codec_types = {str(item.get("codec_type") or "") for item in (data.get("streams") or [])}
    except Exception as exc:
        raise FFmpegError(f"Could not parse verification result for {media.name}") from exc

    has_video = "video" in codec_types
    has_audio = "audio" in codec_types
    if not has_video:
        raise FFmpegError(f"Completed file has no video stream: {media.name}")
    if require_audio and not has_audio:
        raise FFmpegError(f"Completed file has no audio stream: {media.name}")
    if duration <= 0.1:
        raise FFmpegError(f"Completed file has an invalid duration: {media.name}")

    if expected_duration and expected_duration > 0:
        tolerance = max(2.0, float(expected_duration) * 0.05)
        if abs(duration - float(expected_duration)) > tolerance:
            raise FFmpegError(
                f"Completed duration {duration:.1f}s differs from expected {float(expected_duration):.1f}s "
                f"by more than {tolerance:.1f}s."
            )
    return ValidationResult(str(media), duration, size, has_video, has_audio)

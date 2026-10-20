"""Bounded technical media inspection and source-provenance helpers.

Technical metadata is deterministic ffprobe data, not invented AI tagging.
Probe failures return unknown fields; import and existing source history are retained.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TECHNICAL_FIELDS = ("codec", "width", "height", "fps", "orientation", "has_audio")


def classify_orientation(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "unknown"
    ratio = width / height
    if ratio >= 1.12:
        return "landscape"
    if ratio <= 0.89:
        return "portrait"
    return "square"


def safe_origin(source: str, locator: str) -> str:
    """Keep a useful origin without storing URL query credentials or fragments."""
    if source == "local":
        return ""
    if source == "drive":
        path = locator.strip().lstrip("/")
        return path[:500] if not any(x in path for x in ("..", "\\", ":")) else ""
    if source == "youtube":
        url = urlsplit(locator)
        if url.scheme != "https" or url.hostname not in (
            "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"
        ):
            return ""
        if url.hostname == "youtu.be":
            video_id = url.path.strip("/")
        elif url.path == "/watch":
            video_id = parse_qs(url.query).get("v", [""])[0]
        elif url.path.startswith("/shorts/") or url.path.startswith("/live/"):
            video_id = url.path.split("/")[2]
        else:
            video_id = ""
        if not video_id or len(video_id) > 20 or not all(x.isalnum() or x in "-_" for x in video_id):
            return ""
        return "youtube:" + video_id
    return ""


def probe_video(path: Path) -> dict:
    default = {"codec": "", "width": 0, "height": 0, "fps": 0.0,
               "orientation": "unknown", "has_audio": False}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=25
        )
        streams = json.loads(result.stdout).get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if not video:
            return default
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        # Protect DB from bogus input and unexpected divisors.
        ratio = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
        n, sep, d = str(ratio).partition("/")
        fps = float(n) / float(d or 1) if float(d or 1) else 0.0
        if not (0 < fps <= 1000):
            fps = 0.0
        return {"codec": str(video.get("codec_name") or "")[:40],
                "width": max(0, width), "height": max(0, height),
                "fps": round(fps, 3),
                "orientation": classify_orientation(width, height),
                "has_audio": any(s.get("codec_type") == "audio" for s in streams)}
    except (OSError, ValueError, ZeroDivisionError, subprocess.SubprocessError, json.JSONDecodeError):
        return default

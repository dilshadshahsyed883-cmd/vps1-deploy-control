"""Read-only video path validation extracted from Auto Stream.

No media changes, deletion, data access, or network access occur on import.
"""
from __future__ import annotations
from pathlib import Path

VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v"})

def resolve_relative_video(media_root: str | Path, filename: str) -> Path:
    """Resolve a video in a caller-owned media root with legacy validation."""
    if not filename or filename.startswith("/") or "\\" in filename:
        raise ValueError("media_path must be relative to Short Stream media")
    root = Path(media_root)
    base = root.resolve()
    path = root / filename
    if any(p.is_symlink() for p in [path, *path.parents]
           if p != base and base in p.parents):
        raise ValueError("Media symlinks are not accepted")
    target = path.resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise ValueError("Media file is absent or outside Auto Stream sources")
    if target.suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError("Unsupported video format")
    return target

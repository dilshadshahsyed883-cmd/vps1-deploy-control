"""Account-scoped Video Merger handoff: list and delete only verified uploads."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
from .config import DATA_ROOT

ROOT = Path(os.environ.get("COPYRIGHT_MEDIA_ROOT", str(DATA_ROOT / "copyright_handoff")))
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov"})


def owner_root(owner: str) -> Path:
    name = str(owner or "").strip().lower()
    if not name:
        raise ValueError("Login is required for Copyright Uploader folder")
    return ROOT / hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]


def valid_name(filename: str) -> bool:
    value = str(filename or "")
    return (bool(value) and value == Path(value).name and not value.startswith(".")
            and Path(value).suffix.lower() in VIDEO_EXTENSIONS)


def available_videos(owner: str, claimed: set[str] | None = None) -> list[str]:
    folder = owner_root(owner)
    if ROOT.is_symlink() or folder.is_symlink() or not folder.is_dir():
        return []
    blocked = {name.casefold() for name in (claimed or set())}
    result = []
    for path in folder.iterdir():
        if (valid_name(path.name) and path.name.casefold() not in blocked
                and not path.is_symlink() and path.is_file()
                and path.stat().st_size > 0):
            result.append(path.name)
    return sorted(result, key=str.casefold)[:500]


def delete_verified_source(owner: str, filename: str, expected_sha256: str) -> bool:
    """Delete a handoff copy only after caller has independently verified YouTube.

    Never delete Video Merger's separately retained original. A renamed/changed
    file, unsafe link, owner mismatch or wrong checksum fails closed. Missing
    source is idempotent (e.g. completed cleanup interrupted before DB commit).
    """
    if not valid_name(filename) or not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("Invalid verified handoff source identity")
    if ROOT.is_symlink():
        raise RuntimeError("Copyright handoff root must not be a symlink")
    folder = owner_root(owner)
    if folder.is_symlink():
        raise RuntimeError("Copyright owner folder must not be a symlink")
    source = folder / filename
    if source.is_symlink():
        raise RuntimeError("Copyright handoff file changed to symlink")
    if not source.exists():
        return False
    if not source.is_file() or source.resolve().parent != folder.resolve():
        raise RuntimeError("Copyright handoff source escaped owner folder")
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.stat(source, follow_symlinks=False)
        if (not os.path.isfile(source) or before.st_ino != after.st_ino
                or before.st_dev != after.st_dev or before.st_size != after.st_size
                or digest.hexdigest() != expected_sha256.lower()):
            raise RuntimeError("Copyright handoff original changed; refuse deletion")
        source.unlink()
        if source.exists() or source.is_symlink():
            raise RuntimeError("Copyright handoff original still exists after cleanup")
        return True
    finally:
        os.close(fd)

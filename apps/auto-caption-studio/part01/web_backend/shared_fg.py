from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re

from .config import app_root
from .database import connect


FG_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def shared_fg_root() -> Path:
    root = app_root() / "shared" / "fg"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def normalize_fg_name(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/").strip("/")
    path = PurePosixPath(text)
    if not text or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Invalid FG filename")
    if path.suffix.lower() not in FG_EXTENSIONS:
        raise ValueError("Unsupported FG image extension")
    # New assets use a flat filename. Existing category/filename records remain
    # resolvable so live legacy batches are never rewritten or broken.
    return path.as_posix()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_shared_fg(name: str, expected_sha256: str = "") -> Path:
    normalized = normalize_fg_name(name)
    expected = str(expected_sha256 or "").strip().lower()
    if expected and not SHA256_RE.fullmatch(expected):
        raise RuntimeError("Invalid frozen FG SHA-256")
    with connect() as db:
        row = db.execute("SELECT * FROM shared_fg_assets WHERE name=?", (normalized,)).fetchone()
    if row is None or str(row["status"]) != "available":
        raise RuntimeError(f"Shared FG is unavailable: {normalized}")
    root = shared_fg_root()
    target = (root / str(row["relative_path"])).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Shared FG path escaped its protected root") from exc
    if not target.is_file():
        raise RuntimeError(f"Shared FG file is missing: {normalized}")
    stored_sha = str(row["sha256"] or "").lower()
    if expected and stored_sha != expected:
        raise RuntimeError(f"Shared FG database checksum mismatch: {normalized}")
    actual_sha = file_sha256(target)
    if actual_sha != stored_sha or (expected and actual_sha != expected):
        raise RuntimeError(f"Shared FG file checksum mismatch: {normalized}")
    return target

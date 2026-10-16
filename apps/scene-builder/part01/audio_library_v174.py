from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

PERMANENT_ROOT = Path(os.getenv("SCENE_PERMANENT_ROOT", "/var/lib/scene-builder/permanent")).resolve()
MUSIC_ROOT = PERMANENT_ROOT / "music"
SFX_ROOT = PERMANENT_ROOT / "sfx"
MANIFEST_PATH = PERMANENT_ROOT / "library.json"
SUPPORTED_AUDIO = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".aiff", ".aif"}
_LIBRARY_LOCK = threading.RLock()


def ensure_library() -> None:
    MUSIC_ROOT.mkdir(parents=True, exist_ok=True)
    SFX_ROOT.mkdir(parents=True, exist_ok=True)
    if not MANIFEST_PATH.exists():
        save_manifest({"schema": 1, "music": [], "sfx": []})


def load_manifest() -> dict[str, Any]:
    ensure_library()
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        data = {"schema": 1, "music": [], "sfx": []}
    data.setdefault("schema", 1)
    data.setdefault("music", [])
    data.setdefault("sfx", [])
    return data


def save_manifest(data: dict[str, Any]) -> None:
    PERMANENT_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "music": list(data.get("music", [])), "sfx": list(data.get("sfx", []))}
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(MANIFEST_PATH)


def sanitize_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(name).name).strip(" .")
    return stem[:180] or "audio"


def target_root(kind: str) -> Path:
    if kind == "music":
        return MUSIC_ROOT
    if kind == "sfx":
        return SFX_ROOT
    raise ValueError("Invalid audio library kind")


def list_assets(kind: str) -> list[dict[str, Any]]:
    manifest = load_manifest()
    root = target_root(kind)
    rows = []
    dirty = False
    for raw in manifest.get(kind, []):
        if not isinstance(raw, dict):
            dirty = True
            continue
        row = dict(raw)
        path = root / str(row.get("stored_filename") or "")
        if not path.is_file():
            dirty = True
            continue
        row["exists"] = True
        row["size_bytes"] = int(path.stat().st_size)
        rows.append(row)
    if dirty:
        manifest[kind] = rows
        save_manifest(manifest)
    return sorted(rows, key=lambda x: (str(x.get("category") or ""), str(x.get("display_name") or "").casefold()))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def create_asset_record(kind: str, source_path: Path, original_name: str, *, category: str, duration: float, created_at: str) -> dict[str, Any]:
    suffix = source_path.suffix.lower()
    if suffix not in SUPPORTED_AUDIO:
        raise ValueError("Unsupported audio format")
    digest = _sha256_file(source_path)
    with _LIBRARY_LOCK:
        manifest = load_manifest()
        for existing in manifest.get(kind, []):
            if str(existing.get("sha256") or "") == digest:
                return dict(existing)
        asset_id = ("music_" if kind == "music" else "sfx_") + secrets.token_hex(8)
        clean = sanitize_name(original_name)
        stored = f"{asset_id}__{clean}"
        target = target_root(kind) / stored
        source_path.replace(target)
        row = {
            "asset_id": asset_id,
            "kind": kind,
            "filename": clean,
            "stored_filename": stored,
            "display_name": Path(clean).stem,
            "category": re.sub(r"[^A-Za-z0-9 _-]+", "", str(category or "custom")).strip()[:80] or "custom",
            "tags": [],
            "duration": round(max(0.0, float(duration or 0.0)), 3),
            "size_bytes": int(target.stat().st_size),
            "sha256": digest,
            "created_at": created_at,
        }
        manifest[kind] = [*manifest.get(kind, []), row]
        save_manifest(manifest)
        return row



def update_asset(kind: str, asset_id: str, *, display_name: str | None = None, category: str | None = None, tags: list[str] | None = None) -> dict[str, Any]:
    with _LIBRARY_LOCK:
        manifest = load_manifest()
        found = None
        for row in manifest.get(kind, []):
            if str(row.get("asset_id")) != asset_id:
                continue
            if display_name is not None:
                row["display_name"] = re.sub(r"[\r\n\t]+", " ", display_name).strip()[:120] or row.get("display_name") or asset_id
            if category is not None:
                row["category"] = re.sub(r"[^A-Za-z0-9 _-]+", "", category).strip()[:80] or "custom"
            if tags is not None:
                row["tags"] = [re.sub(r"[^A-Za-z0-9 _-]+", "", x).strip()[:50] for x in tags if re.sub(r"[^A-Za-z0-9 _-]+", "", x).strip()][:30]
            found = dict(row)
            break
        if found is None:
            raise KeyError(asset_id)
        save_manifest(manifest)
        return found



def delete_asset(kind: str, asset_id: str) -> None:
    with _LIBRARY_LOCK:
        manifest = load_manifest()
        rows = list(manifest.get(kind, []))
        target = next((dict(x) for x in rows if str(x.get("asset_id")) == asset_id), None)
        if not target:
            raise KeyError(asset_id)
        path = target_root(kind) / str(target.get("stored_filename") or "")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        manifest[kind] = [x for x in rows if str(x.get("asset_id")) != asset_id]
        save_manifest(manifest)



def asset_path(kind: str, asset_id: str) -> Path:
    for row in list_assets(kind):
        if str(row.get("asset_id")) == asset_id:
            path = target_root(kind) / str(row.get("stored_filename") or "")
            if path.is_file():
                return path
    raise KeyError(asset_id)

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

APP_ROOT = Path(os.environ.get(
    "AUTO_CAPTION_APP_ROOT",
    "/home/ubuntu/apps/auto_caption_studio",
))
SHARED_LIBRARY = Path(os.environ.get(
    "AUTO_CAPTION_PEXELS_LIBRARY",
    str(APP_ROOT / "data" / "pexels_background_library"),
))
CONFIG_FILE = Path(os.environ.get(
    "AUTO_CAPTION_PEXELS_CONFIG",
    str(APP_ROOT / "data" / "config" / "shared_pexels.json"),
))
KEYS_FILE = Path(os.environ.get(
    "AUTO_CAPTION_PEXELS_KEYS_FILE",
    str(APP_ROOT / "data" / "config" / "pexels_keys.json"),
))
_KEY_LOCK = threading.Lock()
_KEY_INDEX = 0


def _normalize_keywords(value: Any) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for raw in str(value or "").replace("\n", ",").split(","):
        item = " ".join(raw.strip().split())
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            parts.append(item)
    return ", ".join(parts)


def _load_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_config(data: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(temp, 0o600)
    temp.replace(CONFIG_FILE)


def apply_shared_pexels_settings(settings: dict[str, Any]) -> dict[str, Any]:
    clean = dict(settings or {})
    config = _load_config()
    metadata = clean.get("metadata") if isinstance(clean.get("metadata"), dict) else {}
    is_web_flow = bool(metadata.get("web_flow_v1"))

    submitted_keywords = _normalize_keywords(clean.get("pexels_query"))
    stored_keywords = _normalize_keywords(config.get("keywords"))
    keywords = submitted_keywords or stored_keywords

    preset_library_raw = str(metadata.get("web_preset_pexels_library") or "").strip() if is_web_flow else ""
    preset_library = Path(preset_library_raw).expanduser().resolve() if preset_library_raw else None
    safe_data_root = (APP_ROOT / "data").resolve()
    if preset_library is not None and preset_library != safe_data_root and safe_data_root not in preset_library.parents:
        preset_library = None

    # Preset TXT queries are local to that preset and must never overwrite the
    # shared/global keyword configuration. Legacy/global jobs keep old behavior.
    if submitted_keywords and submitted_keywords != stored_keywords and preset_library is None:
        config["keywords"] = submitted_keywords
        config["updated_from_job"] = True
        _save_config(config)

    active_library = preset_library or SHARED_LIBRARY
    active_library.mkdir(parents=True, exist_ok=True)
    clean["pexels_use_library"] = True
    clean["pexels_library_folder"] = str(active_library)
    clean["pexels_cache_paths"] = []
    # Desktop/job credentials are never trusted or persisted. The worker injects
    # one server-owned key only in memory immediately before rendering.
    clean["pexels_api_key"] = ""

    if is_web_flow:
        # New Web Flow may use images, videos, or both. Preserve the selected
        # preset controls instead of forcing the legacy images-only policy.
        use_images = bool(clean.get("use_image_backgrounds", True))
        use_videos = bool(clean.get("use_video_backgrounds", False))
        if not use_images and not use_videos:
            use_images = True
        clean["use_image_backgrounds"] = use_images
        clean["use_video_backgrounds"] = use_videos
        clean["pexels_media_type"] = (
            "mix" if use_images and use_videos else "videos" if use_videos else "images"
        )
        clean["pexels_auto_keywords"] = bool(clean.get("pexels_auto_keywords", True))
    else:
        # Preserve the exact proven legacy VPS policy.
        clean["pexels_auto_keywords"] = False
        clean["use_image_backgrounds"] = True
        clean["use_video_backgrounds"] = False
        clean["pexels_media_type"] = "images"

    if clean.get("pexels_enabled"):
        clean["pexels_download_enabled"] = True

    if keywords:
        clean["pexels_query"] = keywords

    return clean


def server_pexels_keys() -> list[str]:
    try:
        data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("keys", []) if isinstance(data, dict) else data
    values: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        key = str(item or "").strip()
        if key and key not in values:
            values.append(key)
    return values


def next_server_pexels_key() -> str:
    global _KEY_INDEX
    keys = server_pexels_keys()
    if not keys:
        return ""
    with _KEY_LOCK:
        value = keys[_KEY_INDEX % len(keys)]
        _KEY_INDEX = (_KEY_INDEX + 1) % len(keys)
    return value


def inject_server_pexels_key(settings: dict[str, Any]) -> dict[str, Any]:
    clean = dict(settings or {})
    clean["pexels_api_key"] = next_server_pexels_key() if clean.get("pexels_enabled") else ""
    return clean

"""Scene Builder-only visual-source selection for portable worker packages.

No SML access. Local media selections are bundled and verified inside the
worker's extracted project root; unavailable matches never silently enable a
Pexels lookup in library-only mode.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


VALID_MODES = {"library_only", "scene_first"}


def _keywords(scene: dict[str, Any]) -> list[str]:
    result = []
    for key in ("primary_keyword", "keyword", "query", "tag", "fallback_keyword", "visual_prompt", "text"):
        candidate = str(scene.get(key) or "").strip()
        if candidate and candidate.upper().startswith("SCENE_") is False:
            result.append(candidate)
    for key in ("fallback_keywords", "backup_keywords"):
        others = scene.get(key) or []
        if isinstance(others, list):
            result.extend(str(value).strip() for value in others if str(value).strip())
    return result


def _order(media_mix: str, index: int) -> tuple[str, str]:
    if media_mix == "image_heavy" or (media_mix == "alternate" and index % 2):
        return ("image", "video")
    return ("video", "image")


def _intro_plan(queries: list[dict[str, Any]], duration_seconds: float,
                min_seconds: float, max_seconds: float) -> list[dict[str, Any]]:
    duration = float(max(1, min(600, int(duration_seconds or 15))))
    minimum = max(0.25, min(10.0, float(min_seconds or 1.5)))
    maximum = max(minimum, min(15.0, float(max_seconds or 3.0)))
    count = max(1, int((duration + maximum - 1e-9) // maximum))
    if count * maximum < duration - 1e-6:
        count += 1
    while count > 1 and duration / count < minimum:
        count -= 1
    cursor = 0.0
    rows = []
    index = 0
    while cursor < duration - 0.001:
        remaining_slots = max(1, count - index)
        shot = min(maximum, max(0.05, (duration - cursor) / remaining_slots))
        row = queries[index % len(queries)]
        rows.append({"scene_code": f"INTRO_{index + 1:03d}",
                     "primary_keyword": str(row.get("query") or ""),
                     "fallback_keywords": row.get("backup_keywords") or []})
        cursor = min(duration, cursor + max(0.05, shot))
        index += 1
        if index > 500:
            raise ValueError("Intro planner exceeded 500 shots")
    return rows


def bundle_local_media(scene_plan: list[dict[str, Any]], intro_queries: list[dict[str, Any]],
                       assets_dir: Path, catalog: Any, *, priority: str,
                       media_mix: str, intro_enabled: bool = False,
                       intro_duration_seconds: float = 15,
                       intro_min_seconds: float = 1.5,
                       intro_max_seconds: float = 3.0,
                       allow_pexels: bool = True) -> dict[str, dict[str, Any]]:
    """Attach ranked, auto-approved Scene local assets without touching SML.

    For library_only require a local match for *every* narration and intro shot.
    For scene_first leave unmatched shots unapproved so normal Pexels resolves them.
    The caller includes this returned intro manifest in project metadata.
    """
    if priority not in VALID_MODES:
        raise ValueError("Invalid Scene Media Library priority")
    intro = []
    if intro_enabled:
        valid_queries = [row for row in intro_queries if isinstance(row, dict) and str(row.get("query") or "").strip()]
        if not valid_queries:
            raise ValueError("Opening Intro is enabled but no INTRO_QUERY was supplied")
        intro = _intro_plan(valid_queries, intro_duration_seconds, intro_min_seconds, intro_max_seconds)
    assets_dir.mkdir(parents=True, exist_ok=True)
    unique: set[str] = set()
    archived: set[str] = set()
    manifest: dict[str, dict[str, Any]] = {}
    unmatched: list[str] = []
    for index, (is_intro, scene) in enumerate([(False, s) for s in scene_plan] + [(True, s) for s in intro]):
        code = str(scene.get("scene_code") or ("INTRO" if is_intro else "SCENE") + f"_{index + 1:03d}")
        selection = None
        for excluded in (unique, set()):
            for keyword in _keywords(scene):
                for kind in _order(media_mix, index):
                    options = catalog.ranked({"keyword_used": keyword}, used=excluded, kind=kind)
                    if options:
                        selection = options[0]
                        break
                if selection:
                    break
            if selection:
                break
        if selection is None:
            if priority == "library_only" or not allow_pexels:
                unmatched.append(code)
            continue
        media_path, media = catalog.file(selection["id"])
        name = selection["id"] + media["extension"]
        packaged = "assets/scene_local/" + name
        if name not in archived:
            shutil.copy2(media_path, assets_dir / name)
            archived.add(name)
        unique.add(selection["id"])
        properties = {
            "media_path": packaged, "media_source_path": packaged,
            "media_provider": "scene_media_library",
            "provider_asset_id": selection["id"],
            "assigned_media_type": media["kind"],
            "review_approved": True, "review_locked": True,
            "pexels_enabled": bool(allow_pexels) and priority == "scene_first",
        }
        if is_intro:
            manifest[code] = properties
        else:
            scene.update(properties)
    if unmatched:
        reason = ("Scene Media Library Only" if priority == "library_only"
                  else "Scene Media Library First with Pexels disabled")
        raise ValueError(reason + ": no matching approved local media for " +
                         ", ".join(unmatched[:12]) +
                         (" and more scenes" if len(unmatched) > 12 else "") +
                         ". Add matching files to Scene Media Library or enable Pexels fallback.")
    return manifest

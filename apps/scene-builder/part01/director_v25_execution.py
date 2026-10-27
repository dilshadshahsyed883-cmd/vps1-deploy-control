"""Director V25 compiler: versioned TXT -> existing Scene Builder job contract.

This module only compiles a validated per-project execution plan. It changes no
global preset, media catalog, SML app, or Classic TXT parser.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
from typing import Any

from director_v25 import DirectorSpecError, parse_v25, preflight_v25

SUPPORTED_EFFECTS = {"none", "subtle_pulse", "soft_glow", "edge_vignette",
                     "gentle_shake", "cinematic_grain", "bloom"}


def _flag(value: str) -> bool:
    return str(value).strip().lower() == "true"


def _plain(text: str) -> str:
    return " ".join(str(text or "").split())


def _required_settings(sections: dict[str, dict[str, str]], preset: str) -> dict[str, Any]:
    p = sections["PROJECT"]
    if p["base_preset"] != preset:
        raise DirectorSpecError(
            f"[PROJECT].base_preset={p['base_preset']} conflicts with selected UI preset={preset}")
    o, a, c, l, t, intro = (sections[key] for key in
                           ("OUTPUT", "AUDIO", "CAPTIONS", "LOOK", "TIMING", "INTRO"))
    dimension = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080)}
    w, h = dimension[o["resolution"]]
    if o["aspect_ratio"] == "9:16":
        w, h = h, w
    elif o["aspect_ratio"] == "1:1":
        w = h = {"480p": 480, "720p": 720, "1080p": 1080}[o["resolution"]]
    exact_duration = float(intro["intro_duration_seconds"])
    if not exact_duration.is_integer():
        raise DirectorSpecError("[INTRO].intro_duration_seconds must be whole seconds")
    duration = int(exact_duration)
    if _flag(intro["intro_enabled"]) and (duration < 8 or duration > 24):
        raise DirectorSpecError("[INTRO]: 8 hook shots require 8-24 seconds, each <=3s")
    shot_seconds = duration / 8
    if _flag(intro["intro_enabled"]) and shot_seconds < 0.25:
        raise DirectorSpecError("[INTRO]: hook shot duration is too short")
    if t["narration_start"] == "manual":
        raise DirectorSpecError("Manual narration start needs an exact timestamp; use auto in V25")
    return {
        "width": w, "height": h, "fps": int(o["fps"]),
        "output_resolution": o["resolution"],
        "captions_enabled": _flag(c["captions_enabled"]),
        "caption_mode": _flag(c["captions_enabled"]),
        "caption_style": c["caption_style"],
        "caption_position": c["caption_position"],
        "caption_karaoke_enabled": c["caption_style"] in {
            "karaoke", "social_pop", "word_highlight", "hormozi_purple"},
        "voice_volume_percent": int(a["voice_volume_percent"]),
        "music_enabled": _flag(a["music_enabled"]),
        "lut_enabled": _flag(l["lut_enabled"]),
        "lut_pack": l["lut_pack"], "lut_preset": l["lut_preset"],
        "intro_enabled": _flag(intro["intro_enabled"]),
        "intro_duration_seconds": duration,
        "intro_media_min_seconds": shot_seconds,
        "intro_media_max_seconds": shot_seconds,
        # Director V25 controls all creative effects. Do NOT inherit the
        # selected UI preset's implicit polish, music, SFX, captions or LUT mix.
        "video_quality_preset": "original",
        "quality_crf": 20,  # encoder engineering baseline (not a creative effect)
        "voice_processing_preset": "off",
        "sfx_enabled": False,
        "environment_sfx_enabled": False,
        "transition_sfx_library_enabled": False,
        "first_two_minute_visual_audit_enabled": False,
        "capcut_mirror_enabled": False,
        "capcut_mirror_percent": 0,
        "capcut_zoom_level": 100,
        "capcut_noise_enabled": False,
        "capcut_noise_percent": 0,
        "capcut_animation_effect": "none",
        "capcut_animation_category": "combo",
        "capcut_animation_target": "both",
        "lut_mode": "manual" if _flag(l["lut_enabled"]) else "off",
        "lut_strength_percent": int(l.get("lut_strength_percent", "100") or 100),
        "lut_normalize_enabled": False,
        "lut_scene_aware_enabled": False,
        "lut_continuity_enabled": False,
        "caption_font": c.get("font", "dejavu_sans_bold") or "dejavu_sans_bold",
        "caption_text_color": c.get("text_color", "#ffffff") or "#ffffff",
        "caption_highlight_color": c.get("highlight_color", "#ffd54a") or "#ffd54a",
        "caption_outline_color": c.get("outline_color", "#000000") or "#000000",
        "caption_background_color": c.get("background_color", "#000000") or "#000000",
        "caption_background_opacity": int(c.get("background_opacity_percent", "55") or 0),
        "caption_font_size": int(c.get("font_size", "48") or 48),
        "caption_max_words": int(c.get("max_words", "3") or 3),
        "transition_engine_enabled": True,
        "transition_engine_mode": "scene_plan",
        "general_media_min_seconds": 0.05,
        "general_media_max_seconds": 3.0,
        "hook_media_min_seconds": 0.05,
        "hook_media_max_seconds": 3.0,
        "max_scene_seconds": 3.0,
        "narration_start_mode": "auto",
        "auto_clean_project_after_drive": False,
        "output_filename_requested": o["filename"],
    }


def compile_v25(text: str, catalog: Any, *, source_mode: str, library_enabled: bool,
                selected_preset: str, capabilities: dict[str, Any]) -> dict[str, Any]:
    """Compile and lock exact approved filename selections, not fuzzy keyword guesses."""
    preview = preflight_v25(text, catalog.list(limit=100000), source_mode,
                            library_enabled, capabilities)
    if preview["status"] != "VALIDATED_INTENT_ONLY":
        raise DirectorSpecError("Director preflight blocked: " + "; ".join(preview["errors"][:12]))
    parsed = parse_v25(text)
    sections = parsed["sections"]
    settings = _required_settings(sections, selected_preset)
    requested_name = sections["PROJECT"]["name"]
    if not requested_name or len(requested_name) > 80 or Path(requested_name).name != requested_name or not all(
        char.isalnum() or char in "._-" for char in requested_name):
        raise DirectorSpecError("[PROJECT].name must be a safe simple filename stem")
    if Path(settings["output_filename_requested"]).stem != requested_name:
        raise DirectorSpecError("[OUTPUT].filename must equal [PROJECT].name + .mp4 in V25")
    resolved = {row["shot"]: row for row in preview["resolved_assets"]}
    if any(row["source"] not in {"scene_media_library", "pexels_pending"}
           for row in resolved.values()):
        raise DirectorSpecError("Unsupported media source in Director preflight")
    scene_shots: list[dict[str, Any]] = []
    combined_narration: list[str] = []
    for i in range(1, parsed["scenes"] + 1):
        section = f"SCENE_{i:03d}"
        master_narration = _plain(sections[section]["narration"])
        authored = [key for key in parsed["scene_shots"] if key.startswith(section + ".SHOT_")]
        if len(authored) > 1:
            pieces = [_plain(sections[key].get("narration", "")) for key in authored]
            if not all(pieces) or _plain(" ".join(pieces)) != master_narration:
                raise DirectorSpecError(
                    f"[{section}]: multiple shots require exact narration per shot; "
                    "joined shot narration must reconstruct scene narration")
        else:
            pieces = [master_narration]
        combined_narration.append(master_narration)
        for key, spoken in zip(authored, pieces):
            shot, media = sections[key], resolved[key]
            effect = shot.get("effect", "none")
            if effect not in SUPPORTED_EFFECTS:
                raise DirectorSpecError(f"[{key}].effect unsupported: {effect}")
            if shot.get("visual_direction", "none") not in {"none", ""}:
                visual = shot["visual_direction"]
            else:
                visual = sections[section].get("visual_direction", "auto")
            keyword = shot.get("pexels_primary_keyword", "")
            backups = [b.strip() for b in shot.get("pexels_backup_keywords", "").split(",") if b.strip()]
            if media["source"] == "pexels_pending" and not keyword:
                raise DirectorSpecError(f"[{key}]: Pexels fallback requires a primary keyword")
            if media["source"] == "scene_media_library":
                keyword = ""
                backups = []
            capcut = shot.get("animation", "none")
            scene = {
                "scene_code": f"SCENE_{len(scene_shots) + 1:03d}",
                "director_v25_shot": key,
                "outer_ai_source": "director_v25",
                "text": spoken, "visual_prompt": spoken,
                "media_type": shot["media_type"],
                "primary_keyword": keyword,
                "keyword": keyword,
                "fallback_keywords": backups, "backup_keywords": backups,
                "visual_direction": visual,
                "motion": shot.get("motion", "none"),
                "motion_intensity": float(shot.get("motion_intensity", "0") or 0),
                "capcut_animation_category": shot.get("animation_category", "combo"),
                "capcut_animation_effect": capcut,
                "capcut_animation_duration": float(shot.get("animation_duration", "2.5") or 2.5),
                "capcut_animation_intensity": int(shot.get("animation_intensity", "65") or 65),
                "capcut_animation_target": "images" if shot["media_type"] == "image" else "videos",
                "transition_out": shot.get("transition_out", "none"),
                "transition_duration_ms": int(round(float(shot.get("transition_duration", "0") or 0) * 1000)),
                "filter": "none", "filter_intensity": 0.0,
                "effect": effect,
                "effect_intensity": float(shot.get("effect_intensity", "0") or 0),
                "overlay": "none", "overlay_intensity": 0.0,
                "transition_sfx": "none", "sfx_volume": 0.0,
                "allow_media_fallback": media["source"] == "pexels_pending",
                "pexels_enabled": media["source"] == "pexels_pending",
                "pexels_prefer_landscape": False,
                "director_v25_media_source": media["source"],
                "director_v25_media_filename": media["filename"],
                "director_v25_media_asset_id": media["asset_id"],
                "director_v25_max_seconds": 3.0,
                "review_approved": media["source"] == "scene_media_library",
                "review_locked": media["source"] == "scene_media_library",
            }
            scene_shots.append(scene)
    if not scene_shots:
        raise DirectorSpecError("Director plan has no narration shots")
    intro_rows: list[dict[str, Any]] = []
    intro_edit: dict[str, dict[str, Any]] = {}
    for n, key in enumerate(parsed["intro_shots"], 1):
        shot = sections[key]
        media = resolved.get(key)
        if settings["intro_enabled"] and media is None:
            raise DirectorSpecError(f"Missing intro asset plan: [{key}]")
        if media is None:
            continue
        keyword = shot.get("pexels_primary_keyword", "") if media["source"] == "pexels_pending" else ""
        effect = shot.get("effect", "none")
        if effect not in SUPPORTED_EFFECTS:
            raise DirectorSpecError(f"[{key}].effect unsupported: {effect}")
        intro_edit[f"INTRO_{n:03d}"] = {
            "director_v25_shot": key,
            "outer_ai_source": "director_v25",
            "capcut_animation_category": shot.get("animation_category", "combo"),
            "capcut_animation_effect": shot.get("animation", "none"),
            "capcut_animation_duration": float(shot.get("animation_duration", "2.5") or 2.5),
            "capcut_animation_intensity": int(shot.get("animation_intensity", "65") or 65),
            "capcut_animation_target": "images" if shot["media_type"] == "image" else "videos",
            "motion": shot.get("motion", "none"),
            "motion_intensity": float(shot.get("motion_intensity", "0") or 0),
            "effect": effect,
            "effect_intensity": float(shot.get("effect_intensity", "0") or 0),
            "transition_out": shot.get("transition_out", "none"),
            "transition_duration_ms": int(round(float(shot.get("transition_duration", "0") or 0) * 1000)),
        }
        intro_rows.append({
            "order": n, "director_v25_shot": key,
            "query": keyword if keyword else media["filename"],
            "backup_keywords": [s.strip() for s in shot.get("pexels_backup_keywords", "").split(",") if s.strip()]
                if media["source"] == "pexels_pending" else [],
            "media_type": shot["media_type"],
        })
    return {
        "schema": "scene-builder-director-v25-execution-v1",
        "scenes": scene_shots,
        "intro_queries": intro_rows,
        "intro_lookup": {key: val for key, val in resolved.items() if key.startswith("INTRO.")},
        "intro_edit": intro_edit,
        "source_mode": source_mode,
        "settings": settings,
        "narration_text": " ".join(combined_narration),
        "raw_director_txt": text,
        "preview": preview,
        "intro_count": 8 if settings["intro_enabled"] else 0,
    }


def bundle_exact_director_media(compilation: dict[str, Any], assets_dir: Path,
                                catalog: Any) -> dict[str, dict[str, Any]]:
    """Copy approved exact asset IDs to the job ZIP; no path is supplied by TXT."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    assignments: dict[str, dict[str, Any]] = {}
    copied: set[str] = set()

    def bundle(asset_id: str, expected_name: str, media_type: str) -> dict[str, Any]:
        path, row = catalog.file(asset_id)
        if not row["approved"] or row["original_name"] != expected_name or row["kind"] != media_type:
            raise DirectorSpecError(f"Director local asset changed after preflight: {expected_name}")
        if not path.is_file() or path.is_symlink():
            raise DirectorSpecError(f"Director local asset missing: {expected_name}")
        basename = asset_id + path.suffix.lower()
        if basename not in copied:
            shutil.copy2(path, assets_dir / basename)
            copied.add(basename)
        rel = "assets/scene_local/" + basename
        return {
            "media_path": rel, "media_source_path": rel,
            "media_provider": "scene_media_library",
            "provider_asset_id": asset_id,
            "assigned_media_type": media_type,
            "review_approved": True, "review_locked": True,
            "pexels_enabled": False,
        }

    for row in compilation["scenes"]:
        if row["director_v25_media_source"] == "scene_media_library":
            row.update(bundle(row["director_v25_media_asset_id"],
                              row["director_v25_media_filename"], row["media_type"]))
    parsed = parse_v25(compilation["raw_director_txt"])
    for index, (key, asset) in enumerate(sorted(compilation["intro_lookup"].items()), 1):
        if asset["source"] != "scene_media_library":
            continue
        kind = parsed["sections"][key]["media_type"]
        assignments[f"INTRO_{index:03d}"] = bundle(asset["asset_id"], asset["filename"], kind)
    return assignments

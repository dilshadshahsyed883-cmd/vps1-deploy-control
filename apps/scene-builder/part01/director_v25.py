"""Opt-in Scene Plan Director V25: read-only static intent preflight.

No worker/renderer integration is performed here. Classic parsing and queued
projects are unchanged. Only approved Scene Builder Media Library records are
eligible for exact filename resolution; standalone SML is never queried.
"""
from __future__ import annotations

import configparser
from pathlib import Path
import re
from typing import Any

VERSION = "SCENE_PLAN_VERSION=2"
SCENE_RE = re.compile(r"SCENE_([0-9]{3}|[1-9][0-9]{3})$")
SHOT_RE = re.compile(r"SCENE_([0-9]{3}|[1-9][0-9]{3})\.SHOT_(\d{2,3})$")
INTRO_SHOT_RE = re.compile(r"INTRO\.SHOT_(\d{2})$")
FILENAME_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
                       ".mp4", ".mov", ".m4v", ".webm", ".mkv"}
FIELDS = {
    "PROJECT": {"mode", "name", "base_preset", "validation"},
    "OUTPUT": {"resolution", "aspect_ratio", "fps", "codec", "filename"},
    "MEDIA": {"source_mode", "avoid_duplicates", "missing_media",
              "library_summary_generated"},
    "TIMING": {"audio", "narration_start", "split", "max_semantic_beat_seconds"},
    "CAPTIONS": {"captions_enabled", "caption_style", "caption_position"},
    "AUDIO": {"voice_volume_percent", "music_enabled"},
    "LOOK": {"lut_enabled", "lut_pack", "lut_preset"},
    "INTRO": {"intro_enabled", "intro_duration_seconds"},
}
SHOT_FIELDS = {
    "hook_role", "narration", "media_type", "library_filename", "library_asset_id",
    "library_keyword", "local_match_basis",
    "pexels_primary_keyword", "pexels_backup_keywords",
    "source_fallback", "motion", "motion_intensity", "animation_category",
    "animation", "animation_duration", "animation_intensity",
    "transition_out", "transition_duration", "effect", "effect_intensity",
    "visual_direction",
}
SCENE_FIELDS = {"narration", "visual_direction", "pace", "visual_mood"}
IMAGE_MOTIONS = {"none", "zoom_in", "zoom_out", "pan_left", "pan_right", "ken_burns"}
VIDEO_MOTIONS = {"none", "slow_push", "reframe_left", "reframe_right", "gentle_zoom"}
COMBO = {"zoom_1", "bend_zoom", "distort_left", "pendulum_1", "pendulum_2",
         "spin_fall", "bouncy_rush", "zoom_2", "distort_right", "sway_in",
         "roll_in_out_2", "sway_out", "fall_right", "bounce_1", "wobble"}
CAM = {"pan_left", "pan_right", "right_zoom", "dynamic_shake_2"}
TRANSITIONS = {
    "none", "hard_cut", "cross_dissolve", "film_fade", "smooth_zoom",
    "push", "slide", "whip_pan", "blur_dissolve", "light_flash",
    "luma_fade", "directional_wipe", "soft_spin", "zoom_blur",
    "dip_white", "dip_black",
}
HOOK_ROLES = {
    "core_subject_proof", "subject_variation", "context", "action_process",
    "detail", "scale_contrast", "consequence", "expansion_bridge",
}


class DirectorSpecError(ValueError):
    """Invalid Director V25 text or unsupported instruction key."""


def _value(value: str | None) -> str:
    value = str(value or "").strip()
    return "" if value.casefold() == "none" else value


def _required(doc: dict[str, dict[str, str]], section: str, key: str) -> str:
    value = _value(doc[section].get(key))
    if not value:
        raise DirectorSpecError(f"[{section}]: required field {key} is missing")
    return value


def parse_v25(text: str) -> dict[str, Any]:
    """Parse only the versioned Director grammar; never guess Classic format."""
    if len(text.encode("utf-8")) > 10 * 1024 * 1024:
        raise DirectorSpecError("Director TXT exceeds 10 MB")
    text = text.lstrip("\ufeff").strip()
    if not text or text.splitlines()[0].strip() != VERSION:
        raise DirectorSpecError("Director TXT must start with SCENE_PLAN_VERSION=2")
    config = configparser.RawConfigParser(interpolation=None, strict=True)
    config.optionxform = str.lower
    try:
        config.read_string("[HEADER]\n" + text)
    except configparser.Error as exc:
        raise DirectorSpecError(f"Invalid Director TXT section/key syntax: {exc}") from exc
    if dict(config.items("HEADER")) != {"scene_plan_version": "2"}:
        raise DirectorSpecError("Unsupported Director TXT header")
    doc: dict[str, dict[str, str]] = {}
    scene_numbers: set[int] = set()
    shot_numbers: dict[int, set[int]] = {}
    hook_numbers: set[int] = set()
    for section in config.sections()[1:]:
        values = dict(config.items(section))
        match_scene = SCENE_RE.fullmatch(section)
        match_shot = SHOT_RE.fullmatch(section)
        match_intro = INTRO_SHOT_RE.fullmatch(section)
        if section in FIELDS:
            allowed = FIELDS[section]
        elif match_scene:
            allowed = SCENE_FIELDS
            scene_numbers.add(int(match_scene.group(1)))
        elif match_shot:
            allowed = SHOT_FIELDS - {"hook_role"}
            shot_numbers.setdefault(int(match_shot.group(1)), set()).add(int(match_shot.group(2)))
        elif match_intro:
            allowed = SHOT_FIELDS
            hook_numbers.add(int(match_intro.group(1)))
        else:
            raise DirectorSpecError(f"Unknown Director section [{section}]")
        extra = sorted(set(values) - allowed)
        if extra:
            raise DirectorSpecError(f"[{section}]: unsupported instruction(s): {', '.join(extra)}")
        doc[section] = values
    required = set(FIELDS)
    if not required.issubset(doc):
        raise DirectorSpecError(f"Missing Director sections: {', '.join(sorted(required-set(doc)))}")
    if scene_numbers != set(range(1, len(scene_numbers) + 1)) or not scene_numbers:
        raise DirectorSpecError("Scene IDs must be sequential from SCENE_001")
    if hook_numbers != set(range(1, 9)):
        raise DirectorSpecError("Exactly 8 INTRO.SHOT_01 through INTRO.SHOT_08 sections are required")
    for number in scene_numbers:
        section = f"SCENE_{number:03d}"
        _required(doc, section, "narration")
        assigned = shot_numbers.get(number, set())
        if not assigned or assigned != set(range(1, len(assigned) + 1)):
            raise DirectorSpecError(f"[{section}]: shots must be sequential starting at SHOT_01")
    if set(shot_numbers) != scene_numbers:
        raise DirectorSpecError("Orphan shot section without matching scene")
    if _required(doc, "PROJECT", "mode") != "director":
        raise DirectorSpecError("[PROJECT]: mode must be director")
    if _required(doc, "PROJECT", "validation") != "strict":
        raise DirectorSpecError("[PROJECT]: validation must be strict")
    if _required(doc, "MEDIA", "source_mode") not in {"library_only", "scene_first"}:
        raise DirectorSpecError("[MEDIA]: source_mode must be library_only or scene_first")
    return {
        "sections": doc,
        "scenes": len(scene_numbers),
        "intro_shots": sorted(f"INTRO.SHOT_{n:02d}" for n in hook_numbers),
        "scene_shots": sorted((s for s in doc if SHOT_RE.fullmatch(s)),
                              key=lambda s: tuple(map(int, SHOT_RE.fullmatch(s).groups()))),
    }


def _bool(value: str, field: str, errors: list[str]) -> None:
    if value and value.casefold() not in {"true", "false"}:
        errors.append(f"{field}: expected true or false")


def _number(value: str, field: str, errors: list[str],
            minimum: float, maximum: float, integer: bool = False) -> None:
    if not value:
        return
    try:
        parsed = int(value) if integer else float(value)
        if not minimum <= parsed <= maximum:
            raise ValueError
        if parsed != parsed:
            raise ValueError
    except (ValueError, OverflowError):
        errors.append(f"{field}: expected {'integer' if integer else 'number'} in [{minimum}, {maximum}]")


def _validate_controls(doc: dict[str, dict[str, str]], errors: list[str],
                       capabilities: dict[str, Any] | None = None) -> None:
    output = doc["OUTPUT"]
    for key, allowed in {
        "resolution": {"480p", "720p", "1080p"},
        "aspect_ratio": {"16:9", "9:16", "1:1"},
        "codec": {"h264"},
    }.items():
        if _value(output.get(key)) not in allowed:
            errors.append(f"[OUTPUT].{key}: unsupported or missing value")
    _number(_value(output.get("fps")), "[OUTPUT].fps", errors, 20, 60, integer=True)
    filename = _value(output.get("filename"))
    if filename and (Path(filename).name != filename or not filename.lower().endswith(".mp4")):
        errors.append("[OUTPUT].filename must be a simple .mp4 basename")
    if _value(doc["TIMING"].get("audio")) != "narration_upload":
        errors.append("[TIMING].audio must be narration_upload")
    if _value(doc["TIMING"].get("narration_start")) not in {"auto", "manual"}:
        errors.append("[TIMING].narration_start must be auto or manual")
    if _value(doc["TIMING"].get("split")) != "manual":
        errors.append("[TIMING].split must be manual")
    _number(_value(doc["TIMING"].get("max_semantic_beat_seconds")),
            "[TIMING].max_semantic_beat_seconds", errors, 0.2, 3.0)
    _number(_value(doc["INTRO"].get("intro_duration_seconds")),
            "[INTRO].intro_duration_seconds", errors, 1, 600)
    _number(_value(doc["AUDIO"].get("voice_volume_percent")),
            "[AUDIO].voice_volume_percent", errors, 0, 150, integer=True)
    for section, key in (("MEDIA", "avoid_duplicates"), ("CAPTIONS", "captions_enabled"),
                         ("AUDIO", "music_enabled"), ("LOOK", "lut_enabled"),
                         ("INTRO", "intro_enabled")):
        _bool(_value(doc[section].get(key)), f"[{section}].{key}", errors)
    captions = doc["CAPTIONS"]
    if _value(captions.get("caption_position")) not in {"top", "center", "bottom"}:
        errors.append("[CAPTIONS].caption_position must be top, center or bottom")
    if capabilities and _value(captions.get("caption_style")) not in set(capabilities.get("caption_styles", [])):
        errors.append("[CAPTIONS].caption_style not registered in current Scene Builder")
    if capabilities and _value(doc["LOOK"].get("lut_enabled")).casefold() == "true":
        packs = capabilities.get("lut_packs", {})
        pack = _value(doc["LOOK"].get("lut_pack"))
        preset = _value(doc["LOOK"].get("lut_preset"))
        if pack not in packs or preset not in packs[pack]:
            errors.append("[LOOK]: selected LUT pack/preset not registered in current Scene Builder")
    if _value(doc["MEDIA"].get("missing_media")) != "fail":
        errors.append("[MEDIA].missing_media must be fail")
    for section in [s for s in doc if SHOT_RE.fullmatch(s) or INTRO_SHOT_RE.fullmatch(s)]:
        shot = doc[section]
        if not section.startswith("INTRO.") and not shot.get("narration") and (
                sum(1 for key in doc if SHOT_RE.fullmatch(key) and
                    key.split(".")[0] == section.split(".")[0]) > 1):
            errors.append(f"[{section}]: narration is required for multi-shot scenes")
        effect = _value(shot.get("effect")) or "none"
        if effect not in {"none", "subtle_pulse", "soft_glow", "edge_vignette",
                          "gentle_shake", "cinematic_grain", "bloom"}:
            errors.append(f"[{section}]: unsupported effect {effect}")
        kind = _value(shot.get("media_type"))
        if section.startswith("INTRO.") and _value(shot.get("hook_role")) not in HOOK_ROLES:
            errors.append(f"[{section}].hook_role missing or unrecognized")
        if _value(shot.get("local_match_basis")) not in {"", "descriptive_filename", "filename_only_unverified"}:
            errors.append(f"[{section}].local_match_basis invalid")
        if kind not in {"image", "video"}:
            errors.append(f"[{section}].media_type must be image or video")
        motion = _value(shot.get("motion")) or "none"
        animation = _value(shot.get("animation")) or "none"
        category = _value(shot.get("animation_category")) or "combo"
        if motion not in (IMAGE_MOTIONS if kind == "image" else VIDEO_MOTIONS):
            errors.append(f"[{section}].motion unsupported for {kind}")
        if category not in {"combo", "cam_motion"}:
            errors.append(f"[{section}].animation_category unsupported")
        elif animation not in (COMBO if category == "combo" else CAM) | {"none"}:
            errors.append(f"[{section}].animation unsupported in {category}")
        if animation != "none" and motion != "none":
            errors.append(f"[{section}]: animation and basic motion cannot both be active")
        _number(_value(shot.get("animation_duration")), f"[{section}].animation_duration", errors, .5, 5)
        _number(_value(shot.get("animation_intensity")), f"[{section}].animation_intensity", errors, 10, 100, integer=True)
        _number(_value(shot.get("motion_intensity")), f"[{section}].motion_intensity", errors, 0, 1)
        _number(_value(shot.get("effect_intensity")), f"[{section}].effect_intensity", errors, 0, 1)
        transition = _value(shot.get("transition_out")) or "none"
        if transition not in TRANSITIONS:
            errors.append(f"[{section}].transition_out unsupported")
        _number(_value(shot.get("transition_duration")), f"[{section}].transition_duration", errors, 0, .8)
        if _value(shot.get("source_fallback")) not in {"", "pexels_if_missing"}:
            errors.append(f"[{section}].source_fallback unsupported")


def preflight_v25(text: str, live_assets: list[dict[str, Any]],
                  active_source_mode: str, library_enabled: bool,
                  capabilities: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate intent against live approved filenames; NEVER claim render readiness."""
    try:
        parsed = parse_v25(text)
    except DirectorSpecError as exc:
        return {"status": "BLOCKED", "render_ready": False, "errors": [str(exc)],
                "warnings": [], "resolved_assets": [], "scenes": 0, "shots": 0}
    doc = parsed["sections"]
    errors: list[str] = []
    warnings: list[str] = [
        "Intent-only preflight: audio alignment, semantic suitability and worker render adapters are not verified.",
        "Live Media Library was checked; downloaded Summary TXT is an inventory snapshot, not an asset ID or content manifest.",
    ]
    actual_mode = _required(doc, "MEDIA", "source_mode")
    if actual_mode != active_source_mode:
        errors.append(f"[MEDIA].source_mode={actual_mode} conflicts with current UI mode={active_source_mode}")
    if not library_enabled:
        errors.append("Scene Media Library is disabled in Stock Media Settings")
    _validate_controls(doc, errors, capabilities)
    files: dict[str, list[dict[str, Any]]] = {}
    for asset in live_assets:
        if asset.get("approved") is True and asset.get("kind") in {"image", "video"}:
            files.setdefault(str(asset.get("original_name") or ""), []).append(asset)
    resolved: list[dict[str, str]] = []
    used: set[str] = set()
    intro_on = _value(doc["INTRO"].get("intro_enabled")).casefold() == "true"
    for section in parsed["intro_shots"] + parsed["scene_shots"]:
        if section.startswith("INTRO.") and not intro_on:
            continue
        shot = doc[section]
        name = _value(shot.get("library_filename"))
        kind = _value(shot.get("media_type"))
        keyword = _value(shot.get("library_keyword"))
        primary = _value(shot.get("pexels_primary_keyword"))
        backups = [v.strip() for v in _value(shot.get("pexels_backup_keywords")).split(",") if v.strip()]
        if len(backups) > 3:
            errors.append(f"[{section}]: at most 3 Pexels backup keywords")
        if actual_mode == "library_only" and (primary or backups):
            errors.append(f"[{section}]: Pexels keywords forbidden in Library Only mode")
        fallback = _value(shot.get("source_fallback"))
        if section.startswith("INTRO.") and not fallback:
            fallback = "pexels_if_missing"
        if name:
            if Path(name).name != name or Path(name).suffix.casefold() not in FILENAME_EXTENSIONS:
                errors.append(f"[{section}]: library_filename must be an approved basename with media extension")
                continue
            if keyword and keyword != Path(name).stem:
                errors.append(f"[{section}]: library_keyword must equal exact filename stem")
            matches = [row for row in files.get(name, []) if row.get("kind") == kind]
            chosen_id = _value(shot.get("library_asset_id"))
            if chosen_id:
                if not re.fullmatch(r"[0-9a-f]{32}", chosen_id):
                    errors.append(f"[{section}]: invalid library_asset_id")
                    continue
                matches = [row for row in matches if row.get("id") == chosen_id]
            if len(matches) == 1:
                asset = matches[0]
                ident = str(asset.get("id") or "")
                if ident in used and _value(doc["MEDIA"].get("avoid_duplicates")).casefold() == "true":
                    errors.append(f"[{section}]: repeated library asset {name}")
                used.add(ident)
                resolved.append({"shot": section, "filename": name, "asset_id": ident,
                                 "media_type": kind, "source": "scene_media_library"})
                if _value(shot.get("local_match_basis")) == "filename_only_unverified":
                    warnings.append(f"[{section}]: filename does not prove visual content; preview required")
                continue
            if len(matches) > 1:
                errors.append(f"[{section}]: duplicate exact filename {name}; approved asset ID required")
                continue
        if _value(shot.get("library_asset_id")) and not name:
            errors.append(f"[{section}]: library_asset_id without library_filename")
            continue
        if actual_mode == "scene_first" and fallback == "pexels_if_missing" and primary:
            resolved.append({"shot": section, "filename": "", "asset_id": "",
                             "media_type": kind, "source": "pexels_pending",
                             "pexels_query": primary})
            warnings.append(f"[{section}]: Pexels fallback is an unverified search, not media availability")
        else:
            errors.append(f"[{section}]: approved exact library filename missing or unavailable, fallback disallowed")
    return {
        "status": "BLOCKED" if errors else "VALIDATED_INTENT_ONLY",
        "render_ready": False,
        "scenes": parsed["scenes"],
        "shots": len(parsed["scene_shots"]),
        "intro_shots": len(parsed["intro_shots"]),
        "errors": errors[:100],
        "warnings": warnings[:100],
        "resolved_assets": resolved,
        "next_stage": "Connect Director-specific timing, editing, audio and render adapters before queue submission",
    }

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .creative_v23 import normalize_scene_creative_v23

LEGACY_FIELD_COUNT = 12
EDITORIAL_INSTRUCTION_KEYS = (
    "media_type",
    "primary_keyword",
    "backup_keywords",
    "visual_direction",
    "shot_preference",
    "pace",
    "visual_mood",
    "transition_intent",
    "effect_intent",
    "allow_media_fallback",
)

LEGACY_TECHNICAL_INSTRUCTION_KEYS = (
    "media_type",
    "primary_keyword",
    "backup_keywords",
    "visual_direction",
    "transition",
    "transition_duration",
    "filter",
    "filter_intensity",
    "effect",
    "effect_intensity",
    "motion",
    "motion_intensity",
    "overlay",
    "overlay_intensity",
    "transition_sfx",
    "sfx_volume",
    "allow_media_fallback",
)



def extract_intro_queries(text: str) -> list[dict[str, Any]]:
    """Extract ordered V17.4 intro media queries without making them scene rows.

    Accepted examples:
      INTRO_QUERY_01: Mauritius coastline drone
      INTRO_QUERY_02|Mauritian women walking beach
      INTRO_003|Port Louis street life|sfx=city ambience
    The main scene parser ignores these lines, preserving V16/V17 compatibility.
    """
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^INTRO(?:_QUERY)?_(\d{1,4})\s*(?::|\|)\s*(.+)$", line, flags=re.I)
        if not match:
            continue
        order = int(match.group(1))
        remainder = match.group(2).strip()
        parts = [part.strip() for part in remainder.split("|")]
        query = _clean(parts[0] if parts else "")
        if not query:
            continue
        row: dict[str, Any] = {"order": order, "query": query, "line_no": line_no}
        for part in parts[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().lower()
            value = _clean(value)
            if key in {"sfx", "environment_sfx", "ambient_sfx"} and value:
                row["environment_sfx"] = value
            elif key in {"media_type", "type"} and value.lower() in {"image", "video"}:
                row["media_type"] = value.lower()
            elif key in {"backup", "fallback", "backup_keywords"} and value:
                row["backup_keywords"] = [x.strip() for x in value.split(",") if x.strip()][:3]
        rows.append(row)
    rows.sort(key=lambda item: (int(item.get("order") or 0), int(item.get("line_no") or 0)))
    return rows


def _is_intro_query_line(line: str) -> bool:
    return bool(re.match(r"^INTRO(?:_QUERY)?_\d{1,4}\s*(?::|\|)", str(line or "").strip(), flags=re.I))

def _clean(value: str) -> str:
    return " ".join(str(value or "").replace("\t", " ").split()).strip()


def _float_value(value: str, default: float, line_no: int, label: str) -> float:
    try:
        return float(str(value or default).strip())
    except ValueError as exc:
        raise ValueError(f"Line {line_no}: {label} must be numeric.") from exc


def _parse_bool(value: str, line_no: int, label: str) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"Line {line_no}: {label} must be true or false.")


def _validate_scene_id(scene_id: str, line_no: int, seen: set[str]) -> str:
    scene_id = scene_id.upper().strip()
    if not scene_id.startswith("SCENE_") or not scene_id[6:].isdigit():
        raise ValueError(f"Line {line_no}: invalid Scene ID {scene_id!r}.")
    if scene_id in seen:
        raise ValueError(f"Line {line_no}: duplicate Scene ID {scene_id}.")
    seen.add(scene_id)
    return scene_id


def _parse_instruction_block(block: str, line_no: int) -> dict[str, str]:
    instructions: dict[str, str] = {}
    for segment in block.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if "=" not in segment:
            raise ValueError(
                f"Line {line_no}: invalid scene instruction {segment!r}. "
                "Every instruction must use key=value."
            )
        key, value = segment.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if not key:
            raise ValueError(f"Line {line_no}: an instruction key is empty.")
        if key in instructions:
            raise ValueError(f"Line {line_no}: duplicate instruction key {key!r}.")
        instructions[key] = value

    return instructions


def _parse_new_three_part(parts: list[str], line_no: int, seen: set[str]) -> dict[str, Any]:
    scene_id, narration, instruction_block = parts
    scene_id = _validate_scene_id(scene_id, line_no, seen)
    narration = _clean(narration)
    if not narration:
        raise ValueError(f"Line {line_no}: narration is empty.")

    values = _parse_instruction_block(instruction_block, line_no)
    editorial = all(key in values for key in EDITORIAL_INSTRUCTION_KEYS)
    legacy_technical = all(key in values for key in LEGACY_TECHNICAL_INSTRUCTION_KEYS)
    if not editorial and not legacy_technical:
        expected = ", ".join(EDITORIAL_INSTRUCTION_KEYS)
        raise ValueError(
            f"Line {line_no}: scene instructions must use the V17 editorial format "
            f"({expected}) or the complete legacy technical format."
        )

    media_type = values["media_type"].lower().strip()
    if media_type not in {"image", "video"}:
        raise ValueError(f"Line {line_no}: media_type must be image or video.")
    primary_keyword = _clean(values["primary_keyword"])
    if not primary_keyword:
        raise ValueError(f"Line {line_no}: primary_keyword is empty.")
    backups = [_clean(item) for item in values["backup_keywords"].split(",") if _clean(item)][:3]

    payload: dict[str, Any] = {
        "scene_code": scene_id,
        "text": narration,
        "visual_prompt": narration,
        "media_type": media_type,
        "primary_keyword": primary_keyword,
        "keyword": primary_keyword,
        "backup_keywords": backups,
        "fallback_keywords": backups,
        "visual_direction": _clean(values["visual_direction"]),
        "allow_media_fallback": _parse_bool(values["allow_media_fallback"], line_no, "allow_media_fallback"),
        "environment_sfx": _clean(values.get("environment_sfx", values.get("ambient_sfx", "none"))) or "none",
    }
    if editorial:
        payload.update(
            shot_preference=_clean(values["shot_preference"]) or "auto",
            pace=_clean(values["pace"]) or "normal",
            visual_mood=_clean(values["visual_mood"]) or "natural",
            transition_intent=_clean(values["transition_intent"]).lower() or "continuity",
            effect_intent=_clean(values["effect_intent"]).lower() or "none",
            # Technical choices are deliberately neutral here. The selected preset
            # resolves them later and the frozen render plan becomes authoritative.
            transition_out="cross_dissolve", transition_duration_ms=0,
            filter="none", filter_intensity=0.0, effect="none", effect_intensity=0.0,
            motion="none", motion_intensity=0.0, overlay="none", overlay_intensity=0.0,
            transition_sfx="none", sfx_volume=0.0,
            outer_ai_source="structured_txt_v17_editorial",
        )
    else:
        transition_seconds = _float_value(values["transition_duration"], 0.35, line_no, "transition_duration")
        payload.update(
            transition_out=values["transition"].strip().lower() or "dissolve",
            transition_duration_ms=int(round(transition_seconds * 1000)),
            filter=values["filter"].strip().lower() or "none",
            filter_intensity=_float_value(values["filter_intensity"], 0.5, line_no, "filter_intensity"),
            effect=values["effect"].strip().lower() or "none",
            effect_intensity=_float_value(values["effect_intensity"], 0.35, line_no, "effect_intensity"),
            motion=values["motion"].strip().lower() or "none",
            motion_intensity=_float_value(values["motion_intensity"], 0.35, line_no, "motion_intensity"),
            overlay=values["overlay"].strip() or "none",
            overlay_intensity=_float_value(values["overlay_intensity"], 0.35, line_no, "overlay_intensity"),
            transition_sfx=values["transition_sfx"].strip() or "none",
            sfx_volume=_float_value(values["sfx_volume"], 0.35, line_no, "sfx_volume"),
            outer_ai_source="structured_txt_v2_legacy_technical",
        )
    return normalize_scene_creative_v23(payload)


def _parse_legacy_twelve_part(parts: list[str], line_no: int, seen: set[str]) -> dict[str, Any]:
    (
        scene_id, narration, media_type, primary_keyword, backup_text,
        transition, transition_seconds, scene_filter, effect, motion,
        overlay, transition_sfx,
    ) = parts
    scene_id = _validate_scene_id(scene_id, line_no, seen)
    narration = _clean(narration)
    primary_keyword = _clean(primary_keyword)
    if not narration:
        raise ValueError(f"Line {line_no}: narration is empty.")
    media_type = media_type.lower().strip()
    if media_type not in {"image", "video"}:
        raise ValueError(f"Line {line_no}: media type must be image or video.")
    if not primary_keyword:
        raise ValueError(f"Line {line_no}: primary keyword is empty.")
    backups = [_clean(item) for item in backup_text.split(";") if _clean(item)][:3]
    transition_seconds_value = _float_value(transition_seconds, 0.35, line_no, "transition seconds")
    payload: dict[str, Any] = {
        "scene_code": scene_id,
        "text": narration,
        "visual_prompt": narration,
        "media_type": media_type,
        "primary_keyword": primary_keyword,
        "keyword": primary_keyword,
        "backup_keywords": backups,
        "fallback_keywords": backups,
        "transition_out": transition.strip().lower() or "dissolve",
        "transition_duration_ms": int(round(transition_seconds_value * 1000)),
        "filter": scene_filter.strip().lower() or "none",
        "effect": effect.strip().lower() or "none",
        "motion": motion.strip().lower() or "none",
        "overlay": overlay.strip() or "none",
        "transition_sfx": transition_sfx.strip() or "none",
        "environment_sfx": "none",
        "allow_media_fallback": True,
        "outer_ai_source": "structured_txt_legacy",
    }
    return normalize_scene_creative_v23(payload)


def parse_outer_ai_scene_txt(path: str | Path) -> list[dict[str, Any]]:
    """Parse outer-AI scene TXT.

    Preferred V17 format (3 pipe-separated sections) uses editorial intent only:
    SCENE_ID|NARRATION|media_type=...;primary_keyword=...;...;transition_intent=...

    The previous technical 3-part format and old 12-field format remain supported
    for existing projects, but their low-level choices are resolved/frozen before render.
    """
    source = Path(path)
    if not source.is_file():
        raise ValueError("Select a valid outer-AI scene TXT file.")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _is_intro_query_line(line):
            continue

        parts = [part.strip() for part in raw.split("|")]
        if len(parts) == 3:
            rows.append(_parse_new_three_part(parts, line_no, seen))
        elif len(parts) == LEGACY_FIELD_COUNT:
            rows.append(_parse_legacy_twelve_part(parts, line_no, seen))
        else:
            raise ValueError(
                f"Line {line_no} has {len(parts)} pipe-separated sections; expected 3 "
                f"(new format) or {LEGACY_FIELD_COUNT} (legacy format). "
                "Narration and instruction values must not contain the | character."
            )

    if not rows:
        raise ValueError("The selected TXT contains no scene rows.")
    return rows

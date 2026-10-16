from __future__ import annotations

from dataclasses import MISSING, fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
import json
import types

from smart_scroll.project_model import ProjectSettings

SECTIONS: dict[str, list[str]] = {
    "project": [
        "project_name", "text", "audio_path", "voice_volume_percent",
        "voice_speed_percent", "auto_sync_audio",
    ],
    "captions": [
        "captions_enabled", "caption_mode", "caption_text_source",
        "caption_sync_mode", "caption_language", "caption_whisper_model",
        "caption_max_words", "caption_position", "caption_timing_offset",
        "caption_min_duration", "caption_export_srt",
        "caption_heavy_enabled", "caption_heavy_preset",
        "caption_karaoke_enabled", "caption_highlight_color",
    ],
    "text_layout": [
        "font_family", "font_size", "bold", "text_color",
        "text_background_enabled", "text_background_color",
        "text_background_opacity_percent", "text_background_width_percent",
        "text_background_height_percent", "text_background_x_percent",
        "text_background_y_percent",
    ],
    "background": [
        "custom_background_enabled", "background_mode",
        "gradient_color_1", "gradient_color_2", "dark_overlay_percent",
        "background_image_path", "background_video_path",
    ],
    "media_motion": [
        "pexels_enabled", "motion_enabled", "story_visualizer_enabled",
    ],
    "output": [
        "width", "height", "fps", "quality_crf", "ffmpeg_preset",
        "video_encoder", "post_render_compress_enabled",
    ],
}

CHOICES: dict[str, list[Any]] = {
    "caption_text_source": ["script", "whisper"],
    "caption_sync_mode": ["fast", "better"],
    "caption_position": ["bottom", "center", "top"],
    "caption_whisper_model": ["tiny", "base", "small", "medium"],
    "caption_heavy_preset": ["story_pro", "clean", "bold"],
    "background_mode": ["gradient", "solid", "image", "video"],
    "ffmpeg_preset": ["ultrafast", "veryfast", "fast", "medium"],
    "video_encoder": ["cpu_x264"],
}

LABELS = {
    "caption_text_source": "Caption source",
    "caption_sync_mode": "Caption timing quality",
    "caption_max_words": "Words per caption",
    "caption_whisper_model": "Whisper model",
    "caption_heavy_preset": "Caption preset",
    "quality_crf": "Output quality (CRF)",
    "ffmpeg_preset": "FFmpeg preset",
    "voice_volume_percent": "Voice volume %",
    "voice_speed_percent": "Voice speed %",
}

def _resolved_types() -> dict[str, Any]:
    try:
        return get_type_hints(ProjectSettings)
    except Exception:
        return {field.name: field.type for field in fields(ProjectSettings)}

def _default_for(field) -> Any:
    if field.default is not MISSING:
        return field.default
    if field.default_factory is not MISSING:  # type: ignore[attr-defined]
        return field.default_factory()  # type: ignore[misc]
    return None

def defaults() -> dict[str, Any]:
    if not is_dataclass(ProjectSettings):
        return {}
    return {field.name: _default_for(field) for field in fields(ProjectSettings)}

def _base_type(annotation):
    origin = get_origin(annotation)
    if origin in (types.UnionType,):
        args = [x for x in get_args(annotation) if x is not type(None)]
        return args[0] if len(args) == 1 else str
    if origin is not None:
        args = [x for x in get_args(annotation) if x is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation

def _kind(annotation, value: Any) -> str:
    base = _base_type(annotation)
    if base is bool or isinstance(value, bool):
        return "boolean"
    if base is int or (isinstance(value, int) and not isinstance(value, bool)):
        return "integer"
    if base is float or isinstance(value, float):
        return "number"
    return "string"

def schema() -> dict[str, Any]:
    defaults_map = defaults()
    dataclass_fields = {field.name: field for field in fields(ProjectSettings)}
    resolved_types = _resolved_types()
    used = set()
    sections = []

    for section_name, names in SECTIONS.items():
        controls = []
        for name in names:
            field = dataclass_fields.get(name)
            if not field:
                continue
            used.add(name)
            default = defaults_map.get(name)
            controls.append({
                "name": name,
                "label": LABELS.get(name, name.replace("_", " ").title()),
                "kind": _kind(resolved_types.get(name, field.type), default),
                "default": default,
                "choices": CHOICES.get(name, []),
            })
        sections.append({"name": section_name, "controls": controls})

    advanced = []
    for name, field in dataclass_fields.items():
        if name in used or name in {
            "job_id", "output_path", "output_folder", "duration_seconds",
        }:
            continue
        default = defaults_map.get(name)
        advanced.append({
            "name": name,
            "label": LABELS.get(name, name.replace("_", " ").title()),
            "kind": _kind(resolved_types.get(name, field.type), default),
            "default": default,
            "choices": CHOICES.get(name, []),
        })

    sections.append({"name": "advanced", "controls": advanced})
    return {"sections": sections, "field_count": len(dataclass_fields)}

def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    dataclass_fields = {field.name: field for field in fields(ProjectSettings)}
    resolved_types = _resolved_types()
    result: dict[str, Any] = {}

    for name, value in payload.items():
        field = dataclass_fields.get(name)
        if field is None:
            continue

        annotation = resolved_types.get(name, field.type)
        base = _base_type(annotation)
        origin = get_origin(annotation)

        try:
            if base is bool:
                if isinstance(value, str):
                    result[name] = value.strip().lower() in {
                        "1", "true", "yes", "on"
                    }
                else:
                    result[name] = bool(value)
            elif base is int:
                result[name] = int(value)
            elif base is float:
                result[name] = float(value)
            elif base is dict or origin is dict:
                if isinstance(value, dict):
                    result[name] = value
                elif isinstance(value, str):
                    parsed = json.loads(value)
                    result[name] = parsed if isinstance(parsed, dict) else {}
                else:
                    result[name] = {}
            elif base is list or origin is list:
                if isinstance(value, list):
                    result[name] = value
                elif isinstance(value, str):
                    parsed = json.loads(value)
                    result[name] = parsed if isinstance(parsed, list) else []
                else:
                    result[name] = []
            elif value is None:
                result[name] = None
            else:
                result[name] = str(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    return result

def settings_file(project_path: Path) -> Path:
    return project_path / "project_settings.json"

def load_settings(project_path: Path) -> dict[str, Any]:
    path = settings_file(project_path)
    merged = defaults()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update(normalize(data))
        except Exception:
            pass
    return merged

def save_settings(project_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    current = load_settings(project_path)
    current.update(normalize(payload))
    path = settings_file(project_path)
    path.write_text(
        json.dumps(current, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return current

def build_project_settings(
    *,
    project_name: str,
    job_id: str,
    text: str,
    audio_path: str,
    output_path: str,
    output_folder: str,
    duration_seconds: float,
    saved: dict[str, Any],
) -> ProjectSettings:
    allowed = {field.name for field in fields(ProjectSettings)}
    values = normalize(saved)
    values.update({
        "project_name": project_name,
        "job_id": job_id,
        "text": text,
        "audio_path": audio_path,
        "output_path": output_path,
        "output_folder": output_folder,
        "duration_seconds": duration_seconds,
    })
    values = {key: value for key, value in values.items() if key in allowed}
    return ProjectSettings(**values)

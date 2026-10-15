"""Atomic project-profile persistence and machine-path validation."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Iterable

from .persistence import atomic_write_json, load_json_with_backup
from .project_model import ProjectSettings


def profile_file(state_dir: str | Path) -> Path:
    return Path(state_dir) / "profiles.json"


def load_profiles(state_dir: str | Path, legacy_path: str | Path | None = None) -> dict[str, dict]:
    path = profile_file(state_dir)
    backup = path.with_name(path.stem + "_backup.json")
    candidates: list[tuple[Path, Path | None]] = [(path, backup)]
    if legacy_path:
        candidates.append((Path(legacy_path), None))
    for primary, fallback in candidates:
        try:
            if fallback is None:
                if not primary.exists():
                    continue
                data = json.loads(primary.read_text(encoding="utf-8"))
            else:
                data, _loaded_from = load_json_with_backup(primary, fallback)
            if isinstance(data, dict):
                if primary != path and data:
                    save_profiles(state_dir, data)
                return {str(name): value for name, value in data.items() if isinstance(value, dict)}
        except Exception:
            continue
    return {}


def save_profiles(state_dir: str | Path, profiles: dict[str, dict]) -> None:
    path = profile_file(state_dir)
    backup = path.with_name(path.stem + "_backup.json")
    atomic_write_json(path, profiles, backup_path=backup, indent=2)


def snapshot_profile(settings: ProjectSettings) -> dict:
    profile = copy.deepcopy(settings)
    profile.text = ""
    profile.audio_path = ""
    profile.output_path = ""
    profile.output_folder = ""
    profile.queue_status = "Pending"
    profile.queue_note = ""
    profile.queue_stage = "Pending"
    profile.job_id = ""
    profile.queued_at = ""
    profile.queue_retry_count = 0
    profile.music_assigned_path = ""
    profile.render_audio_path = ""
    return profile.to_dict()


def validate_profile_paths(
    profile: ProjectSettings,
    *,
    default_pexels_folder: str | Path,
) -> tuple[ProjectSettings, list[str]]:
    safe = copy.deepcopy(profile)
    messages: list[str] = []
    if safe.piper_model_path and not Path(safe.piper_model_path).is_file():
        safe.piper_model_path = ""
        messages.append("Profile Piper model was not found on this PC; selection was cleared.")

    valid_music = [str(Path(path)) for path in list(safe.music_paths or []) if Path(path).is_file()]
    folder_valid = bool(safe.music_folder_path and Path(safe.music_folder_path).is_dir())
    safe.music_paths = valid_music
    safe.music_assigned_path = ""
    if safe.music_enabled and not valid_music and not folder_valid:
        safe.music_enabled = False
        messages.append("Profile music folder/files were not found on this PC; music was disabled safely.")

    if safe.pexels_library_folder and not Path(safe.pexels_library_folder).is_dir():
        safe.pexels_library_folder = str(default_pexels_folder)
        safe.pexels_cache_paths = []
        messages.append("Profile background folder was missing; default library selected.")
    if safe.background_image_path and not Path(safe.background_image_path).is_file():
        safe.background_image_path = ""
        if safe.background_mode == "image":
            safe.background_mode = "gradient"
        messages.append("Profile background image was missing; gradient fallback selected.")
    if safe.foreground_image_path and not Path(safe.foreground_image_path).is_file():
        safe.foreground_image_path = ""
        safe.foreground_enabled = False
        messages.append("Profile foreground image was missing; foreground was disabled safely.")
    return safe, messages

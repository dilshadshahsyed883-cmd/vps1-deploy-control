"""Fast, side-effect-free application diagnostics and render preflight checks."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from .ffmpeg_utils import find_tool


@dataclass(frozen=True)
class DiagnosticItem:
    key: str
    label: str
    ok: bool
    detail: str
    fix_hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _tool_version(name: str) -> DiagnosticItem:
    path = find_tool(name)
    if not path:
        return DiagnosticItem(name, name.upper(), False, "Not found", f"Place {name}.exe in ffmpeg/bin or add it to PATH.")
    try:
        result = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=5)
        first = ((result.stdout or result.stderr or "").splitlines() or ["Available"])[0].strip()
        return DiagnosticItem(name, name.upper(), result.returncode == 0, first[:180] or str(path))
    except Exception as exc:
        return DiagnosticItem(name, name.upper(), False, f"Could not execute: {exc}", "Check file permissions or replace the tool build.")


def _path_item(key: str, label: str, path_value: str, *, required: bool, writable: bool = False) -> DiagnosticItem:
    if not path_value:
        return DiagnosticItem(key, label, not required, "Not selected" if not required else "Required path is empty")
    path = Path(path_value).expanduser()
    exists = path.exists()
    if not exists:
        return DiagnosticItem(key, label, False, f"Missing: {path}")
    if writable:
        target = path if path.is_dir() else path.parent
        ok = os.access(target, os.W_OK)
        return DiagnosticItem(key, label, ok, f"Writable: {target}" if ok else f"Not writable: {target}")
    return DiagnosticItem(key, label, True, str(path))


def run_diagnostics(settings=None, font_families: Iterable[str] | None = None) -> list[DiagnosticItem]:
    items = [_tool_version("ffmpeg"), _tool_version("ffprobe")]
    if settings is None:
        return items

    output = str(getattr(settings, "output_path", "") or "")
    output_folder = str(Path(output).expanduser().parent) if output else str(getattr(settings, "output_folder", "") or "")
    items.append(_path_item("output", "Output Folder", output_folder, required=True, writable=True))
    audio = str(getattr(settings, "audio_path", "") or "")
    items.append(_path_item("audio", "Narration Audio", audio, required=False))

    music_enabled = bool(getattr(settings, "music_enabled", False))
    music = str(getattr(settings, "music_assigned_path", "") or "")
    items.append(_path_item("music", "Assigned Music", music, required=music_enabled))

    model = str(getattr(settings, "piper_model_path", "") or "")
    needs_model = bool(getattr(settings, "auto_generate_voice", False)) and not bool(audio)
    items.append(_path_item("piper", "Piper Model", model, required=needs_model))

    selected_font = str(getattr(settings, "font_family", "") or "")
    families = {str(name).casefold() for name in (font_families or [])}
    font_ok = not selected_font or not families or selected_font.casefold() in families
    items.append(DiagnosticItem("font", "Selected Font", font_ok, selected_font or "System default", "Choose an installed font." if not font_ok else ""))

    probe_path = Path(output_folder or Path.home())
    try:
        probe_path.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(probe_path)
        free_gb = usage.free / (1024 ** 3)
        items.append(DiagnosticItem("disk", "Free Disk Space", free_gb >= 2.0, f"{free_gb:.1f} GB free", "Free at least 2 GB before rendering." if free_gb < 2.0 else ""))
    except Exception as exc:
        items.append(DiagnosticItem("disk", "Free Disk Space", False, f"Could not check: {exc}"))
    return items



def run_preflight(settings=None, font_families: Iterable[str] | None = None) -> list[DiagnosticItem]:
    """Fast checks safe to run on the UI thread before starting a job."""
    items: list[DiagnosticItem] = []
    for name in ("ffmpeg", "ffprobe"):
        path = find_tool(name)
        items.append(DiagnosticItem(name, name.upper(), bool(path), str(path) if path else "Not found", f"Place {name}.exe in ffmpeg/bin or add it to PATH." if not path else ""))
    if settings is None:
        return items
    output = str(getattr(settings, "output_path", "") or "")
    output_folder = str(Path(output).expanduser().parent) if output else str(getattr(settings, "output_folder", "") or "")
    items.append(_path_item("output", "Output Folder", output_folder, required=True, writable=True))
    audio = str(getattr(settings, "audio_path", "") or "")
    music_enabled = bool(getattr(settings, "music_enabled", False))
    music = str(getattr(settings, "music_assigned_path", "") or "")
    items.append(_path_item("music", "Assigned Music", music, required=music_enabled))
    needs_model = bool(getattr(settings, "auto_generate_voice", False)) and not bool(audio) and bool(getattr(settings, "captions_enabled", True))
    model = str(getattr(settings, "piper_model_path", "") or "")
    items.append(_path_item("piper", "Piper Model", model, required=needs_model))
    try:
        probe_path = Path(output_folder or Path.home())
        probe_path.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(probe_path).free / (1024 ** 3)
        items.append(DiagnosticItem("disk", "Free Disk Space", free_gb >= 2.0, f"{free_gb:.1f} GB free", "Free at least 2 GB before rendering." if free_gb < 2.0 else ""))
    except Exception as exc:
        items.append(DiagnosticItem("disk", "Free Disk Space", False, f"Could not check: {exc}"))
    return items

def blocking_failures(items: Iterable[DiagnosticItem]) -> list[DiagnosticItem]:
    critical = {"ffmpeg", "ffprobe", "output", "music", "piper", "disk"}
    return [item for item in items if not item.ok and item.key in critical]

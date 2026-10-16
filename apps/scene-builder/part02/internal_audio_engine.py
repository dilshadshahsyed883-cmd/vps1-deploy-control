from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

VERSION = "1.0.0-worker"
ROOT = Path(os.environ.get("SCENE_INTERNAL_AUDIO_ENGINE_ROOT", "/data/internal_audio_engine"))
PRESET_OVERRIDES_FILE = ROOT / "preset_overrides.json"
INTEGRATION_SETTINGS_FILE = ROOT / "integration_settings.json"
FFMPEG_THREADS = max(1, int(os.environ.get("SCENE_INTERNAL_AUDIO_FFMPEG_THREADS", "2")))

# Voice-engineering values are intentionally preserved from standalone Audio Engine v2.
# Timeline/music values remain accepted for compatibility but are NOT applied here.
PRESETS: dict[str, dict[str, Any]] = {
    "documentary": {
        "name": "Documentary Voice",
        "defaults": {"noise_enabled": False, "noise_floor": -48.0, "noise_reduction": 5.0, "highpass": 70.0, "bass_gain": 0.8, "mud_gain": -1.2, "presence_gain": 1.2, "air_gain": 0.3, "deesser": 0.18, "comp_threshold": 0.18, "comp_ratio": 2.4, "comp_attack": 12.0, "comp_release": 140.0, "comp_makeup": 1.0, "target_lufs": -16.0, "true_peak": -1.5, "lra": 10.0, "duck_db": 11.0, "duck_attack": 25.0, "duck_release": 700.0, "music_base_lufs": -24.0, "music_level_db": -2.0, "narration_start": 2.5, "music_start": 0.0, "music_fade_in": 2.0, "narration_fade_in": 0.10, "music_fade_out": 3.0, "output_gain_db": 0.0},
    },
    "clean": {
        "name": "Clean Narration",
        "defaults": {"noise_enabled": True, "noise_floor": -44.0, "noise_reduction": 8.0, "highpass": 80.0, "bass_gain": -0.3, "mud_gain": -1.5, "presence_gain": 1.0, "air_gain": 0.2, "deesser": 0.24, "comp_threshold": 0.17, "comp_ratio": 2.5, "comp_attack": 10.0, "comp_release": 150.0, "comp_makeup": 1.0, "target_lufs": -17.0, "true_peak": -1.5, "lra": 11.0, "duck_db": 10.0, "duck_attack": 30.0, "duck_release": 750.0, "music_base_lufs": -24.0, "music_level_db": -2.5, "narration_start": 2.0, "music_start": 0.0, "music_fade_in": 2.0, "narration_fade_in": 0.10, "music_fade_out": 3.0, "output_gain_db": 0.0},
    },
    "natural": {
        "name": "Natural Voice",
        "defaults": {"noise_enabled": False, "noise_floor": -50.0, "noise_reduction": 4.0, "highpass": 65.0, "bass_gain": 0.3, "mud_gain": -0.5, "presence_gain": 0.4, "air_gain": 0.1, "deesser": 0.10, "comp_threshold": 0.20, "comp_ratio": 1.8, "comp_attack": 18.0, "comp_release": 180.0, "comp_makeup": 1.0, "target_lufs": -18.0, "true_peak": -1.8, "lra": 12.0, "duck_db": 9.0, "duck_attack": 35.0, "duck_release": 850.0, "music_base_lufs": -25.0, "music_level_db": -2.0, "narration_start": 2.5, "music_start": 0.0, "music_fade_in": 2.5, "narration_fade_in": 0.15, "music_fade_out": 3.5, "output_gain_db": 0.0},
    },
    "deep": {
        "name": "Deep Voice",
        "defaults": {"noise_enabled": False, "noise_floor": -48.0, "noise_reduction": 5.0, "highpass": 60.0, "bass_gain": 1.6, "mud_gain": -1.0, "presence_gain": 0.6, "air_gain": -0.2, "deesser": 0.16, "comp_threshold": 0.17, "comp_ratio": 2.6, "comp_attack": 14.0, "comp_release": 160.0, "comp_makeup": 1.0, "target_lufs": -16.5, "true_peak": -1.5, "lra": 10.0, "duck_db": 11.0, "duck_attack": 30.0, "duck_release": 750.0, "music_base_lufs": -24.0, "music_level_db": -2.5, "narration_start": 2.5, "music_start": 0.0, "music_fade_in": 2.0, "narration_fade_in": 0.12, "music_fade_out": 3.0, "output_gain_db": 0.0},
    },
    "youtube": {
        "name": "Loud YouTube Voice",
        "defaults": {"noise_enabled": False, "noise_floor": -46.0, "noise_reduction": 6.0, "highpass": 75.0, "bass_gain": 0.6, "mud_gain": -1.2, "presence_gain": 1.6, "air_gain": 0.4, "deesser": 0.25, "comp_threshold": 0.15, "comp_ratio": 3.0, "comp_attack": 8.0, "comp_release": 110.0, "comp_makeup": 1.0, "target_lufs": -14.5, "true_peak": -1.2, "lra": 8.0, "duck_db": 12.0, "duck_attack": 20.0, "duck_release": 600.0, "music_base_lufs": -23.5, "music_level_db": -2.0, "narration_start": 1.5, "music_start": 0.0, "music_fade_in": 1.5, "narration_fade_in": 0.08, "music_fade_out": 2.5, "output_gain_db": 0.0},
    },
}

VOICE_RANGES = {
    "narration_fade_in": (0.0, 5.0),
    "noise_floor": (-70.0, -20.0), "noise_reduction": (0.01, 24.0), "highpass": (30.0, 180.0),
    "bass_gain": (-8.0, 8.0), "mud_gain": (-8.0, 8.0), "presence_gain": (-8.0, 8.0), "air_gain": (-8.0, 8.0),
    "deesser": (0.0, 1.0), "comp_threshold": (0.01, 1.0), "comp_ratio": (1.0, 20.0),
    "comp_attack": (0.1, 1000.0), "comp_release": (1.0, 5000.0), "comp_makeup": (0.0, 8.0),
    "target_lufs": (-24.0, -10.0), "lra": (1.0, 20.0), "true_peak": (-6.0, -0.1),
    "output_gain_db": (-12.0, 12.0),
}


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8")) if path.is_file() else {}
    except Exception:
        return {}


def _preset_defaults(preset: str) -> dict[str, Any]:
    preset = preset if preset in PRESETS else "documentary"
    base = dict(PRESETS[preset]["defaults"])
    overrides = _read_json(PRESET_OVERRIDES_FILE)
    if isinstance(overrides, dict):
        entry = overrides.get(preset)
        raw = entry.get("defaults") if isinstance(entry, dict) and isinstance(entry.get("defaults"), dict) else {}
        if raw:
            base.update(raw)
    return base


def scene_builder_saved_settings() -> dict[str, Any]:
    data = _read_json(INTEGRATION_SETTINGS_FILE)
    if not isinstance(data, dict):
        return {}
    entry = data.get("scene_builder")
    raw = entry.get("settings") if isinstance(entry, dict) else {}
    return dict(raw) if isinstance(raw, dict) else {}


def resolve_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(config or {})
    saved = scene_builder_saved_settings()
    preset = str(config.get("preset") or saved.get("preset") or "documentary").strip()
    if preset not in PRESETS:
        preset = "documentary"
    defaults = _preset_defaults(preset)
    merged = dict(defaults)
    merged.update({k: v for k, v in saved.items() if k in defaults or k in {"mode", "noise_enabled", "preset"}})
    merged.update({k: v for k, v in config.items() if k in defaults or k in {"mode", "noise_enabled", "preset"}})
    result: dict[str, Any] = {"preset": preset}
    result["mode"] = str(merged.get("mode") or "auto").strip().lower()
    if result["mode"] not in {"auto", "manual"}:
        result["mode"] = "auto"
    result["noise_enabled"] = _bool(merged.get("noise_enabled"), bool(defaults.get("noise_enabled", False)))
    for key, (lo, hi) in VOICE_RANGES.items():
        raw = merged.get(key, defaults.get(key))
        try:
            value = float(raw)
        except Exception:
            value = float(defaults.get(key, 0.0))
        result[key] = max(lo, min(hi, value))
    # Explicit ownership boundary: Scene Builder Timing and Music/SFX own these values.
    result["timeline_ownership"] = "scene_builder"
    result["music_ownership"] = "scene_builder"
    return result


def presets_payload() -> dict[str, Any]:
    return {
        key: {
            "name": value["name"],
            "defaults": resolve_settings({"preset": key}),
        }
        for key, value in PRESETS.items()
    }


def settings_fingerprint(settings: dict[str, Any]) -> str:
    stable = {k: settings[k] for k in sorted(settings) if k not in {"timeline_ownership", "music_ownership"}}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def ffprobe_duration(path: str | Path) -> float:
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ], stderr=subprocess.DEVNULL, text=True, timeout=30).strip()
        return max(0.0, float(out))
    except Exception:
        return 0.0


def _voice_filters(settings: dict[str, Any]) -> str:
    filters = [f"highpass=f={settings['highpass']:.2f}"]
    if settings.get("noise_enabled", False):
        filters.append(f"afftdn=nf={settings['noise_floor']:.2f}:nr={settings['noise_reduction']:.2f}:tn=1:gs=5")
    if abs(settings["bass_gain"]) > 0.01:
        filters.append(f"equalizer=f=120:t=q:w=1:g={settings['bass_gain']:.2f}")
    if abs(settings["mud_gain"]) > 0.01:
        filters.append(f"equalizer=f=250:t=q:w=1.1:g={settings['mud_gain']:.2f}")
    if abs(settings["presence_gain"]) > 0.01:
        filters.append(f"equalizer=f=3200:t=q:w=1.2:g={settings['presence_gain']:.2f}")
    if abs(settings["air_gain"]) > 0.01:
        filters.append(f"equalizer=f=9000:t=q:w=1.0:g={settings['air_gain']:.2f}")
    if settings["deesser"] > 0.001:
        filters.append(f"deesser=i={settings['deesser']:.3f}:m=0.35:f=0.58")
    filters.append(
        "acompressor="
        f"threshold={settings['comp_threshold']:.4f}:ratio={settings['comp_ratio']:.2f}:"
        f"attack={settings['comp_attack']:.2f}:release={settings['comp_release']:.2f}:"
        f"makeup={settings['comp_makeup']:.2f}:knee=3"
    )
    if settings["narration_fade_in"] > 0.001:
        filters.append(f"afade=t=in:st=0:d={settings['narration_fade_in']:.3f}")
    # Deliberately no adelay here. Narration start belongs to Scene Builder Timing.
    return ",".join(filters)


def _extract_loudnorm_json(stderr_text: str) -> dict[str, Any] | None:
    matches = re.findall(r'\{\s*"input_i".*?\}', stderr_text or "", flags=re.S)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except Exception:
        return None


def measure_loudness(path: str | Path, target_lufs: float, true_peak: float, lra: float) -> dict[str, Any] | None:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-threads", str(FFMPEG_THREADS), "-i", str(path),
        "-af", f"loudnorm=I={target_lufs:.2f}:TP={true_peak:.2f}:LRA={lra:.2f}:print_format=json",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=600)
    if proc.returncode != 0:
        return None
    return _extract_loudnorm_json(proc.stderr)


def _two_pass_filter(stats: dict[str, Any] | None, target_lufs: float, true_peak: float, lra: float) -> str:
    if not stats:
        return f"loudnorm=I={target_lufs:.2f}:TP={true_peak:.2f}:LRA={lra:.2f}:linear=false"
    try:
        return (
            f"loudnorm=I={target_lufs:.2f}:TP={true_peak:.2f}:LRA={lra:.2f}:"
            f"measured_I={float(stats['input_i']):.2f}:measured_LRA={float(stats['input_lra']):.2f}:"
            f"measured_TP={float(stats['input_tp']):.2f}:measured_thresh={float(stats['input_thresh']):.2f}:"
            f"offset={float(stats['target_offset']):.2f}:linear=true"
        )
    except Exception:
        return f"loudnorm=I={target_lufs:.2f}:TP={true_peak:.2f}:LRA={lra:.2f}:linear=false"


def _limiter_linear(db_value: float) -> float:
    return max(0.05, min(0.999, 10 ** (float(db_value) / 20.0)))


def qc_for_output(path: str | Path, settings: dict[str, Any]) -> dict[str, Any]:
    target = max(-24.0, min(-10.0, float(settings["target_lufs"]) + float(settings["output_gain_db"])))
    true_peak = float(settings["true_peak"])
    stats = measure_loudness(path, target, true_peak, float(settings["lra"])) or {}
    def f(key: str) -> float | None:
        try:
            v = float(stats.get(key))
            return v if math.isfinite(v) else None
        except Exception:
            return None
    measured_lufs, measured_tp = f("input_i"), f("input_tp")
    return {
        "target_lufs": round(target, 2),
        "target_true_peak": round(true_peak, 2),
        "measured_lufs": round(measured_lufs, 2) if measured_lufs is not None else None,
        "measured_true_peak": round(measured_tp, 2) if measured_tp is not None else None,
        "measured_lra": stats.get("input_lra"),
        "pass": bool(measured_lufs is not None and measured_tp is not None and abs(measured_lufs - target) <= 0.8 and measured_tp <= true_peak + 0.30),
    }


def process_voice(
    input_path: str | Path,
    output_path: str | Path,
    *,
    config: dict[str, Any] | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    source, output = Path(input_path), Path(output_path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError("Internal Audio Engine input is missing or empty")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("Internal Audio Engine requires ffmpeg and ffprobe")
    settings = resolve_settings(config)
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = ffprobe_duration(source)
    if duration <= 0:
        raise RuntimeError("Internal Audio Engine could not determine narration duration")
    if progress_callback:
        progress_callback(5, "Voice engineering")
    with tempfile.TemporaryDirectory(prefix="scene_internal_audio_", dir=str(output.parent)) as td:
        tmp = Path(td)
        premaster = tmp / "premaster.wav"
        cmd1 = [
            "ffmpeg", "-hide_banner", "-y", "-threads", str(FFMPEG_THREADS), "-i", str(source),
            "-map", "0:a:0", "-vn", "-af", _voice_filters(settings),
            "-ac", "1", "-ar", "48000", "-c:a", "pcm_f32le", str(premaster),
        ]
        proc1 = subprocess.run(cmd1, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=max(600, int(duration * 4 + 120)))
        if proc1.returncode != 0:
            raise RuntimeError("Internal Audio Engine voice stage failed: " + proc1.stderr[-1800:])
        if progress_callback:
            progress_callback(50, "Loudness analysis")
        target = max(-24.0, min(-10.0, float(settings["target_lufs"]) + float(settings["output_gain_db"])))
        tp = float(settings["true_peak"])
        lra = float(settings["lra"])
        stats = measure_loudness(premaster, target, tp, lra)
        master_filter = f"{_two_pass_filter(stats, target, tp, lra)},alimiter=limit={_limiter_linear(tp):.6f}:attack=5:release=80:level=false"
        partial = output.with_name(output.stem + ".partial" + output.suffix)
        partial.unlink(missing_ok=True)
        cmd2 = [
            "ffmpeg", "-hide_banner", "-y", "-threads", str(FFMPEG_THREADS), "-i", str(premaster),
            "-af", master_filter, "-vn", "-ac", "1", "-ar", "48000",
            "-c:a", "libmp3lame", "-b:a", "192k", str(partial),
        ]
        if progress_callback:
            progress_callback(70, "Mastering narration")
        proc2 = subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=max(600, int(duration * 4 + 120)))
        if proc2.returncode != 0:
            partial.unlink(missing_ok=True)
            raise RuntimeError("Internal Audio Engine mastering failed: " + proc2.stderr[-1800:])
        if not partial.is_file() or partial.stat().st_size < 1024:
            raise RuntimeError("Internal Audio Engine produced an empty output")
        os.replace(partial, output)
    if progress_callback:
        progress_callback(92, "Audio QC")
    qc = qc_for_output(output, settings)
    if progress_callback:
        progress_callback(100, "Narration ready")
    return {
        "version": VERSION,
        "preset": settings["preset"],
        "settings_fingerprint": settings_fingerprint(settings),
        "duration": ffprobe_duration(output),
        "qc": qc,
        "timeline_owned_by": "scene_builder",
        "music_owned_by": "scene_builder",
        "output": str(output),
    }

"""Narration processing, one-track music rotation mix, and waveform ducking.

ACS_INTERNAL_AUDIO_ENGINE_INTEGRATION_V1

The mixer runs in a worker thread and produces a temporary AAC audio file.
The video renderer then consumes that validated file while caption timing keeps
using the original narration audio.
"""
from __future__ import annotations

import math
import os
import json
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ffmpeg_utils import FFmpegError, get_audio_duration, require_ffmpeg
from .internal_audio_engine import process_voice as process_internal_voice

ProgressCallback = Callable[[int, str], None]

def _acs_audio_settings() -> dict:
    try:
        path = Path("/var/lib/acs-audio-engine/integration_settings.json")
        data = json.loads(path.read_text("utf-8")) if path.is_file() else {}
        entry = data.get("acs") if isinstance(data, dict) else {}
        raw = entry.get("settings") if isinstance(entry, dict) else {}
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}

def _shared_music_number(key: str, fallback: float) -> float:
    raw = _acs_audio_settings().get(key, fallback)
    try: return float(raw)
    except Exception: return float(fallback)

def _shared_duck_enabled(fallback: bool) -> bool:
    raw = _acs_audio_settings().get("duck_enabled", fallback)
    if isinstance(raw, bool): return raw
    return str(raw).strip().lower() in {"1","true","yes","on","enabled"}

def _shared_duck_strength(fallback: str) -> str:
    try: db=float(_acs_audio_settings().get("duck_db"))
    except Exception: return str(fallback or "medium")
    return "strong" if db >= 12 else ("medium" if db >= 10 else "light")
CancelCallback = Callable[[], bool]


class AudioMixCancelled(RuntimeError):
    pass


class MusicMixError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioMixResult:
    path: str
    temporary: bool
    duration: float
    used_music: bool


def _atempo_filters(speed: float) -> list[str]:
    speed = max(0.25, min(4.0, float(speed or 1.0)))
    values: list[float] = []
    while speed > 2.0:
        values.append(2.0)
        speed /= 2.0
    while speed < 0.5:
        values.append(0.5)
        speed /= 0.5
    values.append(speed)
    return [f"atempo={value:.5f}" for value in values if abs(value - 1.0) > 0.0001]


def _voice_filter_chain(settings) -> list[str]:
    chain = ["aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"]
    chain.extend(_atempo_filters(float(getattr(settings, "voice_speed_percent", 100) or 100) / 100.0))
    volume = max(0.0, min(3.0, float(getattr(settings, "voice_volume_percent", 100) or 100) / 100.0))
    if abs(volume - 1.0) > 0.0001:
        chain.append(f"volume={volume:.5f}")

    metadata = getattr(settings, "metadata", {}) or {}
    is_web_flow = isinstance(metadata, dict) and bool(metadata.get("web_flow_v1"))
    engine_cfg = _acs_audio_settings()
    engine_enabled = is_web_flow
    # Internal Audio Engine replaces the legacy EQ/compressor stage; speed and volume below remain ACS-owned.
    preset = "off" if engine_enabled else str(getattr(settings, "voice_processing_preset", "natural") or "natural").strip().lower()
    if preset == "natural":
        chain.extend([
            "highpass=f=70",
            "acompressor=threshold=0.125:ratio=2.5:attack=20:release=240:makeup=1.15",
        ])
    elif preset == "clear":
        chain.extend([
            "highpass=f=85",
            "equalizer=f=3000:t=q:w=1.2:g=2",
            "acompressor=threshold=0.100:ratio=3:attack=12:release=190:makeup=1.28",
        ])
    elif preset == "strong":
        chain.extend([
            "highpass=f=90",
            "equalizer=f=3200:t=q:w=1.1:g=2.5",
            "acompressor=threshold=0.080:ratio=4:attack=8:release=160:makeup=1.42",
        ])
    return chain


def _duck_values(strength: str) -> tuple[float, float, float, float]:
    strength = (strength or "medium").strip().lower()
    if strength == "light":
        return 0.035, 4.0, 35.0, 700.0
    if strength == "strong":
        return 0.012, 12.0, 18.0, 1200.0
    return 0.020, 8.0, 25.0, 900.0


def _looped_music_graph(
    *,
    input_label: str,
    music_duration: float,
    target_duration: float,
    loop_enabled: bool,
    crossfade_seconds: float,
) -> tuple[list[str], str]:
    """Build a finite music bed. Returns filter statements and final label."""
    statements: list[str] = []
    target_duration = max(0.2, float(target_duration))
    music_duration = max(0.2, float(music_duration))
    base_format = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

    if music_duration >= target_duration or not loop_enabled:
        pad = f",apad=pad_dur={target_duration:.3f}" if music_duration < target_duration else ""
        statements.append(
            f"[{input_label}]{base_format}{pad},atrim=0:{target_duration:.3f},asetpts=PTS-STARTPTS[music_raw]"
        )
        return statements, "music_raw"

    fade = max(0.05, min(float(crossfade_seconds), music_duration * 0.25, 1.5))
    effective = max(0.1, music_duration - fade)
    count = max(2, int(math.ceil((target_duration - fade) / effective)) + 1)
    # Prevent pathological one-second clips from creating an enormous graph.
    count = min(count, 40)
    split_labels = "".join(f"[music_part_{i}]" for i in range(count))
    statements.append(f"[{input_label}]{base_format},asplit={count}{split_labels}")
    for i in range(count):
        statements.append(
            f"[music_part_{i}]atrim=0:{music_duration:.3f},asetpts=PTS-STARTPTS[music_trim_{i}]"
        )
    current = "music_trim_0"
    for i in range(1, count):
        output = f"music_cross_{i}"
        statements.append(
            f"[{current}][music_trim_{i}]acrossfade=d={fade:.3f}:c1=tri:c2=tri[{output}]"
        )
        current = output
    statements.append(f"[{current}]atrim=0:{target_duration:.3f},asetpts=PTS-STARTPTS[music_raw]")
    return statements, "music_raw"


def _run_ffmpeg(
    cmd: list[str],
    *,
    progress_callback: ProgressCallback | None,
    cancel_callback: CancelCallback | None,
) -> None:
    """Run FFmpeg without risking a full stderr pipe deadlock.

    FFmpeg may emit enough diagnostics to fill an OS pipe. A dedicated reader
    continuously drains stderr while keeping only a bounded tail for errors.
    """
    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = int(subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    stderr_tail: deque[str] = deque(maxlen=200)

    def _drain_stderr() -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                if line:
                    stderr_tail.append(line.rstrip())
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    reader = threading.Thread(target=_drain_stderr, name="acs-audio-ffmpeg-stderr", daemon=True)
    reader.start()
    last_heartbeat = 0.0
    cancelled = False
    try:
        while proc.poll() is None:
            if cancel_callback and cancel_callback():
                cancelled = True
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
                break
            now = time.monotonic()
            if progress_callback and now - last_heartbeat >= 1.0:
                progress_callback(55, "Processing narration audio...")
                last_heartbeat = now
            time.sleep(0.20)
    finally:
        try:
            reader.join(timeout=2.0)
        except Exception:
            pass

    if cancelled:
        raise AudioMixCancelled("Audio mixing cancelled.")
    if proc.returncode != 0:
        stderr = "\n".join(stderr_tail).strip()
        raise MusicMixError(stderr or f"FFmpeg audio mixer failed with code {proc.returncode}.")


def prepare_final_audio(
    settings,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
    max_duration: float | None = None,
    output_tag: str = "final_mix",
) -> AudioMixResult | None:
    narration_path = Path(str(getattr(settings, "audio_path", "") or ""))
    # Keep legacy ACS behavior unchanged: background music remains disabled there.
    # The separate New Web Flow explicitly opts in through its frozen metadata.
    metadata = getattr(settings, "metadata", {}) or {}
    is_web_flow = isinstance(metadata, dict) and bool(metadata.get("web_flow_v1"))
    engine_cfg = _acs_audio_settings()
    engine_enabled = is_web_flow
    engine_preset = str(engine_cfg.get("preset") or "documentary").strip().lower()
    music_enabled = is_web_flow and bool(getattr(settings, "music_enabled", False))
    assigned_music = Path(str(getattr(settings, "music_assigned_path", "") or ""))
    voice_preset = str(getattr(settings, "voice_processing_preset", "natural") or "natural").lower()
    extra_end_seconds = max(0.0, float(getattr(settings, "extra_end_seconds", 0.0) or 0.0))
    needs_voice_processing = (
        engine_enabled
        or voice_preset != "off"
        or int(getattr(settings, "voice_volume_percent", 100) or 100) != 100
        or int(getattr(settings, "voice_speed_percent", 100) or 100) != 100
        or extra_end_seconds > 0.0
    )

    if not narration_path.exists():
        if music_enabled:
            raise MusicMixError("Background music is enabled, but no narration audio is available for ducking/mixing.")
        return None
    if music_enabled and (not str(getattr(settings, "music_assigned_path", "") or "").strip() or not assigned_music.is_file()):
        raise MusicMixError(f"Assigned music track was not found or is not a file: {assigned_music}")
    if not music_enabled and not needs_voice_processing:
        return None

    if cancel_callback and cancel_callback():
        raise AudioMixCancelled("Audio mixing cancelled.")
    ffmpeg = require_ffmpeg()
    original_duration = get_audio_duration(str(narration_path))
    speed = max(0.25, min(4.0, float(getattr(settings, "voice_speed_percent", 100) or 100) / 100.0))
    target_duration = max(0.2, (original_duration / speed) + extra_end_seconds)
    if max_duration is not None:
        target_duration = min(target_duration, max(0.5, float(max_duration)))

    output_path = Path(str(getattr(settings, "output_path", "") or "output.mp4"))
    temp_dir = output_path.parent / "_acs_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    job_id = str(getattr(settings, "job_id", "") or output_path.stem or "job")
    safe_job = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in job_id)[:80] or "job"
    safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(output_tag or "final_mix"))[:40] or "final_mix"
    unique_token = uuid.uuid4().hex[:12]
    # Preview/media players on Windows keep the current file handle open.
    # A unique filename per mix avoids overwriting a file still owned by Qt.
    mixed_path = temp_dir / f"{safe_job}.__{safe_tag}__.{unique_token}.m4a"

    internal_processed_path = None
    narration_input = narration_path
    if engine_enabled:
        internal_processed_path = temp_dir / f"{safe_job}.__internal_audio_engine__.{unique_token}.mp3"
        if progress_callback:
            progress_callback(4, f"Internal Audio Engine · {engine_preset}")
        def _engine_progress(percent: int, message: str) -> None:
            if progress_callback:
                progress_callback(min(19, 4 + int(max(0, min(100, percent)) * 0.15)), f"Audio Engine · {message}")
        try:
            engine_result = process_internal_voice(
                narration_path,
                internal_processed_path,
                config={**dict(engine_cfg), "preset": engine_preset, "mode": str(engine_cfg.get("mode") or "auto")},
                progress_callback=_engine_progress,
            )
        except Exception as exc:
            raise MusicMixError(f"Internal Audio Engine failed: {exc}") from exc
        if not internal_processed_path.is_file() or internal_processed_path.stat().st_size < 1024:
            raise MusicMixError("Internal Audio Engine did not create a valid narration output.")
        narration_input = internal_processed_path
        if isinstance(metadata, dict):
            metadata["acs_internal_audio_engine_result"] = {
                "version": engine_result.get("version"),
                "preset": engine_result.get("preset"),
                "settings_fingerprint": engine_result.get("settings_fingerprint"),
                "qc": engine_result.get("qc"),
            }
    if progress_callback:
        progress_callback(20 if engine_enabled else 5, "Preparing narration audio...")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(narration_input)]
    if music_enabled:
        cmd.extend(["-i", str(assigned_music)])

    filters: list[str] = []
    voice_filters = _voice_filter_chain(settings)
    if extra_end_seconds > 0.0:
        voice_filters.append(f"apad=pad_dur={extra_end_seconds:.3f}")
    voice_chain = ",".join(voice_filters)
    filters.append(f"[0:a]{voice_chain},atrim=0:{target_duration:.3f},asetpts=PTS-STARTPTS[voice_processed]")

    final_label = "voice_processed"
    if music_enabled:
        if progress_callback:
            progress_callback(15, f"Preparing music: {assigned_music.name}")
        try:
            music_duration = get_audio_duration(str(assigned_music))
        except Exception as exc:
            raise MusicMixError(f"Could not read assigned music track: {assigned_music.name}: {exc}") from exc
        loop_statements, music_raw_label = _looped_music_graph(
            input_label="1:a",
            music_duration=music_duration,
            target_duration=target_duration,
            loop_enabled=bool(getattr(settings, "music_loop_if_short", True)),
            crossfade_seconds=float(getattr(settings, "music_loop_crossfade_seconds", 0.5) or 0.5),
        )
        filters.extend(loop_statements)

        music_filters: list[str] = []
        if bool(getattr(settings, "music_normalize", True)):
            music_filters.append("loudnorm=I=-24:TP=-2:LRA=11")
        music_volume = max(0.0, min(1.0, float(getattr(settings, "music_volume_percent", 12) or 12) / 100.0))
        music_filters.append(f"volume={music_volume:.5f}")
        fade_in = max(0.0, min(target_duration / 2.0, _shared_music_number("music_fade_in", float(getattr(settings, "music_fade_in_seconds", 1.5) or 0.0))))
        fade_out = max(0.0, min(target_duration / 2.0, _shared_music_number("music_fade_out", float(getattr(settings, "music_fade_out_seconds", 2.0) or 0.0))))
        if fade_in > 0:
            music_filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            music_filters.append(f"afade=t=out:st={max(0.0, target_duration - fade_out):.3f}:d={fade_out:.3f}")
        music_filters.extend([f"atrim=0:{target_duration:.3f}", "asetpts=PTS-STARTPTS"])
        filters.append(f"[{music_raw_label}]{','.join(music_filters)}[music_bed]")

        if _shared_duck_enabled(bool(getattr(settings, "music_ducking_enabled", True))):
            threshold, ratio, attack, release = _duck_values(_shared_duck_strength(str(getattr(settings, "music_ducking_strength", "medium") or "medium")))
            filters.append("[voice_processed]asplit=2[voice_mix][voice_side]")
            filters.append(
                f"[music_bed][voice_side]sidechaincompress=threshold={threshold:.5f}:ratio={ratio:.2f}:"
                f"attack={attack:.1f}:release={release:.1f}[music_ducked]"
            )
            filters.append(
                "[voice_mix][music_ducked]amix=inputs=2:duration=first:dropout_transition=2,"
                "alimiter=limit=0.95:attack=5:release=50[final_audio]"
            )
        else:
            filters.append(
                "[voice_processed][music_bed]amix=inputs=2:duration=first:dropout_transition=2,"
                "alimiter=limit=0.95:attack=5:release=50[final_audio]"
            )
        final_label = "final_audio"
    else:
        filters.append("[voice_processed]alimiter=limit=0.95:attack=5:release=50[final_audio]")
        final_label = "final_audio"

    cmd.extend([
        "-filter_complex", ";".join(filters),
        "-map", f"[{final_label}]",
        "-t", f"{target_duration:.3f}",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(mixed_path),
    ])
    if progress_callback:
        progress_callback(25, "Starting final audio mix...")
    try:
        _run_ffmpeg(cmd, progress_callback=progress_callback, cancel_callback=cancel_callback)
        if not mixed_path.exists() or mixed_path.stat().st_size < 4096:
            raise MusicMixError("Audio mixer did not create a valid output file.")
        mixed_duration = get_audio_duration(str(mixed_path))
        if abs(mixed_duration - target_duration) > max(1.5, target_duration * 0.04):
            raise MusicMixError(
                f"Mixed audio duration is invalid ({mixed_duration:.1f}s; expected {target_duration:.1f}s)."
            )
        if progress_callback:
            progress_callback(100, "Final audio mix ready.")
        return AudioMixResult(str(mixed_path), True, mixed_duration, music_enabled)
    except Exception:
        try:
            mixed_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    finally:
        if internal_processed_path is not None:
            try:
                internal_processed_path.unlink(missing_ok=True)
            except Exception:
                pass


def cleanup_temporary_audio(path: str | Path | None) -> None:
    if not path:
        return
    candidate = Path(path)
    try:
        name = candidate.name
        is_owned_temp = (".__final_mix__." in name or ".__preview_mix__." in name) and name.endswith(".m4a")
        if candidate.exists() and is_owned_temp:
            for attempt in range(6):
                try:
                    candidate.unlink()
                    break
                except PermissionError:
                    if attempt >= 5:
                        return
                    time.sleep(0.15 * (attempt + 1))
        parent = candidate.parent
        if parent.name == "_acs_temp" and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except Exception:
        pass

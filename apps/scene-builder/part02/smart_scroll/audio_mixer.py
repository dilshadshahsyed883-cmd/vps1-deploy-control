"""Narration processing, one-track music rotation mix, and waveform ducking.

The mixer runs in a worker thread and produces a temporary AAC audio file.
The video renderer then consumes that validated file while caption timing keeps
using the original narration audio.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .ffmpeg_utils import FFmpegError, get_audio_duration, require_ffmpeg

ProgressCallback = Callable[[int, str], None]
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
    raw_voice_volume = getattr(settings, "voice_volume_percent", 100)
    volume = max(0.0, min(3.0, float(100 if raw_voice_volume is None else raw_voice_volume) / 100.0))
    if abs(volume - 1.0) > 0.0001:
        chain.append(f"volume={volume:.5f}")

    preset = str(getattr(settings, "voice_processing_preset", "natural") or "natural").strip().lower()
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

    # A sample loop is deterministic and keeps the bed present for every frame
    # of narration, including both spoken and silent regions. This deliberately
    # avoids silence-triggered music placement.
    sample_count = max(1, int(round(music_duration * 48000)))
    statements.append(
        f"[{input_label}]{base_format},aloop=loop=-1:size={sample_count},"
        f"atrim=0:{target_duration:.3f},asetpts=PTS-STARTPTS[music_raw]"
    )
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
                progress_callback(55, "Mixing narration and background music...")
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


def _resolved_playlist(settings) -> list[dict[str, Any]]:
    metadata = dict(getattr(settings, "metadata", {}) or {})
    rows = [dict(x) for x in metadata.get("resolved_music_playlist", []) if isinstance(x, dict)]
    valid = []
    for index, row in enumerate(sorted(rows, key=lambda x: int(x.get("order", 9999)))):
        path = Path(str(row.get("path") or ""))
        if path.is_file():
            item = dict(row)
            item["path"] = str(path)
            item["order"] = index + 1
            item["role"] = str(item.get("role") or "general").lower()
            valid.append(item)
    # Backward-compatible one-project track.
    if not valid:
        legacy = Path(str(getattr(settings, "music_assigned_path", "") or ""))
        if legacy.is_file():
            valid = [{"asset_id": "legacy", "path": str(legacy), "order": 1, "role": "general", "display_name": legacy.stem}]
    return valid


def _playlist_sequence(settings, tracks: list[dict[str, Any]], target_duration: float, intro_seconds: float) -> list[dict[str, Any]]:
    """Return finite ordered file segments covering the requested timeline.

    Track 1 can be intro-only. Main/general tracks loop in preset order. An outro
    role is reserved for the tail where practical. The sequence is deterministic.
    """
    if not tracks or target_duration <= 0:
        return []
    duration_cache: dict[str, float] = {}
    def dur(row: dict[str, Any]) -> float:
        path = str(row.get("path") or "")
        if path not in duration_cache:
            try: duration_cache[path] = max(0.2, float(get_audio_duration(path)))
            except Exception: duration_cache[path] = 0.0
        return duration_cache[path]
    tracks = [x for x in tracks if dur(x) > 0.0]
    if not tracks:
        return []

    loop = bool(getattr(settings, "music_playlist_loop", True))
    use_intro = bool(getattr(settings, "music_use_track1_for_intro", True)) and intro_seconds > 0
    allow_intro_main = bool(getattr(settings, "music_allow_intro_track_in_main", False))
    intro_track = next((x for x in tracks if x.get("role") == "intro"), tracks[0]) if use_intro else None
    outro_track = next((x for x in tracks if x.get("role") == "outro"), None)
    main_pool = [x for x in tracks if x.get("role") in {"main", "general"}]
    if allow_intro_main and intro_track and intro_track not in main_pool:
        main_pool.insert(0, intro_track)
    if not main_pool:
        main_pool = [x for x in tracks if x is not outro_track and (allow_intro_main or x is not intro_track)]

    sequence: list[dict[str, Any]] = []
    def fill(pool: list[dict[str, Any]], seconds: float, *, boundary_crossfade: bool = True) -> None:
        remaining = max(0.0, seconds)
        if not pool or remaining <= 0:
            return
        cursor = 0
        safety = 0
        while remaining > 0.01 and safety < 128:
            row = pool[cursor % len(pool)]
            length = min(dur(row), remaining)
            if length <= 0.01:
                break
            cross_from_previous = bool(sequence) and (bool(boundary_crossfade) if cursor == 0 else True)
            sequence.append({"path": row["path"], "duration": length, "crossfade_from_previous": cross_from_previous, "asset_id": row.get("asset_id"), "role": row.get("role")})
            remaining -= length
            cursor += 1
            safety += 1
            if not loop and cursor >= len(pool):
                break

    if intro_track is not None:
        # When the intro track is intro-only, cut it exactly at the narration
        # boundary. When it is allowed in the main documentary, let the active
        # copy continue naturally across that boundary instead of restarting it.
        cursor_seconds = 0.0
        while cursor_seconds < intro_seconds - 0.01 and len(sequence) < 128:
            available = dur(intro_track)
            if allow_intro_main:
                length = min(available, target_duration - cursor_seconds)
            else:
                length = min(available, intro_seconds - cursor_seconds)
            if length <= 0.01:
                break
            sequence.append({"path": intro_track["path"], "duration": length, "crossfade_from_previous": bool(sequence), "asset_id": intro_track.get("asset_id"), "role": "intro"})
            cursor_seconds += length
            if not loop and cursor_seconds < intro_seconds - 0.01:
                break
        main_duration = max(0.0, target_duration - cursor_seconds)
        outro_span = min(dur(outro_track), main_duration) if outro_track else 0.0
        # If Track 1 just continued across narration start, begin the remaining
        # playlist at the following main/general track. Track 1 may return on a
        # later loop when the preset explicitly allows it.
        continuation_pool = list(main_pool)
        if allow_intro_main and intro_track in continuation_pool and len(continuation_pool) > 1:
            pos = continuation_pool.index(intro_track)
            continuation_pool = continuation_pool[pos + 1:] + continuation_pool[:pos + 1]
        fill(continuation_pool, max(0.0, main_duration - outro_span), boundary_crossfade=not bool(sequence))
        if outro_track and outro_span > 0.01:
            sequence.append({"path": outro_track["path"], "duration": outro_span, "crossfade_from_previous": True, "asset_id": outro_track.get("asset_id"), "role": "outro"})
    else:
        outro_span = min(dur(outro_track), target_duration) if outro_track else 0.0
        fill(main_pool or tracks, max(0.0, target_duration - outro_span))
        if outro_track and outro_span > 0.01:
            sequence.append({"path": outro_track["path"], "duration": outro_span, "crossfade_from_previous": True, "asset_id": outro_track.get("asset_id"), "role": "outro"})
    return sequence[:128]


def prepare_final_audio(
    settings,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
    max_duration: float | None = None,
    output_tag: str = "final_mix",
) -> AudioMixResult | None:
    narration_path = Path(str(getattr(settings, "audio_path", "") or ""))
    playlist = _resolved_playlist(settings)
    music_enabled = bool(getattr(settings, "music_enabled", False)) and bool(playlist)
    intro_seconds = float(max(0, int(getattr(settings, "intro_duration_seconds", 0) or 0))) if bool(getattr(settings, "intro_enabled", False)) else 0.0
    voice_preset = str(getattr(settings, "voice_processing_preset", "natural") or "natural").lower()
    extra_end_seconds = max(0.0, float(getattr(settings, "extra_end_seconds", 0.0) or 0.0))
    raw_voice_volume = getattr(settings, "voice_volume_percent", 100)
    needs_voice_processing = (
        voice_preset != "off"
        or int(100 if raw_voice_volume is None else raw_voice_volume) != 100
        or int(getattr(settings, "voice_speed_percent", 100) or 100) != 100
        or extra_end_seconds > 0.0
        or intro_seconds > 0.0
    )

    if not narration_path.exists():
        if music_enabled:
            raise MusicMixError("Background music is enabled, but no narration audio is available for mixing.")
        return None
    if not music_enabled and not needs_voice_processing:
        return None
    if cancel_callback and cancel_callback():
        raise AudioMixCancelled("Audio mixing cancelled.")

    ffmpeg = require_ffmpeg()
    original_duration = get_audio_duration(str(narration_path))
    speed = max(0.25, min(4.0, float(getattr(settings, "voice_speed_percent", 100) or 100) / 100.0))
    narration_duration = max(0.2, (original_duration / speed) + extra_end_seconds)
    target_duration = intro_seconds + narration_duration
    if max_duration is not None:
        target_duration = min(target_duration, max(0.5, float(max_duration)))

    output_path = Path(str(getattr(settings, "output_path", "") or "output.mp4"))
    temp_dir = output_path.parent / "_acs_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    job_id = str(getattr(settings, "job_id", "") or output_path.stem or "job")
    safe_job = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in job_id)[:80] or "job"
    safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(output_tag or "final_mix"))[:40] or "final_mix"
    mixed_path = temp_dir / f"{safe_job}.__{safe_tag}__.{uuid.uuid4().hex[:12]}.m4a"

    if progress_callback:
        progress_callback(5, "Preparing narration audio...")
    sequence = _playlist_sequence(settings, playlist, target_duration, intro_seconds) if music_enabled else []
    if music_enabled and not sequence:
        music_enabled = False
    # Acrossfades overlap neighbouring segments, shortening the composite by the
    # fade duration. When looping is enabled, append deterministic main/general
    # material until the effective music duration still covers the final frame.
    if music_enabled and sequence and bool(getattr(settings, "music_playlist_loop", True)):
        crossfade = max(0.0, min(8.0, float(getattr(settings, "music_track_crossfade_seconds", 1.5) or 0.0)))
        def effective_duration(items):
            total = sum(max(0.0, float(x.get("duration", 0.0) or 0.0)) for x in items)
            for idx in range(1, len(items)):
                if bool(items[idx].get("crossfade_from_previous")) and crossfade > 0.01:
                    total -= min(crossfade, max(0.05, float(items[idx-1]["duration"]) / 2.0), max(0.05, float(items[idx]["duration"]) / 2.0))
            return total
        allow_intro_main = bool(getattr(settings, "music_allow_intro_track_in_main", False))
        candidates = [x for x in sequence if x.get("role") in {"main", "general"} or (allow_intro_main and x.get("role") == "intro")]
        if not candidates:
            candidates = [x for x in sequence if x.get("role") != "outro"]
        cursor = 0
        while candidates and effective_duration(sequence) < target_duration - 0.02 and len(sequence) < 128:
            source = dict(candidates[cursor % len(candidates)])
            try:
                full = max(0.2, float(get_audio_duration(str(source["path"]))))
            except Exception:
                full = max(0.2, float(source.get("duration", 0.2) or 0.2))
            source["duration"] = min(full, max(0.2, target_duration - effective_duration(sequence) + crossfade))
            source["crossfade_from_previous"] = True
            sequence.append(source)
            cursor += 1

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(narration_path)]
    for item in sequence:
        cmd.extend(["-i", str(item["path"])])

    filters: list[str] = []
    voice_filters = _voice_filter_chain(settings)
    if extra_end_seconds > 0.0:
        voice_filters.append(f"apad=pad_dur={extra_end_seconds:.3f}")
    filters.append(f"[0:a]{','.join(voice_filters)},atrim=0:{narration_duration:.3f},asetpts=PTS-STARTPTS[voice_core]")
    if intro_seconds > 0.0:
        delay_ms = int(round(intro_seconds * 1000.0))
        filters.append(f"[voice_core]adelay={delay_ms}|{delay_ms},atrim=0:{target_duration:.3f},asetpts=PTS-STARTPTS[voice_processed]")
    else:
        filters.append(f"[voice_core]atrim=0:{target_duration:.3f},asetpts=PTS-STARTPTS[voice_processed]")

    final_label = "voice_processed"
    if music_enabled and sequence:
        if progress_callback:
            progress_callback(15, f"Preparing ordered music playlist ({len(sequence)} segment(s))")
        labels: list[str] = []
        for index, item in enumerate(sequence, start=1):
            label = f"m{index-1}"
            filters.append(
                f"[{index}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"atrim=0:{max(0.05,float(item['duration'])):.3f},asetpts=PTS-STARTPTS[{label}]"
            )
            labels.append(label)
        current = labels[0]
        crossfade = max(0.0, min(8.0, float(getattr(settings, "music_track_crossfade_seconds", 1.5) or 0.0)))
        for index in range(1, len(labels)):
            nxt = labels[index]
            out = f"mx{index}"
            do_cross = bool(sequence[index].get("crossfade_from_previous")) and crossfade > 0.01
            if do_cross:
                d = min(crossfade, max(0.05, float(sequence[index-1]["duration"]) / 2.0), max(0.05, float(sequence[index]["duration"]) / 2.0))
                filters.append(f"[{current}][{nxt}]acrossfade=d={d:.3f}:c1=tri:c2=tri[{out}]")
            else:
                filters.append(f"[{current}][{nxt}]concat=n=2:v=0:a=1[{out}]")
            current = out

        music_filters: list[str] = []
        if bool(getattr(settings, "music_normalize", True)):
            music_filters.append("loudnorm=I=-24:TP=-2:LRA=11")
        raw_music_volume = getattr(settings, "music_volume_percent", 12)
        music_volume = max(0.0, min(1.0, float(12 if raw_music_volume is None else raw_music_volume) / 100.0))
        music_filters.append(f"volume={music_volume:.5f}")
        fade_in = max(0.0, min(target_duration / 2.0, float(getattr(settings, "music_fade_in_seconds", 1.5) or 0.0)))
        fade_out = max(0.0, min(target_duration / 2.0, float(getattr(settings, "music_fade_out_seconds", 2.0) or 0.0)))
        if fade_in > 0:
            music_filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            music_filters.append(f"afade=t=out:st={max(0.0,target_duration-fade_out):.3f}:d={fade_out:.3f}")
        music_filters.extend([f"apad=pad_dur={target_duration:.3f}", f"atrim=0:{target_duration:.3f}", "asetpts=PTS-STARTPTS"])
        filters.append(f"[{current}]{','.join(music_filters)}[music_bed]")

        if bool(getattr(settings, "music_ducking_enabled", True)):
            threshold, ratio, attack, release = _duck_values(str(getattr(settings, "music_ducking_strength", "medium") or "medium"))
            filters.append("[voice_processed]asplit=2[voice_mix][voice_side]")
            filters.append(f"[music_bed][voice_side]sidechaincompress=threshold={threshold:.5f}:ratio={ratio:.2f}:attack={attack:.1f}:release={release:.1f}[music_ducked]")
            filters.append("[voice_mix][music_ducked]amix=inputs=2:duration=longest:dropout_transition=2,alimiter=limit=0.95:attack=5:release=50[final_audio]")
        else:
            filters.append("[voice_processed][music_bed]amix=inputs=2:duration=longest:dropout_transition=2,alimiter=limit=0.95:attack=5:release=50[final_audio]")
        final_label = "final_audio"
    else:
        filters.append("[voice_processed]alimiter=limit=0.95:attack=5:release=50[final_audio]")
        final_label = "final_audio"

    cmd.extend([
        "-filter_complex", ";".join(filters), "-map", f"[{final_label}]",
        "-t", f"{target_duration:.3f}", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(mixed_path),
    ])
    if progress_callback:
        progress_callback(25, "Starting final audio mix...")
    try:
        _run_ffmpeg(cmd, progress_callback=progress_callback, cancel_callback=cancel_callback)
        if not mixed_path.exists() or mixed_path.stat().st_size < 4096:
            raise MusicMixError("Audio mixer did not create a valid output file.")
        mixed_duration = get_audio_duration(str(mixed_path))
        if abs(mixed_duration - target_duration) > max(1.5, target_duration * 0.04):
            raise MusicMixError(f"Mixed audio duration is invalid ({mixed_duration:.1f}s; expected {target_duration:.1f}s).")
        if progress_callback:
            progress_callback(100, "Final audio mix ready.")
        return AudioMixResult(str(mixed_path), True, mixed_duration, music_enabled)
    except Exception:
        try: mixed_path.unlink(missing_ok=True)
        except Exception: pass
        raise

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

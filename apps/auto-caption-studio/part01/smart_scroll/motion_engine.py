"""Deterministic lightweight motion and visual ordering helpers.

The module deliberately avoids image AI. It provides reproducible cinematic
motion paths, low-resolution zoom protection, subtle easing and stable library
ordering so queue retries/restarts produce the same video.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import random
import re
from pathlib import Path
from typing import Iterable


MOTION_TYPES = (
    "pan_left_to_right",
    "pan_right_to_left",
    "pan_top_to_bottom",
    "pan_bottom_to_top",
    "diagonal_down_right",
    "diagonal_up_left",
    "zoom_in",
    "zoom_out",
    "tiny_drift",
)


@dataclass(frozen=True)
class MotionPlan:
    kind: str
    start_zoom: float
    end_zoom: float
    start_x: float
    end_x: float
    start_y: float
    end_y: float


def stable_seed(*parts: object) -> int:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8", errors="replace")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return value * value * (3.0 - 2.0 * value)


def smootherstep(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)


def eased_progress(value: float, style: str = "cinematic") -> float:
    style = (style or "cinematic").strip().lower()
    if style == "dynamic":
        return smoothstep(value)
    return smootherstep(value)


def _preset_values(style: str, intensity_percent: int) -> tuple[float, float]:
    style = (style or "cinematic").strip().lower()
    intensity = max(0.0, min(1.0, float(intensity_percent or 0) / 100.0))
    if style == "very_slow":
        return 0.025 + 0.030 * intensity, 0.28 + 0.22 * intensity
    if style == "dynamic":
        return 0.065 + 0.075 * intensity, 0.62 + 0.32 * intensity
    return 0.040 + 0.045 * intensity, 0.43 + 0.28 * intensity


def _quality_zoom_factor(source_size: tuple[int, int], target_size: tuple[int, int]) -> float:
    sw, sh = max(1, source_size[0]), max(1, source_size[1])
    tw, th = max(1, target_size[0]), max(1, target_size[1])
    # If the source is already being upscaled to cover the target, extra zoom is
    # reduced. High-resolution sources keep the full cinematic range.
    native_ratio = min(float(sw) / float(tw), float(sh) / float(th))
    if native_ratio >= 1.35:
        return 1.0
    if native_ratio >= 1.0:
        return 0.72 + (native_ratio - 1.0) * 0.8
    return max(0.15, native_ratio * 0.55)


def _motion_kind(seed: int, index: int, portrait: bool, avoid_repeats: bool = True) -> str:
    pool = list(MOTION_TYPES)
    if portrait:
        # Portrait images normally have less horizontal crop room.
        pool = [
            "pan_top_to_bottom",
            "pan_bottom_to_top",
            "zoom_in",
            "zoom_out",
            "tiny_drift",
            "diagonal_down_right",
        ]
    rng = random.Random(stable_seed(seed, index, "motion-kind"))
    chosen = pool[rng.randrange(len(pool))]
    if avoid_repeats and index > 0:
        previous_rng = random.Random(stable_seed(seed, index - 1, "motion-kind"))
        previous_pool = list(MOTION_TYPES) if not portrait else [
            "pan_top_to_bottom", "pan_bottom_to_top", "zoom_in", "zoom_out", "tiny_drift", "diagonal_down_right"
        ]
        previous = previous_pool[previous_rng.randrange(len(previous_pool))]
        if chosen == previous:
            chosen = pool[(pool.index(chosen) + 1 + (seed % max(1, len(pool) - 1))) % len(pool)]
    return chosen


def build_motion_plan(
    *,
    seed: int,
    index: int,
    style: str,
    intensity_percent: int,
    source_size: tuple[int, int],
    target_size: tuple[int, int],
    segment_duration: float,
    quality_protection: bool = True,
    avoid_repeats: bool = True,
) -> MotionPlan:
    sw, sh = max(1, source_size[0]), max(1, source_size[1])
    portrait = sh > sw * 1.08
    zoom_amp, pan_strength = _preset_values(style, intensity_percent)
    if quality_protection:
        zoom_amp *= _quality_zoom_factor(source_size, target_size)

    # Short visual slots should not race through an exaggerated move. Long
    # slots may use the full path, but remain subtle for story videos.
    duration_factor = max(0.55, min(1.0, float(segment_duration or 8.0) / 7.0))
    zoom_amp *= duration_factor
    pan_strength *= max(0.65, duration_factor)
    kind = _motion_kind(seed, index, portrait, avoid_repeats)

    start_zoom = 1.0
    end_zoom = 1.0 + zoom_amp
    start_x = end_x = start_y = end_y = 0.0

    if kind == "pan_left_to_right":
        start_zoom = end_zoom = 1.0 + zoom_amp * 0.72
        start_x, end_x = -pan_strength, pan_strength
    elif kind == "pan_right_to_left":
        start_zoom = end_zoom = 1.0 + zoom_amp * 0.72
        start_x, end_x = pan_strength, -pan_strength
    elif kind == "pan_top_to_bottom":
        start_zoom = end_zoom = 1.0 + zoom_amp * 0.72
        start_y, end_y = -pan_strength, pan_strength
    elif kind == "pan_bottom_to_top":
        start_zoom = end_zoom = 1.0 + zoom_amp * 0.72
        start_y, end_y = pan_strength, -pan_strength
    elif kind == "diagonal_down_right":
        start_zoom = 1.0 + zoom_amp * 0.25
        end_zoom = 1.0 + zoom_amp
        start_x, end_x = -pan_strength * 0.68, pan_strength * 0.68
        start_y, end_y = -pan_strength * 0.55, pan_strength * 0.55
    elif kind == "diagonal_up_left":
        start_zoom = 1.0 + zoom_amp
        end_zoom = 1.0 + zoom_amp * 0.25
        start_x, end_x = pan_strength * 0.68, -pan_strength * 0.68
        start_y, end_y = pan_strength * 0.55, -pan_strength * 0.55
    elif kind == "zoom_out":
        start_zoom, end_zoom = 1.0 + zoom_amp, 1.0
    elif kind == "tiny_drift":
        start_zoom = end_zoom = 1.0 + zoom_amp * 0.45
        start_x, end_x = -pan_strength * 0.22, pan_strength * 0.22
        start_y, end_y = pan_strength * 0.16, -pan_strength * 0.16
    # zoom_in uses the defaults.

    return MotionPlan(kind, start_zoom, end_zoom, start_x, end_x, start_y, end_y)


def interpolate_plan(plan: MotionPlan, progress: float, style: str) -> tuple[float, float, float]:
    p = eased_progress(progress, style)
    zoom = plan.start_zoom + (plan.end_zoom - plan.start_zoom) * p
    x = plan.start_x + (plan.end_x - plan.start_x) * p
    y = plan.start_y + (plan.end_y - plan.start_y) * p
    return zoom, x, y


def _filename_family(path: str | Path) -> str:
    stem = Path(path).stem.casefold()
    stem = re.sub(r"[\s_-]*\d+[a-z]?$", "", stem)
    stem = re.sub(r"[\s_-]+", " ", stem).strip()
    return stem or Path(path).stem.casefold()


def deterministic_visual_order(paths: Iterable[str | Path], seed: int, randomize: bool) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        key = str(path.resolve() if path.exists() else path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    if not randomize or len(unique) < 2:
        return unique

    rng = random.Random(stable_seed(seed, "visual-order", len(unique)))
    rng.shuffle(unique)

    # Lightweight anti-repetition pass: adjacent numbered variants from the
    # same filename family are separated when another family is available.
    for index in range(1, len(unique)):
        if _filename_family(unique[index]) != _filename_family(unique[index - 1]):
            continue
        swap_at = next(
            (candidate for candidate in range(index + 1, len(unique)) if _filename_family(unique[candidate]) != _filename_family(unique[index - 1])),
            None,
        )
        if swap_at is not None:
            unique[index], unique[swap_at] = unique[swap_at], unique[index]
    return unique


def transition_name(mode: str, seed: int, index: int) -> str:
    mode = (mode or "crossfade").strip().lower()
    if mode != "varied":
        return mode
    choices = ("crossfade", "dissolve", "cut")
    return choices[stable_seed(seed, index, "transition") % len(choices)]

"""Narration-aware visual duration calculation with a safe fixed fallback."""
from __future__ import annotations

from typing import Any


def _caption_regions(metadata: dict[str, Any], minimum: float, maximum: float) -> list[float]:
    raw = list(metadata.get("caption_segments", []) or [])
    if not raw:
        return []
    regions: list[float] = []
    region_start: float | None = None
    region_end = 0.0
    for item in raw:
        try:
            start = max(0.0, float(item.get("start", 0.0) or 0.0))
            end = max(start, float(item.get("end", start) or start))
        except Exception:
            continue
        if region_start is None:
            region_start, region_end = start, end
            continue
        candidate = max(0.0, end - region_start)
        # Keep natural caption groups together until the minimum visual length,
        # but split before a visual becomes stagnant.
        if candidate <= maximum and (region_end - region_start) < minimum:
            region_end = end
            continue
        duration = max(minimum, min(maximum, region_end - region_start))
        regions.append(duration)
        region_start, region_end = start, end
    if region_start is not None:
        regions.append(max(minimum, min(maximum, region_end - region_start)))
    return regions


def _scene_plan_durations(metadata: dict[str, Any], total_duration: float) -> list[float]:
    scene_plan = list(metadata.get("scene_sync_plan", []) or [])
    durations: list[float] = []
    for item in scene_plan:
        try:
            start = max(0.0, float(item.get("start", 0.0) or 0.0))
            end = max(start, float(item.get("end", start) or start))
            if end > start:
                durations.append(max(0.01, end - start))
                continue
            duration = max(0.0, float(item.get("duration", 0.0) or 0.0))
            if duration > 0.0:
                durations.append(duration)
        except Exception:
            continue
    if durations:
        return durations
    count = max(0, len(scene_plan))
    if count <= 0:
        return []
    equal = max(1.0, total_duration / count)
    return [equal] * count



def build_visual_durations(settings, path_count: int, fixed_interval: float) -> list[float]:
    path_count = max(0, int(path_count))
    if path_count == 0:
        return []
    fixed = max(1.0, float(fixed_interval or 8.0))
    mode = str(getattr(settings, "image_timing_mode", "fixed") or "fixed").strip().lower()
    if mode != "narration":
        return [fixed] * path_count

    minimum = max(1.0, float(getattr(settings, "image_timing_min_seconds", 4.0) or 4.0))
    maximum = max(minimum, float(getattr(settings, "image_timing_max_seconds", 10.0) or 10.0))
    metadata = dict(getattr(settings, "metadata", {}) or {})
    total_duration = max(1.0, float(getattr(settings, "duration_seconds", 30.0) or 30.0))

    # Preferred narration timing path: explicit scene plan durations built from
    # Whisper/script alignment. In narration mode we preserve those durations as-is
    # and do not clamp them with the fixed min/max controls.
    scene_durations = _scene_plan_durations(metadata, total_duration)
    if scene_durations:
        if len(scene_durations) < path_count:
            scene_durations.extend([scene_durations[-1]] * (path_count - len(scene_durations)))
        return scene_durations[:path_count]

    regions = _caption_regions(metadata, minimum, maximum)
    if not regions:
        return [fixed] * path_count
    return [regions[index % len(regions)] for index in range(path_count)]

"""Director V25 voice-aligned shot timing projection (opt-in only).

Preserve exactly the authored shot identities, narration and media choices while
fitting Whisper's timings into the <=3-second visual cap. Unlike the Classic
normalizer this never invents, splits, merges or reorders a shot.
"""
from __future__ import annotations

from collections.abc import Iterable
import math
from typing import Any


class DirectorTimingError(ValueError):
    """Spoken audio cannot be represented with the authored visual shot count."""


def fit_director_shot_timing(
    scenes: Iterable[dict[str, Any]],
    *,
    audio_duration: float,
    narration_start: float = 0.0,
    max_shot_seconds: float = 3.0,
    min_shot_seconds: float = 0.05,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fit ordered Whisper boundaries while retaining every authored shot.

    Every boundary is constrained by remaining shots' duration budgets, so
    the audio end is covered without automatic shot splitting or merging.
    """
    plan = [dict(row) for row in scenes]
    count = len(plan)
    if not count:
        raise DirectorTimingError("Director V25 has no authored narration shots")
    if len({str(row.get("director_v25_shot") or "") for row in plan}) != count or any(
        not str(row.get("director_v25_shot") or "") for row in plan
    ):
        raise DirectorTimingError("Director V25 shot identity missing or duplicated before timing")
    total = float(audio_duration)
    start = float(narration_start)
    cap = float(max_shot_seconds)
    floor = float(min_shot_seconds)
    if not all(math.isfinite(n) for n in (total, start, cap, floor)) or total <= start or start < 0 or not 0 < floor <= cap:
        raise DirectorTimingError("Director V25 audio duration/start or shot duration limits are invalid")
    span = total - start
    if span > count * cap + 0.0001:
        raise DirectorTimingError(
            f"Director V25 narration is {span:.2f}s but {count} authored shots can cover "
            f"at most {count * cap:.2f}s at {cap:.1f}s each. "
            "Add explicit shots or shorten the narration audio; media cannot silently repeat or split."
        )
    if span < count * floor - 0.0001:
        raise DirectorTimingError(
            f"Director V25 narration is {span:.2f}s, too short for {count} "
            f"authored shots at the {floor:.2f}s minimum."
        )
    original_endpoints: list[float] = []
    old_end = start
    for row in plan:
        a, b = float(row.get("start", math.nan)), float(row.get("end", math.nan))
        if not all(math.isfinite(n) for n in (a, b)) or a < old_end - .003 or b <= a or b > total + .003:
            raise DirectorTimingError(f"Director V25 Whisper timing invalid at {row['director_v25_shot']}")
        old_end = b
        original_endpoints.append(b)
    if abs(old_end - total) > .003:
        raise DirectorTimingError("Director V25 Whisper timing does not reach the end of the audio")

    cursor = start
    updated: list[dict[str, Any]] = []
    moved = 0
    largest_shift = 0.0
    for idx, row in enumerate(plan):
        remaining = count - idx - 1
        lower = max(cursor + floor, total - remaining * cap)
        upper = min(cursor + cap, total - remaining * floor)
        if upper + 0.00001 < lower:
            raise DirectorTimingError(f"Director V25 timing infeasible near {row['director_v25_shot']}")
        endpoint = total if remaining == 0 else min(upper, max(lower, original_endpoints[idx]))
        endpoint = round(endpoint, 3)
        shot_start = round(cursor, 3)
        shot_len = round(endpoint - shot_start, 3)
        if shot_len > cap + 0.001 or shot_len < floor - 0.001:
            raise DirectorTimingError(f"Director V25 duration constraints failed at {row['director_v25_shot']}")
        shift = abs(endpoint - original_endpoints[idx])
        if shift > .002:
            moved += 1
        largest_shift = max(largest_shift, shift)
        row.update(
            start=shot_start, end=endpoint, duration=shot_len,
            matched_end=endpoint, timing_validated=True,
            source_scene_parts=1, source_scene_part=1,
            media_duration_stage="director_v25",
            media_duration_min_seconds=round(floor, 3),
            media_duration_max_seconds=round(cap, 3),
        )
        if shift > .002:
            row["timing_source"] = str(row.get("timing_source") or "whisper") + "_director_duration_fit"
        updated.append(row)
        cursor = endpoint
    report = {
        "authored_shots": count,
        "fitted_shots": len(updated),
        "max_duration_seconds": cap,
        "shifted_whisper_boundaries": moved,
        "max_boundary_shift_seconds": round(largest_shift, 3),
        "narration_span_seconds": round(span, 3),
        "automatic_split_merge": False,
    }
    return updated, report

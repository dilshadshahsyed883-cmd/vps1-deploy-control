"""Exact Director V25 transitions: no editorial preset, auto transitions or silent substitution."""
from __future__ import annotations

from typing import Any
from smart_scroll.transition_engine import (
    TRANSITION_CATALOG, resolve_xfade_transition, transition_uses_xfade,
)


class DirectorTransitionError(ValueError):
    pass


def resolve_director_transitions(scenes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not scenes:
        raise DirectorTransitionError("Director V25 requires authored shots")
    out = []
    for i, authored in enumerate(scenes):
        row = dict(authored)
        shot = str(row.get("director_v25_shot") or "")
        if not shot:
            raise DirectorTransitionError("Director V25 transition has no authored shot ID")
        transition = str(row.get("transition_out") or "").strip()
        if transition not in TRANSITION_CATALOG:
            raise DirectorTransitionError(f"{shot}: unknown TXT transition {transition!r}")
        try:
            duration_ms = int(row.get("transition_duration_ms") or 0)
        except (ValueError, TypeError) as exc:
            raise DirectorTransitionError(f"{shot}: invalid TXT transition duration") from exc
        if duration_ms < 0 or duration_ms > 800:
            raise DirectorTransitionError(f"{shot}: requested transition must be between 0 and 800ms")
        if i == len(scenes) - 1 and transition not in {"none", "hard_cut"}:
            raise DirectorTransitionError(f"{shot}: final narration shot cannot transition to a missing next shot")
        if transition in {"none", "hard_cut"}:
            if duration_ms != 0:
                raise DirectorTransitionError(f"{shot}: {transition} must have zero transition duration")
            row.update(transition_engine="cut", transition_xfade=None,
                       transition_variant=None, transition_duration_ms=0)
        else:
            if duration_ms < 100:
                raise DirectorTransitionError(
                    f"{shot}: {transition} requires explicit 0.10–0.80s duration in TXT"
                )
            current = float(row.get("duration") or 0.0)
            following = float(scenes[i+1].get("duration") or 0.0)
            if duration_ms/1000.0 >= min(current, following) - 0.01:
                raise DirectorTransitionError(
                    f"{shot}: {transition} duration {duration_ms}ms exceeds adjacent "
                    f"shot hold ({current:.2f}s, {following:.2f}s). Fix TXT or timing beats."
                )
            if not transition_uses_xfade(transition):
                raise DirectorTransitionError(f"{shot}: unsupported xfade transition")
            variant = resolve_xfade_transition(transition, i, str(row.get("transition_variant") or ""))
            row.update(transition_engine="xfade", transition_xfade=variant,
                       transition_variant=variant, transition_duration_ms=duration_ms)
        row["transition_out"] = transition
        row["transition_auto"] = False
        row["transition_intensity"] = 0.0
        row["transition_sfx"] = "none"
        row["sfx_volume"] = 0.0
        row["technical_resolution_source"] = "director_v25_txt_only"
        out.append(row)
    return out, {
        "mode": "director_v25_exact",
        "authority": "director_txt",
        "shot_count": len(out),
        "auto_transitions": 0,
        "preset_used": False,
    }

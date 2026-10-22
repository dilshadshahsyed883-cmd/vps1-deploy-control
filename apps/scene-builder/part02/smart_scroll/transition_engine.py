from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

# Named transitions render as visually distinct FFmpeg effects.  Do not silently
# replace unavailable styles with a generic fade: an unsupported FFmpeg build
# must fail the guarded render so the selected creative intent is preserved.
SAFE_XFADE_TRANSITIONS = frozenset({
    "fade", "fadeslow", "fadefast", "fadeblack", "fadewhite", "fadegrays",
    "dissolve", "zoomin", "hblur", "radial", "custom",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "coverleft", "coverright", "hlwind", "hrwind",
})
XFADE_COMPAT_ALIASES = {
    "revealleft": "wipeleft", "revealright": "wiperight",
    "revealup": "wipeup", "revealdown": "wipedown",
    "vuwind": "slideup", "vdwind": "slidedown",
}

TRANSITION_CATALOG: dict[str, dict[str, Any]] = {
    "none": {"label": "Off / No Transition", "category": "clean", "engine": "cut", "sfx": "none"},
    "hard_cut": {"label": "Hard Cut", "category": "clean", "engine": "cut", "sfx": "none"},
    "cross_dissolve": {"label": "Cross Dissolve", "category": "soft", "xfade": "fade", "sfx": "none"},
    "film_fade": {"label": "Film Fade", "category": "soft", "xfade": "fadeslow", "sfx": "none"},
    "smooth_zoom": {"label": "Smooth Zoom", "category": "dynamic", "xfade": "zoomin", "sfx": "riser"},
    "push": {"label": "Push", "category": "dynamic", "xfade_variants": ("coverleft", "coverright"), "sfx": "soft_whoosh"},
    "slide": {"label": "Slide", "category": "dynamic", "xfade_variants": ("slideleft", "slideright", "slideup", "slidedown"), "sfx": "soft_whoosh"},
    "whip_pan": {"label": "Whip Pan", "category": "dynamic", "xfade_variants": ("hlwind", "hrwind"), "sfx": "fast_whoosh"},
    "blur_dissolve": {"label": "Blur Dissolve", "category": "soft", "xfade": "hblur", "sfx": "soft_whoosh"},
    "light_flash": {"label": "Light Flash", "category": "special", "xfade": "fadefast", "sfx": "impact"},
    "luma_fade": {"label": "Luma Fade", "category": "special", "xfade": "dissolve", "sfx": "none"},
    "directional_wipe": {"label": "Directional Wipe", "category": "special", "xfade_variants": ("wipeleft", "wiperight", "wipeup", "wipedown"), "sfx": "fast_whoosh"},
    "soft_spin": {"label": "Soft Spin", "category": "special", "xfade": "custom", "sfx": "riser"},
    "zoom_blur": {"label": "Zoom Blur", "category": "dynamic", "xfade": "custom", "sfx": "riser"},
    "dip_white": {"label": "Dip to White", "category": "special", "xfade": "fadewhite", "sfx": "none"},
    "dip_black": {"label": "Dip to Black", "category": "special", "xfade": "fadeblack", "sfx": "none"},
}

TRANSITION_UI = tuple((key, spec["label"]) for key, spec in TRANSITION_CATALOG.items())
CANONICAL_TRANSITIONS = tuple(TRANSITION_CATALOG)

LEGACY_ALIASES = {
    "cut": "hard_cut",
    "fade": "film_fade",
    "dissolve": "cross_dissolve",
    "zoom": "smooth_zoom",
    "blur": "blur_dissolve",
    "wipe_left": "directional_wipe",
    "wipe_right": "directional_wipe",
    "smooth_left": "whip_pan",
    # The old circle/pixel family is deliberately retired from automatic use.
    "circle_open": "cross_dissolve",
    "circleopen": "cross_dissolve",
    "pixelize": "cross_dissolve",
    "pixel": "cross_dissolve",
    "black_dots": "cross_dissolve",
    "black-dots": "cross_dissolve",
}

PRESETS: dict[str, dict[str, float]] = {
    # Automatic presets deliberately use a restrained editorial vocabulary.
    # Flash, spin, whip and novelty wipes remain available to legacy/manual scene
    # plans, but are never selected by the professional director.
    "documentary": {
        "hard_cut": 68,
        "cross_dissolve": 23,
        "film_fade": 5,
        "push": 2,
        "dip_black": 2,
    },
    "cinematic": {
        "hard_cut": 58,
        "cross_dissolve": 27,
        "film_fade": 7,
        "push": 4,
        "dip_black": 3,
        "dip_white": 1,
    },
    "smooth": {
        "hard_cut": 36,
        "cross_dissolve": 48,
        "film_fade": 11,
        "push": 2,
        "dip_black": 3,
    },
    "energetic": {
        "hard_cut": 72,
        "cross_dissolve": 12,
        "film_fade": 3,
        "push": 10,
        "dip_black": 2,
        "dip_white": 1,
    },
    "minimal": {
        "hard_cut": 76,
        "cross_dissolve": 19,
        "film_fade": 4,
        "dip_black": 1,
    },
    "storytelling": {
        "hard_cut": 47,
        "cross_dissolve": 37,
        "film_fade": 9,
        "push": 2,
        "dip_black": 4,
        "dip_white": 1,
    },
}
PRESET_LABELS = {
    "documentary": "Editorial Documentary",
    "cinematic": "Cinematic — Balanced",
    "smooth": "Smooth",
    "energetic": "Energetic / Hook",
    "minimal": "Minimal",
    "storytelling": "Storytelling",
}


def _slug(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def canonical_transition(value: Any) -> str:
    key = _slug(value) or "cross_dissolve"
    key = LEGACY_ALIASES.get(key, key)
    return key if key in TRANSITION_CATALOG else "cross_dissolve"


def transition_label(value: Any) -> str:
    return str(TRANSITION_CATALOG[canonical_transition(value)]["label"])


def _stable_int(*parts: Any) -> int:
    payload = "|".join(str(x) for x in parts).encode("utf-8", errors="replace")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def sanitize_xfade_transition(value: Any) -> str:
    """Return an xfade name safe for older deployed FFmpeg builds."""
    name = _slug(value) or "fade"
    name = XFADE_COMPAT_ALIASES.get(name, name)
    if name not in SAFE_XFADE_TRANSITIONS:
        raise ValueError(f"Unsupported xfade effect: {name}; a generic fade is not an acceptable substitute")
    return name


def transition_uses_xfade(value: Any) -> bool:
    """True only for boundaries that require an actual overlapping transition."""
    return canonical_transition(value) not in {"none", "hard_cut"}


def resolve_xfade_transition(value: Any, boundary_index: int = 0, variant_hint: str = "") -> str:
    key = canonical_transition(value)
    if not transition_uses_xfade(key):
        raise ValueError(f"{key} is a cut boundary and must not be routed through xfade")
    spec = TRANSITION_CATALOG[key]
    variants = tuple(spec.get("xfade_variants") or ())
    if variants:
        hint = sanitize_xfade_transition(variant_hint) if str(variant_hint or "").strip() else ""
        if hint and hint in variants:
            return hint
        chosen = variants[_stable_int(key, boundary_index, "xfade") % len(variants)]
        return sanitize_xfade_transition(chosen)
    return sanitize_xfade_transition(spec.get("xfade") or "fade")




def _transition_from_intent(value: Any) -> str:
    intent = _slug(value)
    if intent in {"none", "off", "no_transition"}:
        return "none"
    if intent in {"cut", "hard_cut", "clean", "direct", "continuity", "neutral"}:
        return "hard_cut"
    if intent in {"soft", "gentle", "smooth", "dissolve", "cross_dissolve"}:
        return "cross_dissolve"
    if intent in {"section", "chapter", "reflective", "fade", "film_fade"}:
        return "film_fade"
    if intent in {"emphasis", "dynamic", "push", "energy"}:
        return "push"
    return "cross_dissolve"

def suggested_sfx(value: Any) -> str:
    return str(TRANSITION_CATALOG[canonical_transition(value)].get("sfx") or "none")


def _duration(scene: dict[str, Any]) -> float:
    try:
        return max(0.0, float(scene.get("end", 0) or 0) - float(scene.get("start", 0) or 0))
    except Exception:
        return 0.0


def _weighted_pick(weights: dict[str, float], token: int, excluded: set[str] | None = None) -> str:
    excluded = excluded or set()
    items = [(k, max(0.0, float(v))) for k, v in weights.items() if k not in excluded and k in TRANSITION_CATALOG and v > 0]
    if not items:
        return "hard_cut"
    total = sum(v for _, v in items)
    point = (token % 1_000_003) / 1_000_003.0 * total
    running = 0.0
    for key, weight in items:
        running += weight
        if point <= running:
            return key
    return items[-1][0]


def _adjust_weights(base: dict[str, float], duration: float, intensity: int, in_hook: bool) -> dict[str, float]:
    weights = dict(base)
    # Heavy transitions are poor on 1–2 second scenes.
    if duration <= 1.5:
        return {"hard_cut": 88.0, "cross_dissolve": 12.0}
    if duration <= 2.0:
        return {"hard_cut": 80.0, "cross_dissolve": 17.0, "film_fade": 3.0}
    if duration < 3.0:
        allowed = {"hard_cut", "cross_dissolve", "film_fade", "blur_dissolve", "smooth_zoom", "push", "slide"}
        weights = {k: v for k, v in weights.items() if k in allowed}
        weights["hard_cut"] = weights.get("hard_cut", 0) + 12
        weights["cross_dissolve"] = weights.get("cross_dissolve", 0) + 6

    factor = max(0.35, min(1.75, 0.35 + (intensity / 100.0) * 1.40))
    for key in list(weights):
        category = TRANSITION_CATALOG.get(key, {}).get("category")
        if category in {"dynamic", "special"}:
            weights[key] *= factor

    # Hooks are cut-led.  This keeps the opening confident without looking like
    # a template transition reel.
    if in_hook:
        weights["hard_cut"] = weights.get("hard_cut", 0) + 16
        weights["cross_dissolve"] = weights.get("cross_dissolve", 0) + 2
        for key in ("whip_pan", "push", "smooth_zoom"):
            if key in weights:
                weights[key] *= 1.15
    return weights


def _editorial_cue(scene: dict[str, Any], next_scene: dict[str, Any]) -> str:
    """Classify a boundary using narration structure, not random decoration."""
    left = str(scene.get("text") or scene.get("narration") or "").strip().lower()
    right = str(next_scene.get("text") or next_scene.get("narration") or "").strip().lower()
    chapter_markers = (
        "meanwhile", "years later", "days later", "the next day", "later that",
        "on the other hand", "in contrast", "but everything changed", "chapter",
        "finally", "in the end", "however",
    )
    soft_markers = (
        "remember", "imagine", "perhaps", "for many", "at the same time",
        "as night", "as morning", "today", "once", "long ago",
    )
    if any(marker in right for marker in chapter_markers):
        return "chapter"
    if any(marker in right for marker in soft_markers) or left.endswith(("…", "...")):
        return "soft"
    if left.endswith(("!", "?")) or right.startswith(("but ", "yet ", "now ")):
        return "emphasis"
    return "continuity"


def recommended_duration(key: str, boundary_duration: float, base_seconds: float, intensity: int) -> float:
    key = canonical_transition(key)
    if key in {"none", "hard_cut"}:
        return 0.0
    base = max(0.10, min(0.80, float(base_seconds or 0.28)))
    if boundary_duration <= 1.5:
        cap = 0.16
    elif boundary_duration <= 2.0:
        cap = 0.20
    elif boundary_duration <= 3.0:
        cap = 0.30
    else:
        cap = 0.45
    category = TRANSITION_CATALOG[key].get("category")
    multiplier = {"soft": 1.0, "dynamic": 0.90, "special": 0.82, "clean": 0.8}.get(str(category), 1.0)
    intensity_scale = 0.82 + max(0, min(100, int(intensity))) / 100.0 * 0.30
    return max(0.10, min(cap, base * multiplier * intensity_scale))


def assign_auto_transitions(
    scenes: list[dict[str, Any]],
    *,
    mode: str = "auto",
    preset: str = "cinematic",
    intensity: int = 45,
    base_seconds: float = 0.28,
    project_seed: str = "",
    hook_seconds: float = 60.0,
    no_repeat: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    mode = _slug(mode) or "auto"
    if mode == "auto_editorial":
        mode = "auto"
    elif mode == "scene_plan":
        mode = "manual"
    preset = _slug(preset) if _slug(preset) in PRESETS else "cinematic"
    out = [dict(scene) for scene in scenes]
    previous = ""
    special_history: list[int] = []

    for i, scene in enumerate(out):
        if i >= len(out) - 1:
            scene["transition_out"] = "none"
            scene["transition_duration_ms"] = 0
            scene["transition_xfade"] = None
            scene["transition_variant"] = None
            scene["transition_engine"] = "cut"
            scene["transition_sfx"] = "none"
            scene["sfx_volume"] = 0.0
            scene["transition_auto"] = mode == "auto"
            continue

        current_duration = _duration(scene)
        next_duration = _duration(out[i + 1])
        boundary_duration = max(0.01, min(current_duration or 4.0, next_duration or 4.0))

        same_source_split = (
            str(scene.get("source_scene_code") or "")
            and str(scene.get("source_scene_code") or "") == str(out[i + 1].get("source_scene_code") or "")
        )
        if mode == "off":
            chosen = "none"
        elif mode == "cuts_only" or same_source_split:
            # Whisper-created child shots belong to one conceptual Outer-AI scene;
            # never duplicate a decorative transition inside that conceptual scene.
            chosen = "hard_cut"
        elif mode == "manual":
            if str(scene.get("transition_intent") or "").strip():
                chosen = _transition_from_intent(scene.get("transition_intent"))
            else:
                chosen = canonical_transition(scene.get("transition_out") or scene.get("transition") or "cross_dissolve")
        else:
            start = float(scene.get("start", 0) or 0)
            weights = _adjust_weights(PRESETS[preset], boundary_duration, int(intensity), start < max(0.0, float(hook_seconds or 0)))
            cue = _editorial_cue(scene, out[i + 1])
            if cue == "chapter":
                weights = {"dip_black": 62.0, "film_fade": 24.0, "cross_dissolve": 14.0}
            elif cue == "soft":
                weights["cross_dissolve"] = weights.get("cross_dissolve", 0.0) + 26.0
                weights["film_fade"] = weights.get("film_fade", 0.0) + 10.0
            elif cue == "emphasis":
                weights["hard_cut"] = weights.get("hard_cut", 0.0) + 34.0
                if boundary_duration >= 2.5:
                    weights["push"] = weights.get("push", 0.0) + 5.0
            excluded: set[str] = set()
            if no_repeat and previous:
                excluded.add(previous)
            # Avoid clusters of special transitions: at most one inside a 4-boundary window.
            if special_history and i - special_history[-1] <= 3:
                excluded.update(k for k, spec in TRANSITION_CATALOG.items() if spec.get("category") == "special")
            token = _stable_int(project_seed, scene.get("scene_code") or scene.get("scene_id") or i, i, preset)
            chosen = _weighted_pick(weights, token, excluded)
            if no_repeat and chosen == previous:
                chosen = _weighted_pick(weights, token + 7919, {previous})

        scene["transition_out"] = chosen
        scene["transition_auto"] = mode == "auto"
        if mode == "manual" and chosen not in {"none", "hard_cut"}:
            try:
                requested_ms = int(float(scene.get("transition_duration_ms", 350) or 350))
            except Exception:
                requested_ms = 350
            scene["transition_duration_ms"] = max(100, min(800, requested_ms))
        else:
            scene["transition_duration_ms"] = int(round(recommended_duration(chosen, boundary_duration, base_seconds, int(intensity)) * 1000))
        if transition_uses_xfade(chosen):
            variant = resolve_xfade_transition(chosen, i, str(scene.get("transition_variant") or ""))
            scene["transition_xfade"] = variant
            scene["transition_variant"] = variant
            scene["transition_engine"] = "xfade"
        else:
            scene["transition_duration_ms"] = 0
            scene["transition_xfade"] = None
            scene["transition_variant"] = None
            scene["transition_engine"] = "cut"
        scene["transition_intensity"] = max(0.0, min(1.0, float(intensity) / 100.0))
        scene["transition_editorial_cue"] = (
            _editorial_cue(scene, out[i + 1]) if i < len(out) - 1 else "ending"
        )
        if mode in {"auto", "cuts_only", "off"}:
            # Auto owns both the visual transition and its sound pairing.  Clear
            # stale scene-plan SFX so the next mapping pass cannot play a whoosh
            # over a clean cut or a mismatched effect.
            scene["transition_sfx"] = "none"
            if mode in {"cuts_only", "off"}:
                scene["sfx_volume"] = 0.0
        if TRANSITION_CATALOG[chosen].get("category") == "special":
            special_history.append(i)
        previous = chosen

    counts = Counter(canonical_transition(scene.get("transition_out")) for scene in out[:-1])
    total = max(1, sum(counts.values()))
    summary = {
        "mode": mode,
        "preset": preset,
        "total": sum(counts.values()),
        "counts": dict(counts),
        "percentages": {key: round(value * 100.0 / total, 1) for key, value in counts.items()},
        "no_consecutive_repeat": all(
            canonical_transition(out[i].get("transition_out")) != canonical_transition(out[i - 1].get("transition_out"))
            for i in range(1, max(1, len(out) - 1))
        ) if len(out) > 2 else True,
        "black_dot_auto_used": any(canonical_transition(s.get("transition_out")) in {"pixelize", "circle_open"} for s in out[:-1]),
    }
    return out, summary


def transition_distribution(scenes: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(canonical_transition(scene.get("transition_out") or scene.get("transition")) for scene in scenes[:-1])
    total = max(1, sum(counts.values()))
    return {
        "total": sum(counts.values()),
        "counts": dict(counts),
        "percentages": {key: round(value * 100.0 / total, 1) for key, value in counts.items()},
        "labels": {key: transition_label(key) for key in counts},
    }

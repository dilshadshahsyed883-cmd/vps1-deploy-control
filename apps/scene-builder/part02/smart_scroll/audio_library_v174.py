"""V17.4 permanent Music/SFX library resolution.

The web owns library CRUD and writes /data/permanent/library.json.  The worker is
read-only: it resolves stable asset IDs into deterministic per-job plans.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import requests
from pathlib import Path
from typing import Any

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".aiff", ".aif"}
ALIASES: dict[str, tuple[str, ...]] = {
    "beach": ("ocean", "waves", "coast", "sea", "shore"),
    "sea": ("ocean", "waves", "beach", "coast"),
    "ocean": ("waves", "beach", "sea", "coast"),
    "bazaar": ("market", "crowd", "street"),
    "market": ("bazaar", "crowd", "street"),
    "jungle": ("forest", "birds", "nature", "wind"),
    "forest": ("jungle", "birds", "nature"),
    "airplane": ("aircraft", "jet", "airport"),
    "aircraft": ("airplane", "jet", "airport"),
    "traffic": ("cars", "road", "city", "vehicle"),
    "train": ("rail", "railway", "station"),
    "rain": ("storm", "weather", "water"),
    "waterfall": ("water", "river", "nature"),
    "city": ("street", "traffic", "urban"),
}


def _tokens(value: str) -> set[str]:
    base = set(re.findall(r"[a-z0-9]+", str(value or "").lower()))
    expanded = set(base)
    for token in list(base):
        expanded.update(ALIASES.get(token, ()))
    return expanded


def _manifest_path(root: Path) -> Path:
    return root / "library.json"


def load_library(root: str | Path) -> dict[str, Any]:
    base = Path(root)
    try:
        data = json.loads(_manifest_path(base).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("manifest is not an object")
    except Exception:
        data = {"schema": 1, "music": [], "sfx": []}
    data.setdefault("music", [])
    data.setdefault("sfx", [])
    return data


def _valid_assets(root: Path, kind: str) -> list[dict[str, Any]]:
    rows = []
    for raw in load_library(root).get(kind, []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        stored = str(item.get("stored_filename") or "")
        path = root / kind / stored
        if not stored or path.suffix.lower() not in AUDIO_EXTS or not path.is_file() or path.stat().st_size < 1024:
            continue
        item["path"] = str(path)
        item["asset_id"] = str(item.get("asset_id") or "")
        rows.append(item)
    return rows


def _score_asset(item: dict[str, Any], wanted: set[str]) -> int:
    hay = " ".join([
        str(item.get("display_name") or ""), str(item.get("category") or ""),
        " ".join(str(x) for x in item.get("tags", []) if x),
        str(item.get("filename") or ""),
    ])
    toks = _tokens(hay)
    return len(wanted & toks) * 12 + (5 if str(item.get("category") or "").lower() in wanted else 0)




def _smart_rank_music(
    pool: list[dict[str, Any]],
    *,
    project_name: str,
    script_text: str,
    intro_queries: list[str],
) -> list[dict[str, Any]]:
    """Dependency-light deterministic fallback used when AI is disabled/unavailable."""
    context = " ".join([project_name, script_text[:12000], *intro_queries[:20]])
    wanted = _tokens(context)
    digest_seed = hashlib.sha256((project_name + "|" + " ".join(sorted(wanted))).encode("utf-8", errors="replace")).hexdigest()
    return sorted(
        pool,
        key=lambda item: (-_score_asset(item, wanted), hashlib.sha256((digest_seed + str(item.get("asset_id"))).encode()).hexdigest()),
    )


def _ai_rank_music(
    pool: list[dict[str, Any]],
    *,
    project_name: str,
    script_text: str,
    intro_queries: list[str],
) -> tuple[list[dict[str, Any]] | None, str]:
    """Use an optional OpenAI-compatible chat endpoint for genuine AI selection.

    The feature is deliberately opt-in.  No endpoint/model is contacted when the
    preset checkbox is off.  If the administrator has not configured an endpoint
    or the request fails, callers fall back to deterministic smart ranking and
    record the reason in the resolved playlist metadata.
    """
    endpoint = str(os.getenv("MUSIC_AI_ENDPOINT", "") or "").strip()
    model = str(os.getenv("MUSIC_AI_MODEL", "") or "").strip()
    api_key = str(os.getenv("MUSIC_AI_API_KEY", "") or "").strip()
    timeout = max(5.0, min(120.0, float(os.getenv("MUSIC_AI_TIMEOUT_SECONDS", "45") or 45)))
    if not endpoint or not model:
        return None, "AI endpoint/model is not configured; deterministic Smart Music Selection was used."

    candidates = []
    for item in pool[:80]:
        candidates.append({
            "asset_id": str(item.get("asset_id") or ""),
            "name": str(item.get("display_name") or item.get("filename") or ""),
            "category": str(item.get("category") or ""),
            "tags": [str(x) for x in item.get("tags", []) if x][:20],
            "duration": float(item.get("duration") or 0.0),
        })
    prompt = {
        "task": "Select and order background music for a documentary from only the supplied reusable library tracks.",
        "rules": [
            "Return JSON only with key track_ids containing 1 to 6 asset_id values.",
            "Never invent an asset_id.",
            "Prefer a strong intro-compatible first track when an intro is present, followed by documentary-safe main tracks.",
            "Avoid obviously mismatched mood/category choices.",
        ],
        "project_name": project_name,
        "script_excerpt": script_text[:9000],
        "intro_queries": intro_queries[:20],
        "tracks": candidates,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are a documentary music supervisor. Choose only from the supplied track IDs and return strict JSON."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    }
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.S).strip()
        selected = json.loads(content)
        ids = [str(x) for x in (selected.get("track_ids") or []) if str(x).strip()][:6]
        by_id = {str(item.get("asset_id") or ""): item for item in pool}
        ranked = [dict(by_id[asset_id]) for asset_id in ids if asset_id in by_id]
        if ranked:
            return ranked, ""
        return None, "AI returned no valid library track IDs; deterministic Smart Music Selection was used."
    except Exception as exc:
        return None, f"AI music selection failed ({type(exc).__name__}); deterministic Smart Music Selection was used."


def resolve_music_playlist(
    root: str | Path,
    playlist: list[dict[str, Any]],
    *,
    ai_select: bool,
    allowed_categories: list[str],
    project_name: str,
    script_text: str,
    intro_queries: list[str],
    intro_enabled: bool,
) -> list[dict[str, Any]]:
    base = Path(root)
    assets = _valid_assets(base, "music")
    by_id = {str(x.get("asset_id")): x for x in assets if x.get("asset_id")}
    resolved: list[dict[str, Any]] = []

    if not ai_select:
        for index, row in enumerate(sorted((dict(x) for x in playlist if isinstance(x, dict)), key=lambda x: int(x.get("order", 9999)))):
            asset = by_id.get(str(row.get("asset_id") or ""))
            if not asset:
                continue
            item = dict(asset)
            item["order"] = index + 1
            item["role"] = str(row.get("role") or "general").lower()
            resolved.append(item)
        return resolved

    allowed = {str(x).strip().lower() for x in allowed_categories if str(x).strip()}
    pool = [x for x in assets if not allowed or str(x.get("category") or "").lower() in allowed]
    if not pool:
        return []

    ranked, warning = _ai_rank_music(
        pool,
        project_name=project_name,
        script_text=script_text,
        intro_queries=intro_queries,
    )
    selection_method = "ai_model"
    if not ranked:
        ranked = _smart_rank_music(
            pool, project_name=project_name, script_text=script_text, intro_queries=intro_queries
        )
        selection_method = "smart_fallback"

    # Keep the resolver conservative: enough variety for long documentaries without
    # turning music selection into a per-scene decision system.
    for index, asset in enumerate(ranked[: min(6, len(ranked))]):
        item = dict(asset)
        item["order"] = index + 1
        item["role"] = "intro" if intro_enabled and index == 0 else "main"
        item["selection_method"] = selection_method
        if warning and index == 0:
            item["selection_warning"] = warning
        resolved.append(item)
    return resolved


def _best_sfx(assets: list[dict[str, Any]], intent: str) -> dict[str, Any] | None:
    wanted = _tokens(intent)
    if not wanted:
        return None
    ranked = sorted((( _score_asset(item, wanted), item) for item in assets), key=lambda pair: (-pair[0], str(pair[1].get("asset_id") or "")))
    return dict(ranked[0][1]) if ranked and ranked[0][0] > 0 else None


def _scene_times(scene: dict[str, Any]) -> tuple[float, float]:
    start = float(scene.get("start", scene.get("start_seconds", 0.0)) or 0.0)
    end = float(scene.get("end", scene.get("end_seconds", 0.0)) or 0.0)
    if end <= start:
        end = start + max(0.05, float(scene.get("duration", scene.get("duration_seconds", 0.0)) or 0.0))
    return max(0.0, start), max(start, end)


def resolve_sfx_plan(root: str | Path, scenes: list[dict[str, Any]], settings: Any) -> dict[str, Any]:
    if not bool(getattr(settings, "sfx_enabled", False)):
        return {"environment": [], "transition": [], "warnings": []}
    assets = _valid_assets(Path(root), "sfx")
    warnings: list[str] = []
    env_events: list[dict[str, Any]] = []
    transition_events: list[dict[str, Any]] = []
    amount = str(getattr(settings, "sfx_amount", "medium") or "medium").lower()
    density = {"low": 0.10, "medium": 0.20, "high": 0.35}.get(amount, 0.20)
    max_env = max(1, int(round(len(scenes) * density))) if scenes else 0
    selected_env = 0

    if bool(getattr(settings, "environment_sfx_enabled", True)):
        for index, scene in enumerate(scenes):
            intent = str(scene.get("environment_sfx") or scene.get("ambient_sfx") or "").strip()
            if not intent or intent.lower() in {"none", "off", "no"}:
                continue
            asset = _best_sfx(assets, intent)
            if not asset:
                warnings.append(f"No local SFX match for {scene.get('scene_code','scene')}: {intent}")
                continue
            start, end = _scene_times(scene)
            event = {
                "asset_id": asset["asset_id"], "path": asset["path"], "intent": intent,
                "start": start, "end": end,
                "volume": max(0.0, min(0.60, float(getattr(settings, "environment_sfx_volume_percent", 18) or 18) / 100.0)),
                "scene_codes": [str(scene.get("scene_code") or f"SCENE_{index+1:03d}")],
                "kind": "environment",
            }
            # Continuity is evaluated before the density cap: extending an already
            # selected ambience bed across an adjacent matching scene does not consume
            # another SFX slot and must not create audible 4-second restarts.
            if env_events and env_events[-1]["asset_id"] == event["asset_id"] and event["start"] <= env_events[-1]["end"] + 0.20:
                env_events[-1]["end"] = max(env_events[-1]["end"], event["end"])
                env_events[-1]["scene_codes"].extend(event["scene_codes"])
                continue
            # Deterministic density cap applies only when starting a new ambience bed.
            if selected_env >= max_env:
                continue
            env_events.append(event)
            selected_env += 1

    if bool(getattr(settings, "transition_sfx_library_enabled", True)):
        for index, scene in enumerate(scenes[:-1]):
            intent = str(scene.get("transition_sfx") or "").strip()
            if not intent or intent.lower() in {"none", "off", "no", "cut", "hard_cut"}:
                continue
            asset = _best_sfx(assets, intent)
            if not asset:
                # Built-in transition SFX may still be handled by the renderer; local
                # library misses are therefore warnings, never fatal.
                warnings.append(f"No local transition SFX match for {scene.get('scene_code','scene')}: {intent}")
                continue
            _, end = _scene_times(scene)
            transition_events.append({
                "asset_id": asset["asset_id"], "path": asset["path"], "intent": intent,
                "start": max(0.0, end - max(0.0, float(scene.get("transition_duration_ms", 0) or 0) / 1000.0)),
                "end": end + 1.5,
                "volume": max(0.0, min(0.60, float(getattr(settings, "transition_sfx_volume_percent", 24) or 24) / 100.0)),
                "scene_codes": [str(scene.get("scene_code") or f"SCENE_{index+1:03d}")],
                "kind": "transition",
            })
    return {"environment": env_events, "transition": transition_events, "warnings": warnings}

"""Shared scene-sync planning and scene/media tagging helpers."""
from __future__ import annotations

import math
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from .local_ai_tools import extract_scene_plan

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at", "for", "from", "with", "by", "into", "onto",
    "he", "she", "they", "it", "his", "her", "their", "was", "were", "is", "are", "be", "been", "being", "that", "this",
    "then", "than", "just", "very", "there", "here", "when", "while", "after", "before", "who", "whom", "which", "what",
    "had", "has", "have", "not", "out", "up", "down", "over", "under", "as", "if", "so", "because", "about", "through",
}

_ABSTRACT_HINTS = {
    "understood", "realized", "remembered", "wondered", "thought", "felt", "decided", "knew", "believed", "hoped",
    "dreamed", "suspected", "regretted", "imagined", "noticed", "feared", "loved", "missed",
}


def _slugify(value: str, fallback: str = "scene") -> str:
    value = re.sub(r"[^0-9A-Za-z_-]+", "_", str(value or fallback)).strip("_").lower()
    return value or fallback


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z0-9']+", str(text or "").lower()) if w and w not in _STOPWORDS]


def _coverage_score(target_words: list[str], candidate_words: list[str]) -> float:
    if not target_words or not candidate_words:
        return 0.0
    target_set = set(target_words)
    candidate_set = set(candidate_words)
    overlap = len(target_set & candidate_set)
    if overlap <= 0:
        return 0.0
    coverage = overlap / max(1, len(target_set))
    density = overlap / max(1, len(candidate_set))
    ordered = 0
    pos = -1
    for token in target_words:
        try:
            idx = candidate_words.index(token, pos + 1)
            ordered += 1
            pos = idx
        except ValueError:
            continue
    order_score = ordered / max(1, min(len(target_words), len(candidate_words)))
    return (coverage * 0.62) + (density * 0.18) + (order_score * 0.20)


def _segment_dicts(raw_segments: Iterable[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in raw_segments or []:
        if isinstance(raw, dict):
            text = str(raw.get("text", "") or "").strip()
            start = float(raw.get("start", 0.0) or 0.0)
            end = max(start, float(raw.get("end", start) or start))
        else:
            text = str(getattr(raw, "text", "") or "").strip()
            start = float(getattr(raw, "start", 0.0) or 0.0)
            end = max(start, float(getattr(raw, "end", start) or start))
        if not text and end <= start:
            continue
        items.append({"text": text, "start": start, "end": end, "_words": _words(text)})
    return items


def _classify_scene(text: str, query: str) -> str:
    low = f"{text} {query}".lower()
    if any(token in low for token in _ABSTRACT_HINTS):
        return "reaction"
    if re.search(r"\b(door|box|photo|photograph|letter|boots|ring|book|window|chair|house|road|car|train|forest|room)\b", low):
        return "object_or_location"
    if re.search(r"\b(looked|walked|ran|sat|stood|opened|closed|entered|left|arrived|held|watched|turned|smiled|cried)\b", low):
        return "visual_scene"
    return "abstract" if len(_words(text)) < 5 else "visual_scene"


def _auto_scene_limit(total_duration: float) -> int:
    # AI-image workflow target: a fresh visual roughly every 4.5 seconds.
    # This keeps three-minute stories near 40 scenes instead of 19-22, while
    # avoiding hyperactive cuts on short narration.
    duration = max(1.0, float(total_duration or 1.0))
    target = int(round(duration / 4.5))
    return max(16, min(60, target))


def resolve_scene_limit(total_duration: float, requested_limit: int | None = None) -> int:
    requested = int(requested_limit or 0)
    if requested <= 0:
        return _auto_scene_limit(total_duration)
    return max(1, min(60, requested))


def _build_equal_timing(plan: list[dict[str, Any]], total_duration: float, *, reason: str) -> list[dict[str, Any]]:
    count = max(1, len(plan))
    total = max(1.0, float(total_duration or count))
    step = total / count
    current = 0.0
    for item in plan:
        start = current
        end = total if item is plan[-1] else min(total, current + step)
        item["start"] = round(start, 3)
        item["end"] = round(max(start + 0.01, end), 3)
        item["duration"] = round(max(0.01, item["end"] - item["start"]), 3)
        item["timing_source"] = reason
        item["match_confidence"] = 0.0
        item["match_method"] = "equal_scene_count"
        current = end
    return plan


def build_scene_sync_plan(
    story_text: str,
    *,
    caption_segments: Iterable[Any] | None = None,
    total_duration: float = 0.0,
    requested_limit: int | None = None,
) -> list[dict[str, Any]]:
    total = max(1.0, float(total_duration or 1.0))
    limit = resolve_scene_limit(total, requested_limit)
    raw_plan = extract_scene_plan(story_text or "", limit=limit)
    plan: list[dict[str, Any]] = []
    for index, item in enumerate(raw_plan, start=1):
        text = str(item.get("text", "") or "").strip()
        query = str(item.get("query", "") or "").strip()
        tag = f"scene_{index:02d}_{_slugify(query or text, fallback='scene')[:48]}"
        plan.append({
            "scene_id": index,
            "tag": tag,
            "query_tag": _slugify(query or text, fallback='scene'),
            "query": query,
            "text": text,
            "word_count": int(item.get("word_count", 0) or max(1, len(_words(text)))),
            "visual_type": _classify_scene(text, query),
        })
    if not plan:
        return []

    segments = _segment_dicts(caption_segments or [])
    if not segments:
        return _build_equal_timing(plan, total, reason="equal_scene_count")

    cursor = 0
    scene_count = len(plan)
    segment_count = len(segments)
    for idx, item in enumerate(plan):
        target_words = _words(item.get("text", "")) or _words(item.get("query", ""))
        remaining_scenes = max(1, scene_count - idx)
        remaining_segments = max(1, segment_count - cursor)
        ideal_take = max(1, int(round(remaining_segments / remaining_scenes)))
        max_window = max(1, min(4, ideal_take + 1))

        best: tuple[float, int, int] | None = None
        for start_idx in range(cursor, min(segment_count, cursor + 4)):
            combined_words: list[str] = []
            for window in range(1, max_window + 1):
                end_idx = start_idx + window - 1
                if end_idx >= segment_count:
                    break
                combined_words.extend(segments[end_idx]["_words"])
                score = _coverage_score(target_words, combined_words)
                # prefer shorter windows when the score is otherwise close
                score -= max(0, window - 1) * 0.02
                if best is None or score > best[0]:
                    best = (score, start_idx, end_idx)

        if best is None:
            start_idx = min(cursor, segment_count - 1)
            end_idx = start_idx
            confidence = 0.0
        else:
            confidence, start_idx, end_idx = best
            if confidence < 0.18:
                start_idx = min(cursor, segment_count - 1)
                end_idx = min(segment_count - 1, start_idx + ideal_take - 1)
                confidence = 0.0

        start_time = float(segments[start_idx]["start"])
        matched_end = float(segments[end_idx]["end"])
        item["matched_segment_start"] = start_idx
        item["matched_segment_end"] = end_idx
        item["start"] = round(start_time, 3)
        item["matched_end"] = round(matched_end, 3)
        item["match_confidence"] = round(float(confidence), 3)
        if confidence >= 0.62:
            item["match_method"] = "exact_segment_match"
            item["timing_source"] = "whisper_segment_match"
        elif confidence > 0.0:
            item["match_method"] = "approx_segment_match"
            item["timing_source"] = "whisper_segment_approx"
        else:
            item["match_method"] = "ordered_segment_fallback"
            item["timing_source"] = "ordered_segment_fallback"
        cursor = min(segment_count, end_idx + 1)

    for idx, item in enumerate(plan):
        start = float(item.get("start", 0.0) or 0.0)
        if idx + 1 < len(plan):
            next_start = float(plan[idx + 1].get("start", start) or start)
            if next_start > start:
                end = next_start
            else:
                end = max(start, float(item.get("matched_end", start) or start))
        else:
            end = total
        item["end"] = round(max(start + 0.01, min(total, end)), 3)
        item["duration"] = round(max(0.01, item["end"] - start), 3)

    # Guard against duplicate/flat starts that could create zero duration.
    for idx in range(len(plan) - 2, -1, -1):
        cur = plan[idx]
        nxt = plan[idx + 1]
        if float(cur.get("end", 0.0) or 0.0) > float(nxt.get("start", 0.0) or 0.0):
            cur["end"] = round(max(float(cur.get("start", 0.0) or 0.0) + 0.01, float(nxt.get("start", 0.0) or 0.0)), 3)
            cur["duration"] = round(max(0.01, float(cur["end"]) - float(cur.get("start", 0.0) or 0.0)), 3)

    return plan


def summarize_scene_sync(plan: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items = list(plan or [])
    if not items:
        return {"status": "none", "label": "No scene sync plan", "exact": 0, "approx": 0, "fallback": 0, "count": 0}
    exact = sum(1 for item in items if str(item.get("match_method", "")) == "exact_segment_match")
    approx = sum(1 for item in items if str(item.get("match_method", "")) in {"approx_segment_match", "ordered_segment_fallback"})
    fallback = sum(1 for item in items if str(item.get("timing_source", "")) == "equal_scene_count")
    if fallback == len(items):
        status = "fallback"
        label = f"Equal scene fallback ({len(items)} scenes)"
    elif exact == len(items):
        status = "exact"
        label = f"Exact scene sync ({len(items)} scenes)"
    elif exact > 0 or approx > 0:
        status = "approx"
        label = f"Whisper scene sync ({exact} exact, {approx} approx)"
    else:
        status = "unknown"
        label = f"Scene sync ready ({len(items)} scenes)"
    return {"status": status, "label": label, "exact": exact, "approx": approx, "fallback": fallback, "count": len(items)}


def apply_scene_media_tags(
    scene_plan: Iterable[dict[str, Any]],
    media_paths: Iterable[str | Path],
    output_dir: str | Path | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    plan = [dict(item) for item in (scene_plan or [])]
    ordered_paths = [str(Path(str(p))) for p in (media_paths or []) if Path(str(p)).exists()]
    if not plan or not ordered_paths:
        return ordered_paths, plan

    output_root = Path(output_dir) if output_dir else None
    if output_root is not None:
        output_root.mkdir(parents=True, exist_ok=True)

    by_folder: dict[str, list[str]] = {}
    for raw in ordered_paths:
        path = Path(raw)
        folder_key = _slugify(path.parent.name or path.stem, fallback="scene")
        by_folder.setdefault(folder_key, []).append(raw)

    used: set[str] = set()
    cursor = 0
    tagged_paths: list[str] = []
    for item in plan:
        desired = _slugify(str(item.get("query", "") or item.get("query_tag", "") or item.get("tag", "scene")), fallback="scene")
        candidates = by_folder.get(desired, [])
        selected: str | None = None
        match_type = "tag_match"
        for candidate in candidates:
            if candidate not in used:
                selected = candidate
                break
        if selected is None:
            match_type = "sequential_fallback"
            while cursor < len(ordered_paths) and ordered_paths[cursor] in used:
                cursor += 1
            if cursor < len(ordered_paths):
                selected = ordered_paths[cursor]
                cursor += 1
            else:
                # Cycle only if every path has already been used.
                selected = ordered_paths[len(tagged_paths) % len(ordered_paths)]
        used.add(selected)
        source_path = Path(selected)
        tagged_path = source_path
        if output_root is not None:
            tagged_name = f"{item.get('tag', 'scene')}{source_path.suffix.lower() or '.jpg'}"
            candidate = output_root / tagged_name
            if not candidate.exists():
                try:
                    shutil.copy2(source_path, candidate)
                    tagged_path = candidate
                except Exception:
                    tagged_path = source_path
            else:
                tagged_path = candidate
        item["media_source_path"] = str(source_path)
        item["media_path"] = str(tagged_path)
        item["media_match"] = match_type
        item["media_tag"] = str(item.get("tag", "") or "")
        tagged_paths.append(str(tagged_path))
    return tagged_paths, plan

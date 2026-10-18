from __future__ import annotations

import math
import re
from bisect import bisect_right
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

_LINE_RE = re.compile(r"^\s*(SCENE_\d{3,})\|(.*)$", re.IGNORECASE)
_TIMED_LINE_RE = re.compile(
    r"^\s*(SCENE_\d{3,})\|([0-9]+(?:\.[0-9]+)?)\|([0-9]+(?:\.[0-9]+)?)\|(.*)$",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[\w’'-]+", re.UNICODE)


def parse_scene_txt(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8-sig")
    scenes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        timed_match = _TIMED_LINE_RE.match(raw)
        if timed_match:
            code = timed_match.group(1).upper()
            start = float(timed_match.group(2))
            end = float(timed_match.group(3))
            narration = timed_match.group(4).strip()
            if end <= start:
                raise ValueError(f"Invalid timing on line {line_no}: end must be greater than start for {code}")
            payload: dict[str, Any] = {
                "scene_code": code,
                "text": narration,
                "visual_prompt": narration,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "timing_source": "whisper_timed_txt",
            }
        else:
            match = _LINE_RE.match(raw)
            if not match:
                raise ValueError(
                    f"Invalid scene line {line_no}. Expected SCENE_001|Scene text "
                    "or SCENE_001|0.000|4.250|Scene text"
                )
            code = match.group(1).upper()
            narration = match.group(2).strip()
            payload = {"scene_code": code, "text": narration, "visual_prompt": narration}

        if not narration:
            raise ValueError(f"Scene text is empty on line {line_no}: {code}")
        if code in seen:
            raise ValueError(f"Duplicate scene ID on line {line_no}: {code}")
        seen.add(code)
        scenes.append(payload)
    if not scenes:
        raise ValueError("The selected TXT contains no valid scene lines.")
    return scenes


def combined_script(scenes: Iterable[dict[str, Any]]) -> str:
    return " ".join(str(item.get("text", "") or "").strip() for item in scenes if str(item.get("text", "") or "").strip())


def serialize_whisper_scene_txt(
    scenes: Iterable[dict[str, Any]],
    *,
    duration: float = 0.0,
    detected_language: str = "auto",
    engine: str = "",
    audio_path: str = "",
) -> str:
    """Return a re-importable timed scene TXT produced after Whisper alignment.

    Data rows use: SCENE_ID|START_SECONDS|END_SECONDS|EXACT_SCENE_TEXT
    Metadata is written as comments so the same file remains readable by
    :func:`parse_scene_txt`.
    """
    plan = [dict(item) for item in scenes]
    if not plan:
        raise ValueError("There are no Whisper-aligned scenes to export.")

    lines = [
        "# Scene Builder Whisper Timed TXT v1",
        "# format=SCENE_ID|START_SECONDS|END_SECONDS|TEXT",
        f"# duration_seconds={max(0.0, float(duration or 0.0)):.3f}",
        f"# detected_language={str(detected_language or 'auto').strip() or 'auto'}",
        f"# sync_engine={str(engine or '').strip() or 'unknown'}",
        f"# audio_path={str(audio_path or '').strip()}",
        f"# scene_count={len(plan)}",
    ]
    for index, item in enumerate(plan, start=1):
        code = str(item.get("scene_code") or f"SCENE_{index:03d}").strip().upper()
        start = max(0.0, float(item.get("start", 0.0) or 0.0))
        end = max(start, float(item.get("end", start) or start))
        if end <= start:
            raise ValueError(f"Scene {code} does not have valid Whisper timing.")
        narration = " ".join(str(item.get("text", "") or "").replace("|", " ").splitlines()).strip()
        if not narration:
            raise ValueError(f"Scene {code} has empty text.")
        lines.append(f"{code}|{start:.3f}|{end:.3f}|{narration}")
    return "\n".join(lines) + "\n"


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _caption_word_anchors(
    caption_segments: Iterable[dict[str, Any]],
    total_duration: float,
) -> list[dict[str, Any]]:
    """Expand caption chunks into ordered approximate word-level anchors.

    faster-whisper provides word timing before the caption builder groups words
    into display-sized chunks. The public caption cache only contains each
    chunk's start/end time, so words are distributed evenly inside that chunk.
    This is still substantially safer than assigning an entire chunk to one
    scene and prevents the old end-of-captions cursor collapse.
    """
    total = max(0.1, float(total_duration or 0.1))
    anchors: list[dict[str, Any]] = []
    previous_end = 0.0
    for item in caption_segments:
        words = _words(str((item or {}).get("text", "") or ""))
        if not words:
            continue
        start = max(0.0, min(total, float((item or {}).get("start", 0.0) or 0.0)))
        end = max(start, min(total, float((item or {}).get("end", start) or start)))
        if end <= start:
            end = min(total, start + 0.05)
        # Keep malformed/overlapping source chunks ordered without inventing a
        # second copy of the final timestamp.
        start = max(previous_end, start)
        if start >= total:
            break
        end = min(total, max(start + 0.001, end))
        if end <= start:
            continue
        span = end - start
        count = len(words)
        for index, word in enumerate(words):
            word_start = start + span * index / count
            word_end = start + span * (index + 1) / count
            anchors.append({"word": word, "start": word_start, "end": word_end})
        previous_end = end
    return anchors


def _script_to_transcript_boundary_positions(
    script_words: list[str],
    transcript_words: list[str],
    script_boundaries: list[int],
) -> tuple[list[float], float]:
    """Map scene boundaries from script-word space into transcript-word space.

    Exact ordered matching blocks guide the mapping. Unmatched regions are
    interpolated between the nearest valid blocks, so missing or imperfect ASR
    text cannot exhaust a cursor and collapse all remaining scenes onto the
    final Whisper timestamp.
    """
    script_count = len(script_words)
    transcript_count = len(transcript_words)
    if script_count <= 0 or transcript_count <= 0:
        return [0.0 for _ in script_boundaries], 0.0

    matcher = SequenceMatcher(None, script_words, transcript_words, autojunk=False)
    points: list[tuple[int, float]] = [(0, 0.0), (script_count, float(transcript_count))]
    for block in matcher.get_matching_blocks():
        if block.size <= 0:
            continue
        points.append((block.a, float(block.b)))
        points.append((block.a + block.size, float(block.b + block.size)))

    # Merge duplicate script positions while preserving an ordered transcript
    # position. This protects against repeated phrases in narration.
    merged: list[tuple[int, float]] = []
    for script_pos, transcript_pos in sorted(points, key=lambda pair: (pair[0], pair[1])):
        if merged and script_pos == merged[-1][0]:
            merged[-1] = (script_pos, max(merged[-1][1], transcript_pos))
        else:
            merged.append((script_pos, transcript_pos))

    script_positions = [pair[0] for pair in merged]
    mapped: list[float] = []
    last_position = 0.0
    for boundary in script_boundaries:
        boundary = max(0, min(script_count, int(boundary)))
        right = bisect_right(script_positions, boundary)
        if right <= 0:
            position = 0.0
        elif right >= len(merged):
            position = float(transcript_count)
        else:
            left_script, left_transcript = merged[right - 1]
            right_script, right_transcript = merged[right]
            if right_script <= left_script:
                position = left_transcript
            else:
                fraction = (boundary - left_script) / (right_script - left_script)
                position = left_transcript + fraction * (right_transcript - left_transcript)
        position = max(last_position, min(float(transcript_count), position))
        mapped.append(position)
        last_position = position
    return mapped, float(matcher.ratio())


def _transcript_position_to_time(
    position: float,
    anchors: list[dict[str, Any]],
    total_duration: float,
) -> float:
    total = max(0.1, float(total_duration or 0.1))
    count = len(anchors)
    if count <= 0 or position <= 0:
        return 0.0
    if position >= count:
        return total
    index = int(position)
    fraction = position - index
    current_start = float(anchors[index].get("start", 0.0) or 0.0)
    if fraction <= 0.0:
        return max(0.0, min(total, current_start))
    current_end = float(anchors[index].get("end", current_start) or current_start)
    return max(0.0, min(total, current_start + (current_end - current_start) * fraction))


def _strict_scene_boundaries(
    desired: list[float],
    total_duration: float,
    scene_count: int,
    *,
    timeline_start: float = 0.0,
    timeline_end: float | None = None,
) -> list[float]:
    """Clamp boundaries into a continuous, strictly increasing speech timeline.

    R6 allows the first narration scene to begin after an embedded music-only
    lead-in.  The boundary math therefore operates inside ``timeline_start`` →
    ``timeline_end`` instead of forcing the first scene to absolute 0.
    """
    total = max(0.1, float(total_duration or 0.1))
    start = max(0.0, min(total - 0.001, float(timeline_start or 0.0)))
    end = total if timeline_end is None else max(start + 0.001, min(total, float(timeline_end or total)))
    if scene_count <= 0:
        return [start, end]
    span = max(0.001, end - start)
    # Fifty milliseconds is preferred, but very short speech windows must still
    # remain mathematically valid for every scene.
    minimum_gap = min(0.05, span / max(1, scene_count * 2))
    output = [start] * (scene_count + 1)
    output[0] = start
    output[-1] = end
    for index in range(1, scene_count):
        requested = float(desired[index] if index < len(desired) else start)
        lower = output[index - 1] + minimum_gap
        upper = end - (scene_count - index) * minimum_gap
        output[index] = min(max(requested, lower), upper)
    return output


def align_scenes_to_captions(
    scenes: Iterable[dict[str, Any]],
    caption_segments: Iterable[dict[str, Any]],
    total_duration: float,
    *,
    timeline_start: float | None = None,
) -> list[dict[str, Any]]:
    plan = [dict(item) for item in scenes]
    caps = [dict(item) for item in caption_segments if str((item or {}).get("text", "") or "").strip()]
    total = max(0.1, float(total_duration or 0.1))
    requested_start = max(0.0, min(total - 0.001, float(timeline_start or 0.0))) if timeline_start is not None else 0.0
    if not plan:
        return []
    if not caps:
        speech_span = max(0.001, total - requested_start)
        step = speech_span / len(plan)
        for index, item in enumerate(plan):
            start = requested_start + index * step
            end = total if index == len(plan) - 1 else requested_start + (index + 1) * step
            item.update(
                start=round(start, 3),
                end=round(end, 3),
                duration=round(end - start, 3),
                timing_source="equal_scene_fallback",
                match_confidence=0.0,
            )
            item.setdefault("status", "missing_image")
            item.setdefault("approval_status", "approved")
            item.setdefault("scene_id", index + 1)
            item.setdefault("tag", str(item.get("scene_code", f"SCENE_{index + 1:03d}")).lower())
        return plan

    anchors = _caption_word_anchors(caps, total)
    if not anchors:
        return align_scenes_to_captions(plan, [], total, timeline_start=requested_start)

    scene_word_lists = [_words(str(item.get("text", "") or "")) for item in plan]
    scene_weights = [max(1, len(words)) for words in scene_word_lists]
    script_words = [word for words in scene_word_lists for word in words]
    # Empty scene text is not expected, but assigning one placeholder per scene
    # keeps the timeline valid instead of collapsing it.
    if not script_words:
        script_words = [f"scene_{index}" for index in range(len(plan))]

    script_boundaries = [0]
    used = 0
    for weight in scene_weights:
        used += weight
        script_boundaries.append(used)

    transcript_words = [str(anchor.get("word", "") or "") for anchor in anchors]
    mapped_positions, confidence = _script_to_transcript_boundary_positions(
        script_words,
        transcript_words,
        script_boundaries,
    )
    desired = [_transcript_position_to_time(position, anchors, total) for position in mapped_positions]
    # Never re-zero real Whisper timestamps.  Auto/Manual Narration Start owns
    # the first documentary boundary; the final scene still reaches full audio.
    desired[0] = requested_start
    desired[-1] = total
    boundaries = _strict_scene_boundaries(
        desired,
        total,
        len(plan),
        timeline_start=requested_start,
        timeline_end=total,
    )

    timing_source = "whisper_word_alignment" if confidence >= 0.35 else "ordered_whisper_word_fallback"
    for index, item in enumerate(plan):
        start = boundaries[index]
        end = boundaries[index + 1]
        item["start"] = round(start, 3)
        item["end"] = round(end, 3)
        item["duration"] = round(end - start, 3)
        item["matched_end"] = round(end, 3)
        item["match_confidence"] = round(confidence, 3)
        item["timing_source"] = timing_source
        item.setdefault("status", "missing_image")
        item.setdefault("approval_status", "approved")
        item.setdefault("scene_id", index + 1)
        item.setdefault("tag", str(item.get("scene_code", f"SCENE_{index + 1:03d}")).lower())
    return plan


def _hard_min_rebalance_shots(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforce configured visual minimums without crossing pacing stages.

    The normalizer already guarantees each visual is no longer than its stage
    maximum.  Authored/Whisper boundaries can still leave a short visual beat,
    though.  Rebalance only the local cluster around those short beats so the
    *actual visual holds* stay inside Min/Max whenever the stage has enough
    duration to make that mathematically possible.  Hook/general boundaries are
    never crossed.
    """
    if not shots:
        return []

    epsilon = 0.002

    def duration(row: dict[str, Any]) -> float:
        return max(0.0, float(row.get("end", 0.0) or 0.0) - float(row.get("start", 0.0) or 0.0))

    def feasible(span: float, minimum: float, maximum: float) -> tuple[bool, int, int]:
        lower = max(1, int(math.ceil((span - epsilon) / max(0.001, maximum))))
        upper = max(0, int(math.floor((span + epsilon) / max(0.001, minimum))))
        return lower <= upper, lower, upper

    def text_slice(row: dict[str, Any], start: float, end: float) -> str:
        words = str(row.get("text", "") or "").split()
        if not words:
            return ""
        row_start = float(row.get("start", 0.0) or 0.0)
        row_end = float(row.get("end", row_start) or row_start)
        span = max(0.001, row_end - row_start)
        rel_start = max(0.0, min(1.0, (start - row_start) / span))
        rel_end = max(rel_start, min(1.0, (end - row_start) / span))
        word_start = max(0, min(len(words), round(len(words) * rel_start)))
        word_end = max(word_start, min(len(words), round(len(words) * rel_end)))
        return " ".join(words[word_start:word_end])

    def build_row(source_rows: list[dict[str, Any]], start: float, end: float, stage: str, minimum: float, maximum: float) -> dict[str, Any]:
        overlaps: list[tuple[float, dict[str, Any], float, float]] = []
        for row in source_rows:
            row_start = float(row.get("start", 0.0) or 0.0)
            row_end = float(row.get("end", row_start) or row_start)
            overlap_start = max(start, row_start)
            overlap_end = min(end, row_end)
            overlap = max(0.0, overlap_end - overlap_start)
            if overlap > 0.0005:
                overlaps.append((overlap, row, overlap_start, overlap_end))
        if not overlaps:
            base = dict(source_rows[0])
            overlaps = [(end - start, source_rows[0], start, end)]
        else:
            base = dict(max(overlaps, key=lambda item: item[0])[1])

        chunks: list[str] = []
        source_codes: list[str] = []
        for _, row, overlap_start, overlap_end in overlaps:
            chunk = text_slice(row, overlap_start, overlap_end)
            if chunk:
                chunks.append(chunk)
            code = str(row.get("source_scene_code") or row.get("scene_code") or "")
            if code and code not in source_codes:
                source_codes.append(code)
        if chunks:
            base["text"] = " ".join(chunks)
        base.update(
            start=round(start, 3),
            end=round(end, 3),
            duration=round(end - start, 3),
            matched_end=round(end, 3),
            timing_validated=True,
            media_duration_stage=stage,
            media_duration_min_seconds=round(minimum, 3),
            media_duration_max_seconds=round(maximum, 3),
            timing_source=f"{base.get('timing_source') or 'authored'}_minimum_rebalanced",
            generated_child_shot=True,
            source_scene_codes=source_codes,
            source_scene_code=str(base.get("source_scene_code") or base.get("scene_code") or (source_codes[0] if source_codes else "")),
        )
        return base

    result: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(shots):
        stage = str(shots[cursor].get("media_duration_stage") or "general")
        block_end = cursor + 1
        while block_end < len(shots) and str(shots[block_end].get("media_duration_stage") or "general") == stage:
            block_end += 1
        block = [dict(row) for row in shots[cursor:block_end]]

        index = 0
        safety = 0
        while index < len(block) and safety < max(20, len(block) * 8):
            safety += 1
            row = block[index]
            minimum = max(0.05, float(row.get("media_duration_min_seconds", 0.05) or 0.05))
            maximum = max(minimum, float(row.get("media_duration_max_seconds", minimum) or minimum))
            if duration(row) + epsilon >= minimum:
                index += 1
                continue

            left = right = index
            chosen: tuple[int, int, float, float, int, int] | None = None
            while True:
                cluster_start = float(block[left].get("start", 0.0) or 0.0)
                cluster_end = float(block[right].get("end", cluster_start) or cluster_start)
                span = max(0.0, cluster_end - cluster_start)
                ok, lower, upper = feasible(span, minimum, maximum)
                if ok:
                    chosen = (left, right, cluster_start, cluster_end, lower, upper)
                    break

                candidates: list[tuple[float, str]] = []
                if right + 1 < len(block):
                    candidates.append((duration(block[right + 1]), "right"))
                if left > 0:
                    candidates.append((duration(block[left - 1]), "left"))
                if not candidates:
                    break
                # Expand by the smaller neighbour first to keep re-timing local.
                _, side = min(candidates, key=lambda item: item[0])
                if side == "right":
                    right += 1
                else:
                    left -= 1

            if chosen is None:
                # A stage shorter than its configured minimum cannot satisfy the
                # range. Keep it render-safe and make the exceptional condition
                # explicit in audit metadata instead of silently pretending it met Min.
                row["media_duration_min_unavoidable"] = True
                block[index] = row
                index += 1
                continue

            left, right, cluster_start, cluster_end, lower, upper = chosen
            original_count = right - left + 1
            part_count = max(lower, min(original_count, upper))
            cluster_rows = block[left:right + 1]
            span = cluster_end - cluster_start
            replacement: list[dict[str, Any]] = []
            for part in range(part_count):
                shot_start = cluster_start + span * part / part_count
                shot_end = cluster_end if part == part_count - 1 else cluster_start + span * (part + 1) / part_count
                replacement.append(build_row(cluster_rows, shot_start, shot_end, stage, minimum, maximum))
            block[left:right + 1] = replacement
            index = max(0, left - 1)

        result.extend(block)
        cursor = block_end

    return result


def normalize_scene_timing(
    scenes: Iterable[dict[str, Any]],
    total_duration: float,
    *,
    max_scene_seconds: float = 4.0,
    min_scene_seconds: float = 0.50,
    hook_seconds: float = 0.0,
    hook_min_scene_seconds: float | None = None,
    hook_max_scene_seconds: float | None = None,
    timeline_start: float = 0.0,
) -> list[dict[str, Any]]:
    """Return a complete render-safe visual timeline.

    Scene TXT plans normally contain creative direction but no timestamps.  The
    old worker silently interpreted every missing span as 50 ms, causing every
    scene after the first to reuse the opening visual.  This function makes
    timing explicit before media acquisition:

    * valid authored/Whisper timing is retained;
    * missing or malformed timing is distributed by narration word weight;
    * spans longer than the visual cap are divided into consecutive shots;
    * every emitted shot records its source scene and timing provenance.
    """
    plan = [dict(item) for item in scenes]
    total = max(0.1, float(total_duration or 0.1))
    timeline_start = max(0.0, min(total - 0.001, float(timeline_start or 0.0)))
    speech_span = max(0.001, total - timeline_start)
    maximum = max(0.25, min(30.0, float(max_scene_seconds or 4.0)))
    minimum = max(0.05, min(maximum, float(min_scene_seconds or 0.50)))
    hook_window = max(0.0, float(hook_seconds or 0.0))
    hook_maximum = max(0.25, min(30.0, float(hook_max_scene_seconds if hook_max_scene_seconds is not None else maximum)))
    hook_minimum = max(0.05, min(hook_maximum, float(hook_min_scene_seconds if hook_min_scene_seconds is not None else minimum)))
    validation_minimum = min(minimum, hook_minimum)
    if not plan:
        return []

    previous_end = 0.0
    timing_valid = True
    for index, item in enumerate(plan):
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            timing_valid = False
            break
        if (
            start < -0.001
            or end <= start
            or (index and start < previous_end - 0.05)
            or (speech_span >= len(plan) * validation_minimum and end - start < validation_minimum * 0.80)
        ):
            timing_valid = False
            break
        previous_end = end
    if timing_valid and abs(previous_end - total) > max(0.75, total * 0.02):
        timing_valid = False

    if not timing_valid:
        weights = [max(1, len(_words(str(item.get("text", "") or "")))) for item in plan]
        weight_total = max(1, sum(weights))
        cursor = timeline_start
        for index, (item, weight) in enumerate(zip(plan, weights)):
            end = total if index == len(plan) - 1 else cursor + speech_span * weight / weight_total
            item.update(
                start=round(cursor, 3),
                end=round(end, 3),
                duration=round(end - cursor, 3),
                matched_end=round(end, 3),
                match_confidence=0.0,
                timing_source="narration_weighted_fallback",
            )
            cursor = end

    shots: list[dict[str, Any]] = []
    for source_index, item in enumerate(plan, 1):
        start = max(0.0, float(item.get("start", 0.0) or 0.0))
        end = min(total, max(start + 0.001, float(item.get("end", start) or start)))
        span = max(0.001, end - start)
        words = str(item.get("text", "") or "").split()
        keyword_options = [
            str(item.get("primary_keyword") or item.get("keyword") or "").strip(),
            *[
                str(value or "").strip()
                for value in (item.get("backup_keywords") or item.get("fallback_keywords") or [])
            ],
        ]
        keyword_options = [value for value in keyword_options if value]
        original_code = str(item.get("scene_code") or f"SCENE_{source_index:03d}")

        # Split an authored/Whisper scene exactly at the hook boundary before
        # applying duration limits. This guarantees that the First 2 Minutes
        # and General ranges never bleed into each other.
        segments: list[tuple[float, float, str, float, float]] = []
        if hook_window > 0.0 and start < hook_window < end:
            segments.append((start, hook_window, "hook", hook_minimum, hook_maximum))
            segments.append((hook_window, end, "general", minimum, maximum))
        elif hook_window > 0.0 and start < hook_window:
            segments.append((start, end, "hook", hook_minimum, hook_maximum))
        else:
            segments.append((start, end, "general", minimum, maximum))

        part_number = 0
        segment_parts: list[tuple[float, float, str, float, float]] = []
        for segment_start, segment_end, stage, active_minimum, active_maximum in segments:
            segment_span = max(0.001, segment_end - segment_start)
            part_count = max(1, int(math.ceil(segment_span / active_maximum)))
            while part_count > 1 and segment_span / part_count < active_minimum:
                part_count -= 1
            for part in range(part_count):
                shot_start = segment_start + segment_span * part / part_count
                shot_end = segment_end if part == part_count - 1 else segment_start + segment_span * (part + 1) / part_count
                segment_parts.append((shot_start, shot_end, stage, active_minimum, active_maximum))

        total_parts = len(segment_parts)
        for shot_start, shot_end, stage, active_minimum, active_maximum in segment_parts:
            shot = dict(item)
            # Slice narration words by the shot's proportional position in the
            # original source scene so a boundary split does not repeat text.
            relative_start = max(0.0, min(1.0, (shot_start - start) / span))
            relative_end = max(relative_start, min(1.0, (shot_end - start) / span))
            word_start = round(len(words) * relative_start)
            word_end = round(len(words) * relative_end)
            if words:
                shot["text"] = " ".join(words[word_start:word_end]) or " ".join(words)
            if keyword_options:
                selected = keyword_options[part_number % len(keyword_options)]
                shot["primary_keyword"] = selected
                shot["keyword"] = selected
            shot.update(
                source_scene_code=original_code,
                source_scene_part=part_number + 1,
                source_scene_parts=total_parts,
                start=round(shot_start, 3),
                end=round(shot_end, 3),
                duration=round(shot_end - shot_start, 3),
                matched_end=round(shot_end, 3),
                timing_validated=True,
                media_duration_stage=stage,
                media_duration_min_seconds=round(active_minimum, 3),
                media_duration_max_seconds=round(active_maximum, 3),
            )
            if total_parts > 1:
                shot["timing_source"] = f"{item.get('timing_source') or 'authored'}_visual_split"
                shot["generated_child_shot"] = True
                if part_number > 0:
                    shot["effect"] = "none"
                    shot["effect_intensity"] = 0.0
                    shot["overlay"] = "none"
                    shot["overlay_intensity"] = 0.0
                    shot["transition_sfx"] = "none"
                    shot["sfx_volume"] = 0.0
            shots.append(shot)
            part_number += 1

    shots = _hard_min_rebalance_shots(shots)

    for index, shot in enumerate(shots, 1):
        shot["scene_code"] = f"SCENE_{index:03d}"
        shot["scene_id"] = index
        shot["tag"] = f"scene_{index:03d}"
    return shots

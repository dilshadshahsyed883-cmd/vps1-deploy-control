"""Caption timing, language detection, and SRT helpers for Auto Caption Video Studio.

Important design rule:
Better Sync may use Whisper/faster-whisper for timing, but the visible caption
text should normally come from the user's original script. This prevents Urdu,
Arabic, Hindi, Gujarati, and other languages from being changed by ASR mistakes.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math
import re
import hashlib
import json
from pathlib import Path
from typing import Callable, Iterable

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]


@dataclass
class CaptionSegment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


# Famous/common languages supported by faster-whisper/Whisper where possible.
# Display name, ISO-ish whisper language code, text direction.
LANGUAGE_CHOICES: list[tuple[str, str, str]] = [
    ("Auto Detect", "auto", "auto"),
    ("English", "en", "ltr"), ("Hindi", "hi", "ltr"), ("Urdu", "ur", "rtl"),
    ("Arabic", "ar", "rtl"), ("Gujarati", "gu", "ltr"), ("Punjabi", "pa", "ltr"),
    ("Bengali", "bn", "ltr"), ("Marathi", "mr", "ltr"), ("Tamil", "ta", "ltr"),
    ("Telugu", "te", "ltr"), ("Kannada", "kn", "ltr"), ("Malayalam", "ml", "ltr"),
    ("Nepali", "ne", "ltr"), ("Sindhi", "sd", "rtl"), ("Pashto", "ps", "rtl"),
    ("Persian / Farsi", "fa", "rtl"), ("Sanskrit", "sa", "ltr"),
    ("Spanish", "es", "ltr"), ("French", "fr", "ltr"), ("German", "de", "ltr"),
    ("Portuguese", "pt", "ltr"), ("Italian", "it", "ltr"), ("Russian", "ru", "ltr"),
    ("Ukrainian", "uk", "ltr"), ("Polish", "pl", "ltr"), ("Dutch", "nl", "ltr"),
    ("Turkish", "tr", "ltr"), ("Indonesian", "id", "ltr"), ("Malay", "ms", "ltr"),
    ("Chinese", "zh", "ltr"), ("Cantonese", "zh", "ltr"), ("Japanese", "ja", "ltr"),
    ("Korean", "ko", "ltr"), ("Thai", "th", "ltr"), ("Vietnamese", "vi", "ltr"),
    ("Filipino / Tagalog", "tl", "ltr"), ("Swahili", "sw", "ltr"), ("Hebrew", "he", "rtl"),
    ("Greek", "el", "ltr"), ("Romanian", "ro", "ltr"), ("Czech", "cs", "ltr"),
    ("Hungarian", "hu", "ltr"), ("Swedish", "sv", "ltr"), ("Norwegian", "no", "ltr"),
    ("Danish", "da", "ltr"), ("Finnish", "fi", "ltr"), ("Bulgarian", "bg", "ltr"),
    ("Serbian", "sr", "ltr"), ("Croatian", "hr", "ltr"), ("Slovak", "sk", "ltr"),
    ("Slovenian", "sl", "ltr"), ("Lithuanian", "lt", "ltr"), ("Latvian", "lv", "ltr"),
    ("Estonian", "et", "ltr"), ("Albanian", "sq", "ltr"), ("Armenian", "hy", "ltr"),
    ("Azerbaijani", "az", "ltr"), ("Kazakh", "kk", "ltr"), ("Uzbek", "uz", "ltr"),
    ("Kyrgyz", "ky", "ltr"), ("Mongolian", "mn", "ltr"), ("Georgian", "ka", "ltr"),
    ("Afrikaans", "af", "ltr"), ("Amharic", "am", "ltr"), ("Somali", "so", "ltr"),
    ("Hausa", "ha", "ltr"), ("Yoruba", "yo", "ltr"), ("Igbo", "ig", "ltr"),
    ("Zulu", "zu", "ltr"), ("Xhosa", "xh", "ltr"), ("Latin", "la", "ltr"),
    ("Esperanto", "eo", "ltr"), ("Welsh", "cy", "ltr"), ("Irish", "ga", "ltr"),
    ("Icelandic", "is", "ltr"), ("Basque", "eu", "ltr"), ("Catalan", "ca", "ltr"),
    ("Galician", "gl", "ltr"), ("Belarusian", "be", "ltr"), ("Macedonian", "mk", "ltr"),
    ("Bosnian", "bs", "ltr"), ("Sinhala", "si", "ltr"), ("Burmese", "my", "ltr"),
    ("Khmer", "km", "ltr"), ("Lao", "lo", "ltr"), ("Javanese", "jw", "ltr"),
    ("Sundanese", "su", "ltr"), ("Maori", "mi", "ltr"), ("Haitian Creole", "ht", "ltr"),
]

LANGUAGE_MAP = {name: (code, direction) for name, code, direction in LANGUAGE_CHOICES}
CODE_DIRECTION = {code: direction for _, code, direction in LANGUAGE_CHOICES if code != "auto"}
RTL_CODES = {code for _, code, direction in LANGUAGE_CHOICES if direction == "rtl"}

# Extra Arabic-script hints for separating Urdu/Persian/Pashto/Sindhi from Arabic.
URDU_HINT_CHARS = set("ٹڈڑںےھہچگپژ")
PERSIAN_HINT_CHARS = set("گچپژک")
PASHTO_HINT_CHARS = set("ټډړږښځڅڼېۍ")
SINDHI_HINT_CHARS = set("ٻڀٿٺٽڙڏڍڻڃڄڇ")
ARABIC_HINT_CHARS = set("ثحخضظطغق")


def language_items() -> list[tuple[str, str]]:
    return [(name, code) for name, code, _direction in LANGUAGE_CHOICES]


def language_code(value: str | None) -> str:
    value = (value or "auto").strip()
    if not value:
        return "auto"
    if value in LANGUAGE_MAP:
        return LANGUAGE_MAP[value][0]
    lowered = value.lower()
    for _name, code, _direction in LANGUAGE_CHOICES:
        if lowered == code.lower():
            return code
    return "auto"


def language_name_from_code(code: str | None) -> str:
    code = (code or "auto").strip().lower()
    for name, lang_code, _direction in LANGUAGE_CHOICES:
        if lang_code.lower() == code:
            return name
    return "Auto Detect"


def _count_pattern(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text or ""))




def caption_generation_signature(
    *,
    text: str,
    audio_path: str | Path | None,
    language: str = "auto",
    model_size: str = "base",
    max_words: int = 8,
    offset: float = 0.0,
    min_duration: float = 0.0,
    tighten_gaps: bool = False,
    voice_speed_percent: int = 100,
    text_source: str = "whisper",
) -> str:
    """Stable cache key for caption text/timing inputs."""
    source = Path(str(audio_path or ""))
    audio_signature = str(source)
    if source.exists():
        try:
            stat = source.stat()
            audio_signature = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        except Exception:
            audio_signature = str(source)
    payload = {
        "text_sha256": hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest(),
        "audio": audio_signature,
        "language": language_code(language),
        "model": str(model_size or "base"),
        "max_words": max(2, int(max_words or 8)),
        "offset": round(float(offset or 0.0), 4),
        "min_duration": round(float(min_duration or 0.0), 4),
        "tighten_gaps": bool(tighten_gaps),
        "voice_speed_percent": int(voice_speed_percent or 100),
        "text_source": str(text_source or "whisper").strip().lower(),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _detect_arabic_script_language(sample: str) -> str:
    """Heuristic for common Arabic-script languages.

    This is intentionally script-first because caption text must follow the
    user's script, not Whisper's sometimes noisy ASR language guess.
    """
    chars = [ch for ch in sample if "\u0600" <= ch <= "\u06ff" or "\u0750" <= ch <= "\u077f" or "\u08a0" <= ch <= "\u08ff"]
    if not chars:
        return "ar"
    counts = {
        "ur": sum(ch in URDU_HINT_CHARS for ch in chars),
        "fa": sum(ch in PERSIAN_HINT_CHARS for ch in chars),
        "ps": sum(ch in PASHTO_HINT_CHARS for ch in chars),
        "sd": sum(ch in SINDHI_HINT_CHARS for ch in chars),
        "ar": sum(ch in ARABIC_HINT_CHARS for ch in chars),
    }
    # Urdu normally contains frequent "ہے/کی/میں/نہیں" style chars; prefer Urdu
    # when Urdu-only letters appear, because Whisper often reports it as Arabic.
    for code in ("ps", "sd", "ur", "fa"):
        if counts[code] > 0:
            return code
    return "ar"


def detect_script_language(text: str) -> str:
    """Detect language primarily from the script text.

    Used for caption language/direction and to hint Whisper. This keeps Urdu,
    Arabic, Hindi, Gujarati, etc. captions in the same language/script that the
    user typed.
    """
    sample = text or ""
    counts = {
        "arabic_script": _count_pattern(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", sample),
        "hi": _count_pattern(r"[\u0900-\u097F]", sample),
        "bn": _count_pattern(r"[\u0980-\u09FF]", sample),
        "pa": _count_pattern(r"[\u0A00-\u0A7F]", sample),
        "gu": _count_pattern(r"[\u0A80-\u0AFF]", sample),
        "ta": _count_pattern(r"[\u0B80-\u0BFF]", sample),
        "te": _count_pattern(r"[\u0C00-\u0C7F]", sample),
        "kn": _count_pattern(r"[\u0C80-\u0CFF]", sample),
        "ml": _count_pattern(r"[\u0D00-\u0D7F]", sample),
        "si": _count_pattern(r"[\u0D80-\u0DFF]", sample),
        "th": _count_pattern(r"[\u0E00-\u0E7F]", sample),
        "lo": _count_pattern(r"[\u0E80-\u0EFF]", sample),
        "my": _count_pattern(r"[\u1000-\u109F]", sample),
        "km": _count_pattern(r"[\u1780-\u17FF]", sample),
        "zh": _count_pattern(r"[\u4E00-\u9FFF]", sample),
        "ja": _count_pattern(r"[\u3040-\u30FF]", sample),
        "ko": _count_pattern(r"[\uAC00-\uD7AF]", sample),
        "he": _count_pattern(r"[\u0590-\u05FF]", sample),
        "ru": _count_pattern(r"[\u0400-\u04FF]", sample),
    }
    if counts["arabic_script"] > 0:
        return _detect_arabic_script_language(sample)
    best_code, best_count = max(((k, v) for k, v in counts.items() if k != "arabic_script"), key=lambda kv: kv[1])
    if best_count > 0:
        return best_code
    return "en"


def resolve_caption_language(text: str, requested_language: str | None, *, prefer_script: bool = True) -> str:
    requested = language_code(requested_language)
    if requested != "auto":
        return requested
    return detect_script_language(text) if prefer_script else "auto"


def is_rtl_text(text: str, language: str | None = None) -> bool:
    code = language_code(language)
    if code == "auto":
        code = detect_script_language(text)
    return code in RTL_CODES or bool(re.search(r"[\u0590-\u08FF]", text or ""))


def _sentences(text: str) -> list[str]:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    # Keep user's sentence/order; normalize spaces only inside each piece.
    parts = re.split(r"(?<=[.!?؟۔।;؛])\s+|\n+", raw)
    return [re.sub(r"\s+", " ", p).strip() for p in parts if re.sub(r"\s+", " ", p).strip()]


def _has_word_spaces(text: str) -> bool:
    return bool(re.search(r"\S+\s+\S+", text or ""))


def _script_units(text: str) -> int:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return 0
    if _has_word_spaces(text):
        return max(1, len(text.split()))
    # CJK/Thai/etc. have no spaces; approximate units by visible chars.
    return max(1, math.ceil(len(re.sub(r"\s+", "", text)) / 3))


def split_text_to_caption_chunks(text: str, max_words: int = 8) -> list[str]:
    """Split the original script into readable caption chunks."""
    max_words = max(2, min(24, int(max_words or 8)))
    chunks: list[str] = []
    for sentence in _sentences(text):
        if not _has_word_spaces(sentence) and len(sentence) > max(24, max_words * 3):
            step = max(8, max_words * 3)
            chunks.extend(sentence[i:i + step].strip() for i in range(0, len(sentence), step) if sentence[i:i + step].strip())
            continue
        words = sentence.split()
        # Do not create ugly one-word leftovers. A caption with one or two extra
        # words is usually better than splitting punctuation into its own card.
        if len(words) <= max_words + 2:
            chunks.append(sentence)
            continue
        i = 0
        while i < len(words):
            remaining = len(words) - i
            if remaining <= max_words + 2 and chunks:
                chunk = " ".join(words[i:]).strip()
                if chunk:
                    prev_words = chunks[-1].split()
                    if len(prev_words) <= 2 or remaining <= 2:
                        chunks[-1] = (chunks[-1] + " " + chunk).strip()
                    else:
                        chunks.append(chunk)
                break
            chunk = " ".join(words[i:i + max_words]).strip()
            if chunk:
                chunks.append(chunk)
            i += max_words
    return chunks or ([text.strip()] if text.strip() else [])


def _caption_chunks_with_units(text: str, max_words: int) -> list[tuple[str, int]]:
    return [(chunk, _script_units(chunk)) for chunk in split_text_to_caption_chunks(text, max_words=max_words) if chunk.strip()]


def _apply_offset(start: float, end: float, offset: float, duration: float) -> CaptionSegment:
    start = max(0.0, min(float(duration), start + offset))
    end = max(start + 0.15, min(float(duration), end + offset))
    return CaptionSegment(start=start, end=end, text="")


def _smooth_segments(segments: list[CaptionSegment], duration: float) -> list[CaptionSegment]:
    if not segments:
        return []
    cleaned: list[CaptionSegment] = []
    prev_end = 0.0
    for idx, seg in enumerate(segments):
        start = max(0.0, min(duration, float(seg.start)))
        end = max(start + 0.25, min(duration, float(seg.end)))
        # Avoid heavy overlap, but allow tiny overlap tolerance for natural reading.
        if cleaned and start < prev_end - 0.03:
            start = prev_end
            end = max(start + 0.25, end)
        if idx < len(segments) - 1:
            end = min(duration, end)
        text = re.sub(r"\s+", " ", seg.text or "").strip()
        if text and start < duration:
            cleaned.append(CaptionSegment(start=start, end=min(duration, end), text=text))
            prev_end = cleaned[-1].end
    return cleaned


def polish_caption_segments(segments: list[CaptionSegment], duration: float, *, min_duration: float = 0.0, tighten_gaps: bool = False) -> list[CaptionSegment]:
    """Post-process captions for more professional on-screen timing.

    Heavy Caption Lab uses this to avoid blink-fast captions and to close tiny
    silent gaps that make Shorts/Reels captions feel jumpy. Text is never
    rewritten here; only segment timing is refined.
    """
    if not segments:
        return []
    duration = max(0.5, float(duration or 0.5))
    min_duration = max(0.0, min(2.5, float(min_duration or 0.0)))
    out: list[CaptionSegment] = []
    for index, seg in enumerate(_smooth_segments(segments, duration)):
        start = max(0.0, min(duration, float(seg.start)))
        end = max(start + 0.15, min(duration, float(seg.end)))
        if min_duration > 0.0 and (end - start) < min_duration:
            extra = min_duration - (end - start)
            start = max(0.0, start - extra * 0.35)
            end = min(duration, end + extra * 0.65)
        if out and start < out[-1].end:
            start = out[-1].end
            end = max(start + 0.15, end)
        if tighten_gaps and out:
            gap = start - out[-1].end
            if 0.03 < gap < 0.28:
                midpoint = out[-1].end + gap * 0.55
                out[-1].end = min(midpoint, duration)
                start = max(out[-1].end, midpoint)
        if index < len(segments) - 1:
            end = min(end, duration)
        if (seg.text or "").strip() and start < duration:
            out.append(CaptionSegment(start=start, end=min(duration, max(start + 0.15, end)), text=seg.text))
    return _smooth_segments(out, duration)


def fast_sync_captions(text: str, duration: float, max_words: int = 8, offset: float = 0.0) -> list[CaptionSegment]:
    duration = max(0.5, float(duration or 0.5))
    chunks = _caption_chunks_with_units(text, max_words=max_words)
    if not chunks:
        return []
    weights = [max(1, units) for _chunk, units in chunks]
    total_weight = max(1, sum(weights))
    out: list[CaptionSegment] = []
    cursor = 0.0
    for index, ((chunk, _units), weight) in enumerate(zip(chunks, weights)):
        if index == len(chunks) - 1:
            end = duration
        else:
            end = cursor + (duration * weight / total_weight)
            end = max(cursor + 0.35, end)
            remaining = len(chunks) - index - 1
            end = min(end, duration - remaining * 0.25)
        seg = _apply_offset(cursor, end, offset, duration)
        seg.text = chunk
        out.append(seg)
        cursor = max(end, cursor + 0.25)
        if cursor >= duration:
            break
    return _smooth_segments(out, duration)


def _timing_anchors_from_whisper_segments(segments: Iterable[object], duration: float, cancel_callback: CancelCallback | None = None, progress_callback: ProgressCallback | None = None) -> list[tuple[float, float]]:
    anchors: list[tuple[float, float]] = []
    for seg_index, seg in enumerate(segments):
        if cancel_callback and cancel_callback():
            return []
        seg_start = max(0.0, float(getattr(seg, "start", 0.0) or 0.0))
        seg_end = min(duration, max(seg_start + 0.05, float(getattr(seg, "end", duration) or duration)))
        words = getattr(seg, "words", None) or []
        used_words = 0
        for w in words:
            token = str(getattr(w, "word", "") or "").strip()
            if not token:
                continue
            start = max(0.0, float(getattr(w, "start", seg_start) or seg_start))
            end = min(duration, max(start + 0.03, float(getattr(w, "end", seg_end) or seg_end)))
            anchors.append((start, end))
            used_words += 1
        if used_words == 0:
            # Word timestamps can be missing for some models/languages. Build
            # rough anchors from segment text so original script can still align.
            seg_text = str(getattr(seg, "text", "") or "").strip()
            approx_count = max(1, len(seg_text.split()) if seg_text else max(1, round((seg_end - seg_start) / 0.45)))
            step = max(0.03, (seg_end - seg_start) / approx_count)
            for i in range(approx_count):
                anchors.append((seg_start + i * step, min(seg_end, seg_start + (i + 1) * step)))
        if progress_callback and seg_index % 5 == 0:
            progress_callback(min(95, 15 + seg_index), "Better Sync: reading voice timing anchors...")
    return [(max(0.0, s), min(duration, e)) for s, e in anchors if e > s]


def _script_guided_segments_from_anchors(*, script_text: str, anchors: list[tuple[float, float]], duration: float, max_words: int, offset: float) -> list[CaptionSegment]:
    chunks = _caption_chunks_with_units(script_text, max_words=max_words)
    if not chunks:
        return []
    if not anchors:
        return fast_sync_captions(script_text, duration, max_words=max_words, offset=offset)

    total_units = max(1, sum(max(1, units) for _chunk, units in chunks))
    total_anchors = max(1, len(anchors))
    out: list[CaptionSegment] = []
    used_units = 0
    for chunk, units in chunks:
        units = max(1, units)
        start_units = used_units
        end_units = min(total_units, used_units + units)
        start_idx = min(total_anchors - 1, max(0, int(math.floor(start_units / total_units * total_anchors))))
        end_idx = min(total_anchors - 1, max(start_idx, int(math.ceil(end_units / total_units * total_anchors)) - 1))
        start = anchors[start_idx][0]
        end = anchors[end_idx][1]
        seg = _apply_offset(start, end, offset, duration)
        seg.text = chunk
        out.append(seg)
        used_units = end_units
    return _smooth_segments(out, duration)


def _whisper_transcript_segments_from_anchors_text(*, segments: Iterable[object], duration: float, max_words: int, offset: float, cancel_callback: CancelCallback | None = None, progress_callback: ProgressCallback | None = None) -> list[CaptionSegment]:
    """Legacy option: use Whisper's transcription words as caption text."""
    out: list[CaptionSegment] = []
    for seg_index, seg in enumerate(segments):
        if cancel_callback and cancel_callback():
            return []
        words = getattr(seg, "words", None) or []
        if words:
            bucket: list[str] = []
            bucket_start: float | None = None
            bucket_end: float | None = None
            for w in words:
                token = str(getattr(w, "word", "") or "").strip()
                if not token:
                    continue
                if bucket_start is None:
                    bucket_start = float(getattr(w, "start", getattr(seg, "start", 0.0)) or 0.0)
                bucket_end = float(getattr(w, "end", getattr(seg, "end", duration)) or duration)
                bucket.append(token)
                if len(bucket) >= max_words:
                    s = _apply_offset(bucket_start, bucket_end or bucket_start + 1.0, offset, duration)
                    s.text = " ".join(bucket).strip()
                    out.append(s)
                    bucket, bucket_start, bucket_end = [], None, None
            if bucket:
                s = _apply_offset(bucket_start or float(getattr(seg, "start", 0.0) or 0.0), bucket_end or float(getattr(seg, "end", duration) or duration), offset, duration)
                s.text = " ".join(bucket).strip()
                out.append(s)
        else:
            seg_text = str(getattr(seg, "text", "") or "").strip()
            seg_start = float(getattr(seg, "start", 0.0) or 0.0)
            seg_end = min(duration, float(getattr(seg, "end", duration) or duration))
            local = fast_sync_captions(seg_text, max(0.2, seg_end - seg_start), max_words=max_words, offset=0.0)
            for item in local:
                s = _apply_offset(seg_start + item.start, seg_start + item.end, offset, duration)
                s.text = item.text
                out.append(s)
        if progress_callback and seg_index % 5 == 0:
            progress_callback(min(95, 15 + seg_index), "Better Sync: building Whisper transcript captions...")
    return _smooth_segments([s for s in out if s.text.strip()], duration)


def better_sync_captions(
    *,
    audio_path: str | Path,
    fallback_text: str,
    duration: float,
    language: str = "auto",
    max_words: int = 8,
    offset: float = 0.0,
    model_size: str = "base",
    text_source: str = "whisper",
    prefer_script_language: bool = True,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> tuple[list[CaptionSegment], str, str]:
    """Use faster-whisper for caption timing/text.

    text_source="whisper" uses the voice-to-text transcript as the visible
    caption text. If Whisper is unavailable or fails in this mode, captions are
    skipped instead of falling back to script-duration captions.
    """
    duration = max(0.5, float(duration or 0.5))
    if progress_callback:
        progress_callback(0, "Better Sync: checking faster-whisper...")
    text_source_mode = (text_source or "whisper").lower().strip()
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as exc:
        if text_source_mode == "whisper":
            if progress_callback:
                progress_callback(0, "Whisper unavailable; captions skipped instead of using fallback text")
            detected = language_code(language) if language_code(language) != "auto" else "auto"
            return [], detected, f"whisper-unavailable: {exc}"
        if progress_callback:
            progress_callback(0, "Better Sync unavailable; using script Fast Sync fallback")
        detected = resolve_caption_language(fallback_text, language, prefer_script=True)
        return fast_sync_captions(fallback_text, duration, max_words=max_words, offset=offset), detected, f"script-fast-fallback: {exc}"

    if cancel_callback and cancel_callback():
        return [], "cancelled", "Cancelled"

    requested_code = language_code(language)
    script_code = resolve_caption_language(fallback_text, language, prefer_script=prefer_script_language)
    if text_source_mode == "whisper":
        # Voice-to-text default: let Whisper auto-detect unless the user picked a language.
        whisper_lang = None if requested_code == "auto" else requested_code
    else:
        # Script-guided mode: when Auto Detect is selected, pass the script
        # language as a hint so Urdu does not get treated as Arabic.
        whisper_lang = None if (requested_code == "auto" and not prefer_script_language) else script_code
    if whisper_lang == "auto":
        whisper_lang = None

    if progress_callback:
        source_note = "Whisper voice-to-text transcript" if text_source_mode == "whisper" else "script words"
        progress_callback(5, f"Better Sync: loading Whisper model ({model_size}) for {source_note}...")
    try:
        model = _persistent_whisper_model(model_size, device="cpu", compute_type="int8")
        if progress_callback:
            progress_callback(15, "Better Sync: analyzing voice timings...")
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=whisper_lang,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        detected_audio = getattr(info, "language", None) or "auto"
        detected = script_code if script_code != "auto" else detected_audio

        # faster-whisper returns a generator; materialize once so we can build
        # anchors or legacy transcript captions reliably.
        whisper_segments = list(segments_iter)
        if text_source_mode == "whisper":
            out = _whisper_transcript_segments_from_anchors_text(
                segments=whisper_segments,
                duration=duration,
                max_words=max_words,
                offset=offset,
                cancel_callback=cancel_callback,
                progress_callback=progress_callback,
            )
            engine = "whisper-transcript"
        else:
            anchors = _timing_anchors_from_whisper_segments(
                whisper_segments,
                duration,
                cancel_callback=cancel_callback,
                progress_callback=progress_callback,
            )
            out = _script_guided_segments_from_anchors(
                script_text=fallback_text,
                anchors=anchors,
                duration=duration,
                max_words=max_words,
                offset=offset,
            )
            engine = "script-guided-whisper"

        out = [s for s in out if s.text.strip()]
        if not out:
            raise RuntimeError("Whisper returned no usable caption text/timing.")
        return out, detected, engine
    except Exception as exc:
        if text_source_mode == "whisper":
            if progress_callback:
                progress_callback(0, f"Whisper captions failed; captions skipped: {exc}")
            detected = language_code(language) if language_code(language) != "auto" else "auto"
            return [], detected, f"whisper-failed: {exc}"
        if progress_callback:
            progress_callback(0, f"Better Sync failed; using script Fast Sync fallback: {exc}")
        detected = resolve_caption_language(fallback_text, language, prefer_script=True)
        return fast_sync_captions(fallback_text, duration, max_words=max_words, offset=offset), detected, f"script-fast-fallback: {exc}"


def build_caption_segments(
    *,
    text: str,
    audio_path: str | Path | None,
    duration: float,
    sync_mode: str = "better",
    language: str = "auto",
    max_words: int = 8,
    offset: float = 0.0,
    text_source: str = "script",
    prefer_script_language: bool = True,
    model_size: str = "base",
    min_duration: float = 0.0,
    tighten_gaps: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> tuple[list[CaptionSegment], str, str]:
    sync_mode = (sync_mode or "better").lower().strip()
    text_source_mode = (text_source or "whisper").lower().strip()
    detected = resolve_caption_language(text, language, prefer_script=prefer_script_language)
    if text_source_mode == "whisper" and (not audio_path or not Path(audio_path).exists()):
        return [], (language_code(language) if language_code(language) != "auto" else "auto"), "whisper-no-audio"
    if sync_mode == "better" and audio_path and Path(audio_path).exists():
        segments, detected_language, engine = better_sync_captions(
            audio_path=audio_path,
            fallback_text=text,
            duration=duration,
            language=language,
            max_words=max_words,
            offset=offset,
            model_size=model_size,
            text_source=text_source_mode,
            prefer_script_language=prefer_script_language,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )
        return polish_caption_segments(segments, duration, min_duration=min_duration, tighten_gaps=tighten_gaps), detected_language, engine
    if text_source_mode == "whisper":
        return [], (language_code(language) if language_code(language) != "auto" else "auto"), "whisper-not-run"
    segments = fast_sync_captions(text, duration, max_words=max_words, offset=offset)
    return polish_caption_segments(segments, duration, min_duration=min_duration, tighten_gaps=tighten_gaps), detected, "script-fast"


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        seconds += 1
        millis = 0
    total = int(seconds)
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{millis:03d}"


def write_srt(segments: list[CaptionSegment], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for idx, seg in enumerate(segments, 1):
        text = re.sub(r"\s+", " ", seg.text or "").strip()
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{_srt_time(seg.start)} --> {_srt_time(seg.end)}")
        lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path
_WHISPER_MODEL_CACHE: dict[tuple[str, str, str], object] = {}


def _persistent_whisper_model(model_size: str, device: str = "cpu", compute_type: str = "int8"):
    """Load one model per configuration and retain it for the worker lifetime."""
    key = (str(model_size or "base"), device, compute_type)
    model = _WHISPER_MODEL_CACHE.get(key)
    if model is None:
        from faster_whisper import WhisperModel  # type: ignore
        model = WhisperModel(key[0], device=device, compute_type=compute_type)
        _WHISPER_MODEL_CACHE[key] = model
    return model

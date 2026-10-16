"""Local PiperTTS and Pexels helpers for Auto Caption Video Studio.

This module keeps the app dependency-light while providing a reusable local
voice system and a persistent Pexels background library. Pexels images are saved
once into a general library folder, then reused randomly for future videos so
batch rendering does not require manual background preparation for every script.
"""
from __future__ import annotations

import hashlib
import math
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

import requests

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]
LogCallback = Callable[[str], None]

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

DEFAULT_STORY_KEYWORDS = [
    "nature landscape",
    "forest path",
    "mountains sky",
    "village road",
    "sunset clouds",
    "rain weather",
    "night moon",
    "river scenery",
]

DEFAULT_CREATOR_KEYWORDS = [
    "content creator",
    "recording studio",
    "microphone studio",
    "podcast microphone",
    "video creator",
]

_COMMON_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by", "can", "could", "dear",
    "did", "do", "does", "for", "from", "had", "has", "have", "he", "her", "here", "hers", "him", "his",
    "i", "if", "in", "into", "is", "it", "its", "me", "my", "of", "on", "or", "our", "ours", "she",
    "so", "that", "the", "their", "them", "they", "this", "to", "was", "we", "were", "what", "when",
    "where", "which", "who", "why", "will", "with", "would", "you", "your", "yours", "team",
}

CREATOR_EXPANSION_KEYWORDS = [
    "content creator", "video creator", "digital creator", "youtube creator", "youtube studio",
    "video content creator", "creator workspace", "creative office", "home studio", "recording studio",
    "voice recording", "voice over recording", "audio studio", "microphone studio", "podcast microphone",
    "person speaking microphone", "professional microphone", "studio microphone", "sound recording", "podcast studio",
    "video editing desk", "laptop workspace", "camera setup", "filming setup", "social media creator",
    "online creator", "media production", "content production", "studio lights", "creator desk",
    "video production studio", "audio production", "voice artist studio", "narration recording", "headphones microphone",
    "computer editing", "creative workspace", "modern office desk", "work from home studio", "influencer studio",
    "camera and microphone", "video blogger", "vlogger studio", "youtube workspace", "speaker microphone",
    "broadcast studio", "professional voiceover", "sound engineer studio", "creator laptop", "media creator",
]

STORY_EXPANSION_KEYWORDS = [
    "nature landscape", "forest path", "mountains sky", "village road", "sunset clouds", "rain weather",
    "night moon", "river scenery", "city street", "old house", "sad person", "family home",
    "market street", "temple prayer", "flower garden", "desert sand", "snow winter", "beach sea",
    "rural village", "rainy street", "dark clouds", "lonely road", "old village house", "dramatic sky",
    "emotional portrait", "woman portrait", "man portrait", "children playing", "mother and child", "farmer field",
    "empty road", "storm clouds", "moonlit night", "quiet village", "traditional house", "cinematic landscape",
    "morning sunrise", "evening village", "beautiful nature", "old street", "spiritual prayer", "busy market",
    "mountain valley", "river bridge", "forest road", "village woman", "elderly person", "sad woman",
    "hopeful sunrise", "dark room", "candle light", "family sitting", "rural home", "train station",
]

QUERY_VARIANT_WORDS = [
    "cinematic", "background", "wide shot", "realistic", "dramatic", "professional", "modern", "close up",
    "workspace", "studio", "portrait", "scene", "atmosphere", "story background", "film still", "high quality",
]



def app_root() -> Path:
    return Path(__file__).resolve().parent.parent


def piper_models_dir() -> Path:
    return app_root() / "piper_models"


def pexels_library_dir() -> Path:
    """Persistent reusable Pexels photo library.

    On VPS this is outside the release/current directory so deployments cannot
    erase downloaded photos. Desktop behavior remains portable by default.
    """
    configured = str(os.environ.get("AUTO_CAPTION_PEXELS_LIBRARY", "") or "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    return app_root() / "pexels_background_library"


def _path_parts_for_detection(value: str | Path | None) -> list[str]:
    text = str(value or "").replace("\\", "/").strip()
    return [part.lower() for part in text.split("/") if part and part not in {".", ".."}]


def looks_like_app_default_pexels_library(path: str | Path | None) -> bool:
    """Return True for old default library paths from another app location.

    Example fixed by this helper:
    Downloads/smart_scroll_video_editor_piper_pexels_auto_studio/pexels_background_library
    should become the current app folder's pexels_background_library after the
    user moves the software folder to Documents or another drive.
    """
    parts = _path_parts_for_detection(path)
    if len(parts) < 2 or parts[-1] != "pexels_background_library":
        return False
    return parts[-2] in {
        "smart_scroll_video_editor_piper_pexels_auto_studio",
        "auto_caption_video_studio",
        "auto caption video studio",
    }


def resolve_pexels_library_dir(path: str | Path | None = None) -> Path:
    """Resolve the Pexels library folder.

    Blank or old default app-package paths are normalized to this running
    app's default library folder. Any other folder is treated as a user-selected
    custom library.
    """
    if path is None or not str(path).strip() or looks_like_app_default_pexels_library(path):
        return pexels_library_dir()
    return Path(os.path.expandvars(os.path.expanduser(str(path).strip())))


def find_piper_models(search_dirs: Iterable[Path] | None = None) -> list[Path]:
    """Return sorted Piper .onnx model files from local piper_models folders."""
    dirs = list(search_dirs or [piper_models_dir(), app_root() / "smart_scroll" / "piper_models"])
    seen: set[str] = set()
    models: list[Path] = []
    for folder in dirs:
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.onnx")):
            key = str(path.resolve()).lower()
            if key not in seen:
                models.append(path)
                seen.add(key)
    return models


def find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    root = app_root()
    candidates = []
    if os.name == "nt":
        candidates.append(name + ".exe")
    candidates.append(name)
    for rel in (Path("bin"), Path("tools") / name, Path(".")):
        for candidate in candidates:
            p = root / rel / candidate
            if p.exists():
                return str(p)
    return None


def _emit(callback: ProgressCallback | None, percent: int, message: str) -> None:
    if callback:
        callback(max(0, min(100, int(percent))), message)


def _log(callback: LogCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _cancelled(cancel_callback: CancelCallback | None) -> bool:
    return bool(cancel_callback and cancel_callback())


def run_piper_tts(
    *,
    text: str,
    model_path: str | Path,
    output_wav: str | Path,
    speaker: str | None = None,
    length_scale: str | None = None,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> Path:
    """Generate a WAV file using PiperTTS via piper executable or python -m piper."""
    text = (text or "").strip()
    if not text:
        raise ValueError("PiperTTS text is empty.")
    model_path = Path(model_path)
    output_wav = Path(output_wav)
    if not model_path.exists():
        raise FileNotFoundError(f"Piper model not found: {model_path}")
    if model_path.suffix.lower() != ".onnx":
        _log(log_callback, "Warning: Piper model should usually be a .onnx file.")

    output_wav.parent.mkdir(parents=True, exist_ok=True)
    piper_bin = find_binary("piper")
    cmd = [piper_bin] if piper_bin else [sys.executable, "-m", "piper"]
    cmd.extend(["--model", str(model_path), "--output_file", str(output_wav)])
    if speaker and str(speaker).strip():
        cmd.extend(["--speaker", str(speaker).strip()])
    if length_scale and str(length_scale).strip():
        cmd.extend(["--length_scale", str(length_scale).strip()])

    _emit(progress_callback, 4, "Generating voice with PiperTTS")
    _log(log_callback, "Generating voice with PiperTTS...")
    _log(log_callback, f"Piper model: {model_path}")
    _log(log_callback, f"Voice output: {output_wav}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    output_lines: list[str] = []
    try:
        assert process.stdin is not None
        process.stdin.write(text)
        process.stdin.close()
        assert process.stdout is not None
        while True:
            if _cancelled(cancel_callback):
                try:
                    process.terminate()
                except Exception:
                    pass
                raise KeyboardInterrupt("PiperTTS cancelled by user.")
            line = process.stdout.readline()
            if line:
                line = line.rstrip()
                output_lines.append(line)
                if line.strip():
                    _log(log_callback, f"Piper: {line.strip()}")
            if process.poll() is not None:
                break
        process.wait()
    finally:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except Exception:
            pass

    if process.returncode != 0:
        detail = "\n".join(output_lines[-12:])
        raise RuntimeError("PiperTTS failed. Install piper-tts and check the model path." + (f"\n{detail}" if detail else ""))
    if not output_wav.exists() or output_wav.stat().st_size == 0:
        raise RuntimeError("PiperTTS did not create a valid WAV file.")

    _emit(progress_callback, 100, "PiperTTS voice generated")
    _log(log_callback, "PiperTTS voice generated successfully.")
    return output_wav


def _dedupe_keep_order(values: Iterable[str], *, limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
        if limit is not None and len(result) >= limit:
            break
    return result



# Visual keyword engine -------------------------------------------------------
# Two independent outputs are generated from every script:
#   1) concrete single-word nouns for broad/fallback searches;
#   2) scene phrases for story-relevant primary searches.
# The implementation is deliberately deterministic so queue retries produce the
# same media search plan without depending on an online language model.

_VISUAL_NOUN_ALIASES: dict[str, str] = {
    # People
    "man": "man", "men": "man", "gentleman": "man", "father": "father",
    "woman": "woman", "women": "woman", "lady": "woman", "mother": "mother",
    "boy": "boy", "girl": "girl", "child": "child", "children": "child",
    "neighbor": "neighbor", "neighbour": "neighbor", "widow": "widow",
    "traveler": "traveler", "traveller": "traveler", "stranger": "stranger",
    "family": "family", "couple": "couple", "wife": "wife", "husband": "husband",
    # Places / structures
    "village": "village", "town": "town", "city": "city", "house": "house",
    "home": "house", "room": "room", "kitchen": "kitchen", "bedroom": "bedroom",
    "garden": "garden", "road": "road", "street": "street", "field": "field",
    "farm": "farm", "forest": "forest", "river": "river", "lake": "lake",
    "mountain": "mountain", "beach": "beach", "market": "market", "shop": "shop",
    "hospital": "hospital", "school": "school", "office": "office", "church": "church",
    "mosque": "mosque", "temple": "temple", "porch": "porch", "fireplace": "fireplace",
    # Objects
    "wall": "wall", "walls": "wall", "chair": "chair", "door": "door",
    "window": "window", "lamp": "lamp", "lantern": "lantern", "boot": "boots",
    "boots": "boots", "shoe": "shoes", "shoes": "shoes", "table": "table",
    "plate": "plate", "plates": "plate", "clothes": "clothes", "box": "box",
    "letter": "letter", "letters": "letter", "photograph": "photograph", "photo": "photograph",
    "picture": "photograph", "cloth": "cloth", "firewood": "firewood", "wood": "firewood",
    "tea": "tea", "coffee": "coffee", "soup": "soup", "food": "food",
    "money": "money", "gold": "gold", "jewelry": "jewelry", "jewellery": "jewelry",
    "book": "book", "books": "book", "phone": "phone", "car": "car", "bicycle": "bicycle",
    "tree": "tree", "trees": "tree", "flower": "flower", "flowers": "flower",
    "rain": "rain", "snow": "snow", "moon": "moon", "sun": "sun", "cloud": "cloud",
}

_PERSON_NOUNS = {"man", "woman", "father", "mother", "boy", "girl", "child", "neighbor", "widow", "traveler", "stranger", "wife", "husband", "couple", "family"}
_LOCATION_NOUNS = {"village", "town", "city", "house", "room", "kitchen", "bedroom", "garden", "road", "street", "field", "farm", "forest", "river", "lake", "mountain", "beach", "market", "shop", "hospital", "school", "office", "church", "mosque", "temple", "porch", "fireplace"}

# Search-friendly visual action phrases. Only actions that can be represented in
# stock footage are included; abstract verbs are intentionally ignored.
_ACTION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("sitting", (" sat ", " sits ", " sitting ")), 
    ("standing", (" stood ", " stands ", " standing ")),
    ("walking", (" walked ", " walks ", " walking ")),
    ("talking", (" asked ", " said ", " spoke ", " talking ", " listened ")),
    ("opening", (" opened ", " opens ", " opening ")),
    ("unlocking", (" unlocked ", " unlocks ", " unlocking ")),
    ("knocking", (" knocked ", " knocks ", " knocking ")),
    ("carrying", (" carried ", " carry ", " carrying ")),
    ("repairing", (" repaired ", " repairs ", " repairing ", " fixed ", " fixing ")),
    ("helping", (" helped ", " helps ", " helping ")),
    ("drinking", (" drinking ", " cup of tea", " cup of coffee")),
    ("resting", (" slept ", " sleeping ", " resting ")),
    ("cooking", (" cooked ", " cooking ")),
    ("driving", (" drove ", " driving ")),
    ("reading", (" read ", " reading ")),
    ("crying", (" cried ", " crying ", " tears ")),
    ("smiling", (" smiled ", " smiling ")),
]

_ADJECTIVES = {
    "old", "elderly", "young", "small", "large", "wooden", "rustic", "simple",
    "quiet", "empty", "open", "unlocked", "broken", "dark", "warm", "cold",
    "rural", "winter", "night", "evening", "front", "lonely",
}


def _sentences(text: str) -> list[str]:
    """Split narration into image-friendly visual beats, not only grammar sentences.

    Long narration sentences commonly contain two or three distinct actions. Keeping
    them together produces stale visuals and weak AI-image prompts. This splitter
    preserves order while breaking only at strong clause/action boundaries.
    """
    clean = re.sub(r"[\r\n]+", " ", text or " ")
    clean = re.sub(r"\s+", " ", clean).strip()
    base = [part.strip() for part in re.split(r"(?<=[.!?])\s+|[;]+", clean) if part.strip()]
    beats: list[str] = []
    boundary = re.compile(
        r"(?<=[,])\s+(?=(?:and|but|then|while|before|after|because|so)\b)"
        r"|\s+(?=(?:But|Then|Meanwhile|Later|Finally|Suddenly|Normally|Sometimes)\b)"
    )
    for sentence in base:
        words = sentence.split()
        if len(words) <= 18:
            beats.append(sentence)
            continue
        pieces = [part.strip(' ,') for part in boundary.split(sentence) if part.strip(' ,')]
        if len(pieces) == 1:
            # Conservative comma split for very long descriptive/action sentences.
            pieces = [part.strip(' ,') for part in re.split(r",\\s+(?=[A-Za-z])", sentence) if part.strip(' ,')]
        pending = ""
        for piece in pieces:
            candidate = f"{pending} {piece}".strip() if pending else piece
            if pending and len(candidate.split()) > 18:
                beats.append(pending)
                pending = piece
            else:
                pending = candidate
        if pending:
            beats.append(pending)
    return beats

def _sentence_nouns(sentence: str) -> list[str]:
    lower = sentence.casefold()
    words = re.findall(r"[A-Za-z]+", lower)
    nouns = [_VISUAL_NOUN_ALIASES[w] for w in words if w in _VISUAL_NOUN_ALIASES]
    if re.search(r"\bno\s+(money|gold|jewelry|jewellery)\b", lower):
        nouns = [n for n in nouns if n not in {"money", "gold", "jewelry"}]
    return _dedupe_keep_order(nouns)


def extract_visual_nouns(text: str, *, limit: int = 24) -> list[str]:
    """Return concrete, single-word visual nouns only.

    Proper names, verbs, adjectives, pronouns and abstract concepts never enter
    this list. Aliases and plurals are normalized (for example, ``walls`` ->
    ``wall`` and ``home`` -> ``house``).
    """
    nouns: list[str] = []
    for sentence in _sentences(text):
        nouns.extend(_sentence_nouns(sentence))
    return _dedupe_keep_order(nouns, limit=max(1, limit))


def _adjective_for_noun(sentence: str, noun: str) -> str:
    words = re.findall(r"[A-Za-z]+", sentence.casefold())
    aliases = {word: _VISUAL_NOUN_ALIASES.get(word) for word in words}
    for idx, word in enumerate(words):
        if aliases.get(word) != noun:
            continue
        if idx > 0 and words[idx - 1] in _ADJECTIVES:
            return words[idx - 1]
    if noun == "man" and ("old man" in sentence.casefold() or "elderly man" in sentence.casefold()):
        return "elderly"
    if noun == "woman" and ("old woman" in sentence.casefold() or "elderly woman" in sentence.casefold()):
        return "elderly"
    return ""


def _visual_action(sentence: str) -> str:
    padded = " " + re.sub(r"[^a-z]+", " ", sentence.casefold()).strip() + " "
    raw = sentence.casefold()
    for action, triggers in _ACTION_RULES:
        if any(trigger in padded or trigger.strip() in raw for trigger in triggers):
            return action
    return ""


def _compose_scene_query(sentence: str, nouns: list[str]) -> str:
    if not nouns:
        return ""
    people = [n for n in nouns if n in _PERSON_NOUNS]
    locations = [n for n in nouns if n in _LOCATION_NOUNS]
    objects = [n for n in nouns if n not in _PERSON_NOUNS and n not in _LOCATION_NOUNS]
    action = _visual_action(sentence)

    subject = people[0] if people else ""
    if subject:
        adj = _adjective_for_noun(sentence, subject)
        subject = f"{adj} {subject}".strip()

    # Keep scene searches compact: subject + action + one object + one location.
    parts: list[str] = []
    if subject:
        parts.append(subject)
    if action:
        parts.append(action)
    if objects:
        obj = objects[0]
        adj = _adjective_for_noun(sentence, obj)
        parts.append(f"{adj} {obj}".strip())
    if locations:
        loc = locations[0]
        adj = _adjective_for_noun(sentence, loc)
        loc_phrase = f"{adj} {loc}".strip()
        if parts:
            parts.append(f"in {loc_phrase}")
        else:
            parts.append(loc_phrase)

    # Object-led symbolic scenes (door, box, photograph, boots, etc.).
    if not subject and objects:
        parts = []
        for obj in objects[:3]:
            adj = _adjective_for_noun(sentence, obj)
            parts.append(f"{adj} {obj}".strip())
        if locations:
            parts.append(f"in {_adjective_for_noun(sentence, locations[0]) + ' ' if _adjective_for_noun(sentence, locations[0]) else ''}{locations[0]}")

    query = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return query if len(query.split()) >= 2 else ""


def extract_scene_plan(text: str, *, limit: int = 24) -> list[dict]:
    """Return ordered image-generation scenes without deleting repeated locations.

    Repeated queries are valid story beats (for example, returning to the same door
    later in the story). They must remain separate because scene IDs and timing are
    positional; uniqueness is handled by scene tags instead of query deduplication.
    """
    plan: list[dict] = []
    pending_nouns: list[str] = []
    pending_text: list[str] = []
    max_scenes = max(1, limit)

    for beat in _sentences(text):
        nouns = _sentence_nouns(beat)
        action = _visual_action(beat)
        word_count = len(re.findall(r"\b\w+\b", beat))

        # Non-visual fragments such as short dialogue answers attach to the next
        # visual beat, but long emotional/reaction lines remain usable AI scenes.
        if not nouns and not action and word_count < 9:
            pending_text.append(beat)
            continue

        pending_text.append(beat)
        pending_nouns.extend(nouns)
        merged_text = " ".join(pending_text).strip()
        merged_nouns = _dedupe_keep_order(pending_nouns)
        query = _compose_scene_query(merged_text, merged_nouns)

        if query:
            plan.append({
                "query": query,
                "text": merged_text,
                "word_count": max(1, len(re.findall(r"\b\w+\b", merged_text))),
            })
            pending_nouns, pending_text = [], []
        elif word_count >= 6:
            # Keep chronology even when the lightweight stock-search dictionary
            # cannot form a two-word query. This is especially important for AI
            # image generation, where the original visual beat is itself useful.
            fallback = " ".join(re.findall(r"[A-Za-z]+", merged_text)[:12]).strip()
            if fallback:
                plan.append({"query": fallback, "text": merged_text, "word_count": max(1, word_count)})
                pending_nouns, pending_text = [], []

    if pending_text and len(plan) < max_scenes:
        merged_text = " ".join(pending_text).strip()
        query = _compose_scene_query(merged_text, _dedupe_keep_order(pending_nouns))
        if not query:
            query = " ".join(re.findall(r"[A-Za-z]+", merged_text)[:12]).strip()
        if query:
            plan.append({"query": query, "text": merged_text, "word_count": max(1, len(re.findall(r"\b\w+\b", merged_text)))})

    if len(plan) <= max_scenes:
        return plan

    # Preserve the entire story when a cap is active. Merge the shortest adjacent
    # beats instead of dropping the ending after the scene limit is reached.
    compact = [dict(item) for item in plan]
    while len(compact) > max_scenes:
        merge_at = min(
            range(len(compact) - 1),
            key=lambda i: int(compact[i].get("word_count", 1)) + int(compact[i + 1].get("word_count", 1)),
        )
        left, right = compact[merge_at], compact[merge_at + 1]
        merged_text = f"{left.get('text', '')} {right.get('text', '')}".strip()
        merged_nouns = _sentence_nouns(merged_text)
        merged_query = _compose_scene_query(merged_text, merged_nouns)
        if not merged_query:
            merged_query = " ".join(re.findall(r"[A-Za-z]+", merged_text)[:12]).strip()
        compact[merge_at:merge_at + 2] = [{
            "query": merged_query,
            "text": merged_text,
            "word_count": max(1, len(re.findall(r"\b\w+\b", merged_text))),
        }]
    return compact

def extract_scene_queries(text: str, *, limit: int = 12) -> list[str]:
    """Create one concise, searchable phrase for each meaningful visual scene."""
    scenes: list[str] = []
    pending_nouns: list[str] = []
    pending_text: list[str] = []
    for sentence in _sentences(text):
        nouns = _sentence_nouns(sentence)
        if not nouns:
            continue
        pending_nouns.extend(nouns)
        pending_text.append(sentence)
        # A sentence with an action, person, or at least two concrete nouns is a
        # complete visual scene. Short descriptive fragments join the next line.
        action = _visual_action(sentence)
        people = [n for n in pending_nouns if n in _PERSON_NOUNS]
        if action or people or len(_dedupe_keep_order(pending_nouns)) >= 2:
            merged_text = " ".join(pending_text)
            merged_nouns = _dedupe_keep_order(pending_nouns)
            query = _compose_scene_query(merged_text, merged_nouns)
            if query:
                scenes.append(query)
            pending_nouns, pending_text = [], []
    if pending_nouns:
        query = _compose_scene_query(" ".join(pending_text), _dedupe_keep_order(pending_nouns))
        if query:
            scenes.append(query)
    return _dedupe_keep_order(scenes, limit=max(1, limit))


def _extract_raw_text_keywords(text: str, *, limit: int = 8) -> list[str]:
    """Backward-compatible alias: scene phrases are the primary script keywords."""
    return extract_scene_queries(text, limit=limit)


def _text_has_token(text: str, token: str) -> bool:
    token = str(token or "").strip().lower()
    if not token:
        return False
    if re.fullmatch(r"[a-z0-9 _-]+", token):
        pattern = r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return token in text


def _expand_queries_to_target(base_queries: Iterable[str], *, target_count: int | None = None, story_text: str = "") -> list[str]:
    """Keep a compact, relevant query set; never invent style-suffix variants."""
    target = max(1, min(24, int(target_count or 12)))
    base = _dedupe_keep_order(base_queries, limit=target)
    return base or DEFAULT_STORY_KEYWORDS[:target]


def build_story_queries(
    story_text: str,
    manual_query: str | None = None,
    disable_keyword_fetch: bool = False,
    target_count: int | None = None,
) -> list[str]:
    """Build a hybrid search plan: scene phrases first, noun fallbacks second."""
    target = max(1, min(24, int(target_count or 12)))
    manual_queries = [q.strip() for q in re.split(r"[,;|\n]", manual_query or "") if q.strip()]
    if disable_keyword_fetch:
        return _expand_queries_to_target(manual_queries or DEFAULT_STORY_KEYWORDS, target_count=target, story_text=story_text)

    scene_limit = max(1, (target + 1) // 2)
    noun_limit = max(1, target - scene_limit)
    scenes = extract_scene_queries(story_text or "", limit=max(scene_limit, 12))
    nouns = extract_visual_nouns(story_text or "", limit=max(noun_limit, 24))

    # Manual entries are explicit user intent and lead the list. Script scene
    # searches follow, then concrete one-word noun fallbacks fill any shortage.
    manual_room = max(0, target - len(manual_queries))
    scene_take = min(len(scenes), max(1, (manual_room + 1) // 2)) if manual_room else 0
    noun_take = max(0, manual_room - scene_take)
    combined = manual_queries + scenes[:scene_take] + nouns[:noun_take]
    return _expand_queries_to_target(combined, target_count=target, story_text=story_text)


def explain_story_queries(
    story_text: str,
    manual_query: str | None = None,
    disable_keyword_fetch: bool = False,
    target_count: int | None = None,
) -> dict:
    """Return separate noun and scene outputs for transparent UI logging."""
    target = max(1, min(24, int(target_count or 12)))
    manual = [q.strip() for q in re.split(r"[,;|\n]", manual_query or "") if q.strip()]
    nouns = extract_visual_nouns(story_text or "", limit=24) if not disable_keyword_fetch else []
    scenes = extract_scene_queries(story_text or "", limit=12) if not disable_keyword_fetch else []
    final_queries = build_story_queries(story_text, manual_query, disable_keyword_fetch, target_count=target)
    used_default = bool(final_queries) and not manual and not nouns and not scenes
    return {
        "manual_keywords": manual,
        "noun_keywords": nouns,
        "scene_queries": scenes,
        "script_keywords": scenes,
        "final_queries": final_queries,
        "used_default": used_default,
        "auto_keywords_enabled": not disable_keyword_fetch,
        "target_query_count": target,
        "final_query_count": len(final_queries),
    }


def choose_pexels_src(photo: dict) -> str | None:
    src = photo.get("src") or {}
    for key in ("large2x", "large", "landscape", "original", "medium"):
        value = src.get(key)
        if value:
            return value
    return None


def safe_pexels_extension(img_url: str) -> str:
    clean_path = urlparse(img_url).path
    ext = Path(clean_path).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"
    return ext


def _slugify(value: str, fallback: str = "pexels") -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "_", (value or fallback).strip()).strip("_").lower()
    return value[:48] or fallback



def has_background_image(folder: str | Path | None = None) -> bool:
    """Return quickly after finding one usable image; avoids full-library scans on the UI thread."""
    base = Path(folder) if folder else pexels_library_dir()
    if not base.exists() or not base.is_dir():
        return False
    try:
        for root, _dirs, files in os.walk(base):
            for name in files:
                path = Path(root) / name
                if path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                try:
                    if path.stat().st_size > 10_000:
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False

def list_background_images(folder: str | Path | None = None) -> list[Path]:
    """List valid images from the persistent Pexels/background folder."""
    base = Path(folder) if folder else pexels_library_dir()
    if not base.exists():
        return []
    results: list[Path] = []
    for path in base.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS and path.stat().st_size > 10_000:
            results.append(path)
    return sorted(results, key=lambda p: (p.parent.name.lower(), p.name.lower()))


def list_query_background_images(folder: str | Path | None, queries: Iterable[str]) -> list[Path]:
    """Return only backgrounds saved under folders matching the active queries."""
    base = Path(folder) if folder else pexels_library_dir()
    if not base.exists():
        return []
    query_paths: list[Path] = []
    for query in queries or []:
        qdir = base / _slugify(str(query), fallback="pexels")
        if qdir.exists():
            query_paths.extend(list_background_images(qdir))
    unique = _dedupe_keep_order([str(p) for p in query_paths])
    return [Path(p) for p in unique]


def list_background_videos(folder: str | Path | None = None) -> list[Path]:
    """List usable local video background files from a folder."""
    if not folder:
        return []
    base = Path(folder)
    if not base.exists():
        return []
    results: list[Path] = []
    for path in base.rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and path.stat().st_size > 100_000:
            results.append(path)
    return sorted(results, key=lambda p: (p.parent.name.lower(), p.name.lower()))


def list_query_background_videos(folder: str | Path | None, queries: Iterable[str]) -> list[Path]:
    """Return saved Pexels/local videos under folders matching active queries."""
    base = Path(folder) if folder else pexels_library_dir()
    if not base.exists():
        return []
    query_paths: list[Path] = []
    video_base = base / "pexels_videos"
    for query in queries or []:
        for qroot in (base / _slugify(str(query), fallback="pexels"), video_base / _slugify(str(query), fallback="pexels")):
            if qroot.exists():
                query_paths.extend(list_background_videos(qroot))
    unique = _dedupe_keep_order([str(p) for p in query_paths])
    return [Path(p) for p in unique]


def choose_pexels_video_file(video: dict, target_width: int = 1280) -> str | None:
    """Pick a practical MP4 video file from a Pexels video result."""
    files = video.get("video_files") or []
    candidates: list[dict] = []
    for item in files:
        link = str(item.get("link") or "").strip()
        file_type = str(item.get("file_type") or "").lower()
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        if not link or (file_type and "mp4" not in file_type) or ".mp4" not in link.lower():
            continue
        if width > 0 and height > 0 and width < height:
            continue
        candidates.append(item)
    if not candidates:
        for item in files:
            link = str(item.get("link") or "").strip()
            if link and ".mp4" in link.lower():
                candidates.append(item)
    if not candidates:
        return None

    def score(item: dict) -> tuple[int, int]:
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        # Prefer HD-ish files close to the project width, not huge originals.
        width_penalty = abs((width or target_width) - target_width)
        hd_bonus = 0 if width >= 960 and height >= 540 else 5000
        return (hd_bonus + width_penalty, -(width or 0))

    candidates.sort(key=score)
    return str(candidates[0].get("link") or "").strip()


def safe_pexels_video_extension(video_url: str) -> str:
    clean_path = urlparse(video_url).path
    ext = Path(clean_path).suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
        ext = ".mp4"
    return ext


def _video_filename_from_pexels(query: str, video: dict, video_url: str, ext: str) -> str:
    video_id = str(video.get("id") or "")
    if not video_id:
        video_id = hashlib.sha1(video_url.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{_slugify(query)}_{video_id}{ext}"


def download_pexels_video_clips(
    *,
    api_key: str,
    queries: list[str],
    needed: int,
    cache_dir: str | Path,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
    cancel_callback: CancelCallback | None = None,
    use_existing: bool = True,
    randomize: bool = True,
    target_width: int = 1280,
) -> list[Path]:
    """Download/reuse Pexels video clips for current-video background use."""
    cache_dir = Path(cache_dir)
    video_root = cache_dir / "pexels_videos"
    video_root.mkdir(parents=True, exist_ok=True)
    queries = _dedupe_keep_order(queries or DEFAULT_STORY_KEYWORDS.copy())
    needed = max(1, int(needed))

    existing = list_query_background_videos(cache_dir, queries) if use_existing else []
    if existing and len(existing) >= needed:
        _emit(progress_callback, 100, f"Using {needed} saved Pexels video clip(s)")
        _log(log_callback, f"Pexels video reuse: {len(existing)} saved query-matched video clip(s).")
        return _cycle_to_needed(existing, needed, randomize=randomize)

    if not api_key or not api_key.strip():
        if existing:
            _log(log_callback, f"No Pexels API key. Reusing {len(existing)} saved Pexels video clip(s).")
            return _cycle_to_needed(existing, needed, randomize=randomize)
        _log(log_callback, "No Pexels API key and no saved video clips. Pexels video backgrounds will be skipped; the base background will be used.")
        _emit(progress_callback, 100, "No Pexels video backgrounds available; using base background")
        return []

    per_query = 4
    headers = {"Authorization": api_key.strip()}
    session = requests.Session()
    session.headers.update(headers)
    downloaded: list[Path] = []
    seen_urls: set[str] = set()

    _log(log_callback, f"Pexels video query plan: {len(queries)} query/queries, need {needed} video clip(s), normally 1 clip per query.")
    for query_index, query in enumerate(queries, start=1):
        if _cancelled(cancel_callback):
            raise KeyboardInterrupt("Pexels video download cancelled by user.")
        _emit(progress_callback, min(95, 8 + int((query_index - 1) / max(1, len(queries)) * 72)), f"Searching Pexels videos: {query}")
        _log(log_callback, f"Searching Pexels videos: {query}")
        try:
            response = session.get(
                "https://api.pexels.com/videos/search",
                params={"query": query, "per_page": per_query, "orientation": "landscape"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            _log(log_callback, f"Pexels video search failed for '{query}': {exc}")
            continue

        videos = data.get("videos") or []
        if not videos:
            _log(log_callback, f"No Pexels video results for: {query}")
            continue

        query_dir = video_root / _slugify(query)
        query_dir.mkdir(parents=True, exist_ok=True)
        downloaded_this_query = 0
        for video in videos:
            if _cancelled(cancel_callback):
                raise KeyboardInterrupt("Pexels video download cancelled by user.")
            video_url = choose_pexels_video_file(video, target_width=target_width)
            if not video_url or video_url in seen_urls:
                continue
            seen_urls.add(video_url)
            try:
                ext = safe_pexels_video_extension(video_url)
                file_path = query_dir / _video_filename_from_pexels(query, video, video_url, ext)
                if file_path.exists() and file_path.stat().st_size > 100_000:
                    downloaded.append(file_path)
                    downloaded_this_query += 1
                    break
                video_response = session.get(video_url, timeout=60)
                video_response.raise_for_status()
                file_path.write_bytes(video_response.content)
                if file_path.stat().st_size < 100_000:
                    file_path.unlink(missing_ok=True)
                    raise RuntimeError("downloaded video was too small")
                downloaded.append(file_path)
                downloaded_this_query += 1
                current_total = len(existing) + len(downloaded)
                _emit(progress_callback, min(98, 20 + int(current_total / max(1, needed) * 70)), f"Saved Pexels video {current_total}/{needed}")
                _log(log_callback, f"Saved Pexels video: {file_path.name} | query: {query}")
            except Exception as exc:
                _log(log_callback, f"Pexels video download skipped for '{query}': {exc}")
            if downloaded_this_query >= 1:
                break
            if len(existing) + len(downloaded) >= needed:
                break
        if len(existing) + len(downloaded) >= needed:
            break

    library = list_query_background_videos(cache_dir, queries)
    if not library:
        raise RuntimeError("No quality Pexels videos could be downloaded. Check API key, internet, or query.")
    if len(library) < needed:
        _log(log_callback, f"Only {len(library)} saved Pexels video clip(s) available; reusing them to fill the full video duration.")
    _emit(progress_callback, 100, "Pexels video library ready")
    return _cycle_to_needed(library, needed, randomize=randomize)


def _video_motion_frame_filename(video_path: Path, segment_index: int) -> str:
    stem = re.sub(r"[^0-9A-Za-z_-]+", "_", video_path.stem).strip("_")[:48] or "video"
    digest = hashlib.sha1(str(video_path.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"{stem}_{digest}_seg{segment_index:04d}"


def prepare_pexels_video_background_frames(
    *,
    api_key: str,
    queries: list[str],
    needed: int,
    cache_dir: str | Path,
    interval_seconds: float = 4.0,
    frame_fps: float = 6.0,
    target_width: int = 1280,
    target_height: int = 720,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
    cancel_callback: CancelCallback | None = None,
    randomize: bool = True,
    local_video_paths: Iterable[str | Path] | None = None,
) -> list[Path]:
    """Download Pexels videos and convert each interval into motion frames.

    Long Pexels videos are cut to interval_seconds. Shorter videos are looped
    until the interval is filled, so the background never leaves a blank gap.
    Frames are loaded lazily by the renderer.
    """
    needed = max(1, int(needed))
    interval_seconds = max(1.0, float(interval_seconds or 4.0))
    frame_fps = max(1.0, min(12.0, float(frame_fps or 6.0)))
    frames_per_segment = max(1, int(math.ceil(interval_seconds * frame_fps)))

    clips: list[Path] = []
    local_clean = []
    for raw in local_video_paths or []:
        path = Path(str(raw))
        if path.exists() and path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            local_clean.append(path)
    if local_clean:
        clips.extend(_cycle_to_needed(local_clean, needed, randomize=randomize))
        _log(log_callback, f"Using {len(local_clean)} optional local video clip(s) as fallback/mix.")
    if len(clips) < needed:
        clips = download_pexels_video_clips(
            api_key=api_key,
            queries=queries,
            needed=needed,
            cache_dir=cache_dir,
            progress_callback=progress_callback,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            use_existing=True,
            randomize=randomize,
            target_width=target_width,
        )

    from .ffmpeg_utils import get_audio_duration, require_ffmpeg

    ffmpeg = require_ffmpeg()
    frame_root = Path(cache_dir) / "pexels_video_frames"
    frame_root.mkdir(parents=True, exist_ok=True)
    all_frames: list[Path] = []

    _log(log_callback, f"Pexels video intervals: {needed} clip segment(s), interval {interval_seconds:g}s, motion preview {frame_fps:g} fps.")
    for segment_index in range(needed):
        if _cancelled(cancel_callback):
            raise KeyboardInterrupt("Pexels video frame preparation cancelled by user.")
        clip = clips[segment_index % len(clips)]
        try:
            source_duration = get_audio_duration(str(clip))
        except Exception:
            source_duration = interval_seconds
        source_duration = max(0.5, float(source_duration or interval_seconds))
        clip_dir = frame_root / _video_motion_frame_filename(clip, segment_index + 1)
        clip_dir.mkdir(parents=True, exist_ok=True)
        pattern = clip_dir / "frame_%04d.jpg"
        existing_frames = sorted(clip_dir.glob("frame_*.jpg"))
        if len(existing_frames) < frames_per_segment:
            for old in existing_frames:
                old.unlink(missing_ok=True)
            long_clip = source_duration > interval_seconds + 0.2
            if long_clip:
                max_start = max(0.0, source_duration - interval_seconds)
                start_at = (segment_index * interval_seconds) % max(0.1, max_start)
                loop_args: list[str] = ["-ss", f"{start_at:.3f}", "-i", str(clip), "-t", f"{interval_seconds:.3f}"]
                _log(log_callback, f"Pexels video longer than {interval_seconds:g}s; cutting clip to {interval_seconds:g}s: {clip.name}")
            else:
                loop_args = ["-stream_loop", "-1", "-i", str(clip), "-t", f"{interval_seconds:.3f}"]
                _log(log_callback, f"Pexels video shorter than {interval_seconds:g}s; looping/adjusting to fit interval: {clip.name}")
            vf = f"fps={frame_fps:.3f},scale={target_width}:{target_height}:force_original_aspect_ratio=increase,crop={target_width}:{target_height}"
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *loop_args,
                "-vf",
                vf,
                "-an",
                "-q:v",
                "3",
                str(pattern),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=max(60, int(interval_seconds * 20)))
            if result.returncode != 0:
                raise RuntimeError((result.stderr or "FFmpeg could not prepare Pexels video background frames.").strip())
            existing_frames = sorted(clip_dir.glob("frame_*.jpg"))

        if not existing_frames:
            raise RuntimeError(f"No frames were created from Pexels video: {clip}")
        if len(existing_frames) < frames_per_segment:
            existing_frames = _cycle_to_needed(existing_frames, frames_per_segment, randomize=False)
        segment_frames = existing_frames[:frames_per_segment]
        all_frames.extend(segment_frames)
        _emit(progress_callback, min(98, int((segment_index + 1) / max(1, needed) * 100)), f"Prepared Pexels video segment {segment_index + 1}/{needed}")

    return all_frames



def _video_frame_filename(video_path: Path, frame_index: int, at_seconds: float) -> str:
    stem = re.sub(r"[^0-9A-Za-z_-]+", "_", video_path.stem).strip("_")[:60] or "video"
    digest = hashlib.sha1(str(video_path.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"{stem}_{digest}_{frame_index:04d}_{int(at_seconds * 1000):08d}.jpg"


def prepare_video_background_frames(
    *,
    video_paths: Iterable[str | Path],
    needed: int,
    cache_dir: str | Path,
    interval_seconds: float = 4.0,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
    cancel_callback: CancelCallback | None = None,
    randomize: bool = True,
) -> list[Path]:
    """Convert local video clips into current-video background frame images.

    This keeps rendering reliable on older PCs: videos are accepted as a media
    source, and the app extracts strong still frames into the same background
    sequence system instead of trying to decode multiple MP4 streams live during
    caption rendering.
    """
    clean_paths: list[Path] = []
    for raw in video_paths or []:
        path = Path(str(raw))
        if path.exists() and path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            clean_paths.append(path)
    if not clean_paths or needed <= 0:
        return []
    if randomize:
        random.shuffle(clean_paths)

    from .ffmpeg_utils import get_audio_duration, require_ffmpeg

    ffmpeg = require_ffmpeg()
    cache_dir = Path(cache_dir) / "video_frames"
    cache_dir.mkdir(parents=True, exist_ok=True)
    needed = max(1, int(needed))
    interval_seconds = max(1.0, float(interval_seconds or 4.0))
    extracted: list[Path] = []

    _log(log_callback, f"Preparing video background frames: {needed} frame(s) from {len(clean_paths)} local video file(s).")
    attempts = 0
    index = 0
    while len(extracted) < needed and attempts < needed * 4:
        if _cancelled(cancel_callback):
            raise KeyboardInterrupt("Video background preparation cancelled by user.")
        video = clean_paths[index % len(clean_paths)]
        index += 1
        attempts += 1
        try:
            duration = get_audio_duration(str(video))
        except Exception:
            duration = max(1.0, interval_seconds)
        safe_duration = max(1.0, float(duration or interval_seconds))
        # Spread frames through the clip; avoid exact 0s because intros are often black.
        at_seconds = min(max(0.4, (attempts * interval_seconds) % safe_duration), max(0.4, safe_duration - 0.2))
        out_path = cache_dir / _video_frame_filename(video, len(extracted) + 1, at_seconds)
        if not out_path.exists() or out_path.stat().st_size < 10_000:
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{at_seconds:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or "FFmpeg could not extract a frame.").strip())
        if out_path.exists() and out_path.stat().st_size > 10_000:
            extracted.append(out_path)
            _emit(progress_callback, min(98, int(len(extracted) / needed * 100)), f"Prepared video background frame {len(extracted)}/{needed}")
    if not extracted:
        raise RuntimeError("No video background frames could be prepared. Check the selected video files and FFmpeg.")
    if len(extracted) < needed:
        _log(log_callback, f"Only {len(extracted)} video frame(s) prepared; reusing them to fill the video.")
    return _cycle_to_needed(extracted, needed, randomize=randomize)



def _cycle_to_needed(paths: list[Path], needed: int, randomize: bool = True) -> list[Path]:
    if not paths:
        return []
    pool = list(paths)
    if randomize:
        random.shuffle(pool)
    needed = max(1, int(needed))
    result: list[Path] = []
    while len(result) < needed:
        if randomize:
            random.shuffle(pool)
        result.extend(pool)
    return result[:needed]


def _photo_filename(query: str, photo: dict, img_url: str, ext: str) -> str:
    photo_id = str(photo.get("id") or "")
    if not photo_id:
        photo_id = hashlib.sha1(img_url.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{_slugify(query)}_{photo_id}{ext}"


def download_pexels_backgrounds(
    *,
    api_key: str,
    queries: list[str],
    needed: int,
    cache_dir: str | Path,
    progress_callback: ProgressCallback | None = None,
    log_callback: LogCallback | None = None,
    cancel_callback: CancelCallback | None = None,
    use_existing: bool = True,
    randomize: bool = True,
    min_library_size: int | None = None,
) -> list[Path]:
    """Return local background images, downloading only when needed.

    Existing images in cache_dir are preferred, but query-matched folders now take
    priority so script keywords are actually used instead of always falling back to
    old generic library images.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    queries = _dedupe_keep_order(queries or DEFAULT_STORY_KEYWORDS.copy())
    needed = max(1, int(needed))
    min_library_size = max(needed, int(min_library_size or needed))

    existing = list_background_images(cache_dir) if use_existing else []
    query_existing = list_query_background_images(cache_dir, queries) if use_existing else []
    if query_existing and len(query_existing) >= needed:
        _emit(progress_callback, 100, f"Using {needed} script-matched background(s) from saved Pexels library")
        _log(log_callback, f"Pexels query-folder reuse: {len(query_existing)} image(s) matched current script queries in {cache_dir}")
        return _cycle_to_needed(query_existing, needed, randomize=randomize)

    if not api_key or not api_key.strip():
        if query_existing:
            _log(log_callback, f"No Pexels API key. Reusing {len(query_existing)} query-matched saved image(s).")
            return _cycle_to_needed(query_existing, needed, randomize=randomize)
        if existing:
            _log(log_callback, f"No Pexels API key. Query-matched images were not found, so reusing {len(existing)} generic saved image(s) from library.")
            return _cycle_to_needed(existing, needed, randomize=randomize)
        _log(log_callback, "No Pexels API key and no saved images. Pexels image backgrounds will be skipped; the base background will be used.")
        _emit(progress_callback, 100, "No Pexels image backgrounds available; using base background")
        return []

    # Force script-keyword search when query-matched folders are missing/short,
    # even if the overall generic library already has many images.
    target_downloads = max(needed, min_library_size) - len(query_existing)
    min_query_searches = min(max(1, needed), len(queries))
    max_per_query = 1 if len(queries) >= needed else max(1, math.ceil(max(1, target_downloads) / max(1, len(queries))))
    # Fetch only a few candidates per query. This keeps current-video mode fast and
    # encourages variety: a 44-image video searches roughly 44 different queries
    # instead of downloading many images from the first few queries.
    per_query = max(3, min(8, max_per_query + 2))
    headers = {"Authorization": api_key.strip()}
    session = requests.Session()
    session.headers.update(headers)
    seen_urls: set[str] = set()
    downloaded: list[Path] = []
    download_failures = 0

    _log(log_callback, f"Pexels current-video query plan: {len(queries)} query/queries available, searching at least {min_query_searches} where needed, max {max_per_query} download(s) per query.")
    _log(log_callback, f"Pexels current-video cache: {len(existing)} total saved image(s), {len(query_existing)} matching current script queries, downloading up to {target_downloads} more into {cache_dir}")
    for query_index, query in enumerate(queries, start=1):
        if _cancelled(cancel_callback):
            raise KeyboardInterrupt("Pexels download cancelled by user.")
        _emit(progress_callback, min(95, 8 + int((query_index - 1) / max(1, len(queries)) * 72)), f"Searching Pexels: {query}")
        _log(log_callback, f"Searching Pexels: {query}")
        try:
            response = session.get(
                "https://api.pexels.com/v1/search",
                params={"query": query, "per_page": per_query, "orientation": "landscape"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            _log(log_callback, f"Pexels search failed for '{query}': {exc}")
            continue

        photos = data.get("photos") or []
        if not photos:
            _log(log_callback, f"No Pexels results for: {query}")
            continue

        query_dir = cache_dir / _slugify(query)
        query_dir.mkdir(parents=True, exist_ok=True)
        downloaded_this_query = 0
        for photo in photos:
            if _cancelled(cancel_callback):
                raise KeyboardInterrupt("Pexels download cancelled by user.")
            img_url = choose_pexels_src(photo)
            if not img_url or img_url in seen_urls:
                continue
            seen_urls.add(img_url)
            try:
                ext = safe_pexels_extension(img_url)
                file_path = query_dir / _photo_filename(query, photo, img_url, ext)
                if file_path.exists() and file_path.stat().st_size > 10_000:
                    downloaded.append(file_path)
                    downloaded_this_query += 1
                    break
                img_response = session.get(img_url, timeout=30)
                img_response.raise_for_status()
                file_path.write_bytes(img_response.content)
                if file_path.stat().st_size < 10_000:
                    file_path.unlink(missing_ok=True)
                    raise RuntimeError("downloaded image was too small")
                downloaded.append(file_path)
                downloaded_this_query += 1
                current_total = len(query_existing) + len(downloaded)
                _emit(progress_callback, min(98, 20 + int(current_total / max(1, min_library_size) * 70)), f"Saved script-matched background {current_total}/{min_library_size}")
                _log(log_callback, f"Saved Pexels background: {file_path.name} | query: {query}")
            except Exception as exc:
                download_failures += 1
                if download_failures <= 5:
                    _log(log_callback, f"Pexels download skipped for '{query}': {exc}")
                elif download_failures == 6:
                    _log(log_callback, "More Pexels download errors hidden to keep logs clean.")
            if downloaded_this_query >= max_per_query:
                break
            if len(query_existing) + len(downloaded) >= min_library_size:
                break
        if len(query_existing) + len(downloaded) >= min_library_size:
            break

    library = list_background_images(cache_dir)
    matched_library = list_query_background_images(cache_dir, queries)
    if not library:
        raise RuntimeError("No quality Pexels backgrounds could be downloaded. Check API key, internet, or query.")
    if matched_library:
        if len(matched_library) < needed:
            _log(log_callback, f"Only {len(matched_library)} script-matched background(s) available; reusing them to fill the full video duration.")
        _emit(progress_callback, 100, "Pexels script-matched background library ready")
        return _cycle_to_needed(matched_library, needed, randomize=randomize)
    if len(library) < needed:
        _log(log_callback, f"Only {len(library)} saved background(s) available; reusing them to fill the full video duration.")
    _log(log_callback, "No script-matched background folders were found after download; falling back to general library results.")
    _emit(progress_callback, 100, "Pexels background library ready")
    return _cycle_to_needed(library, needed, randomize=randomize)


def estimate_pexels_needed(duration_seconds: float, interval_seconds: float) -> int:
    interval_seconds = max(1.0, float(interval_seconds or 8.0))
    return max(1, int(math.ceil(max(1.0, float(duration_seconds or 1.0)) / interval_seconds)))

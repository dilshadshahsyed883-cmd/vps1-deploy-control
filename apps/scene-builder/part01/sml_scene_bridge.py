"""Read-only SML catalog bridge used by Scene Builder's media audit.

SML remains the source-of-truth for its own clips, database, Drive verification
and lifecycle. Scene Builder imports a *job-scoped copy* through the existing
worker upload API; it must never mutate SML records or delete library media.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8147"
MAX_CLIP_BYTES = 256 * 1024 * 1024
TOKEN = re.compile(r"[^\W_]+", flags=re.UNICODE)
CLIP_PATH = re.compile(r"^/media/clips/[A-Za-z0-9._-]+\.mp4$")
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "is",
    "it", "of", "on", "or", "the", "to", "with", "video", "clip", "scene",
})


class SMLUnavailable(RuntimeError):
    """SML is unreachable or does not have a safe, usable clip."""


def library_url() -> str:
    # Internal server-to-server address; never accept arbitrary URLs from a
    # browser request. A deployment may override this via a service environment.
    value = os.getenv("SCENE_SML_URL", DEFAULT_URL).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise SMLUnavailable("SCENE_SML_URL must be a service origin")
    return value


def _get(path: str, params: dict[str, object] | None = None) -> bytes:
    url = library_url() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=8) as response:
            return response.read(3 * 1024 * 1024 + 1)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise SMLUnavailable("SML service is currently unavailable") from exc


def approved_clips(limit: int = 200) -> list[dict[str, Any]]:
    """Fetch up to 200 approved clips; rejected/trashed clips are never offered."""
    limit = max(1, min(200, int(limit)))
    try:
        payload = json.loads(_get("/api/clips", {
            "status": "approved", "trash": "false", "sort": "quality", "limit": limit,
        }))
    except (ValueError, TypeError) as exc:
        raise SMLUnavailable("SML returned an invalid media catalog") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise SMLUnavailable("SML catalog format is unsupported")
    return [r for r in payload["items"] if isinstance(r, dict) and eligible(r)]


def eligible(clip: dict[str, Any]) -> bool:
    if clip.get("status") != "approved" or clip.get("deleted_at"):
        return False
    if str(clip.get("duplicate_state") or "clear").lower() == "duplicate":
        return False
    if not CLIP_PATH.fullmatch(str(clip.get("clip_url") or "")):
        return False
    return bool(re.fullmatch(r"CLP-[A-Za-z0-9_-]+", str(clip.get("id") or "")))


def terms(value: object) -> set[str]:
    return {word for word in TOKEN.findall(str(value or "").casefold())
            if len(word) >= 3 and word not in STOPWORDS}


def rank_clips(scene: dict[str, Any], clips: list[dict[str, Any]],
               used: set[str] | None = None) -> list[dict[str, Any]]:
    """Deterministic match from requested scene keyword to approved SML metadata.

    If no keyword matches, return [] so the established Pexels selection stays.
    """
    used = used or set()
    needle = terms(scene.get("query_override") or scene.get("keyword_used") or
                   scene.get("original_query") or scene.get("narration"))
    if not needle:
        return []
    desired = max(0.0, float(scene.get("final_end") or 0) -
                  float(scene.get("final_start") or 0))
    scored = []
    for clip in clips:
        if not eligible(clip) or str(clip.get("id")) in used:
            continue
        kw = clip.get("keywords") or []
        tags = " ".join(str(x.get("keyword", "")) if isinstance(x, dict) else str(x)
                        for x in kw)
        haystack = terms(tags + " " + str(clip.get("description") or ""))
        overlap = len(needle & haystack)
        if not overlap:
            continue
        try:
            quality = min(100.0, max(0.0, float(clip.get("quality") or 0)))
            duration = max(0.0, float(clip.get("duration") or 0))
        except (TypeError, ValueError):
            continue
        orientation = str(clip.get("source_orientation") or "")
        fitness = max(0.0, 8.0 - abs(desired - duration) * 3) if desired else 0
        score = 100.0 * overlap / len(needle) + 6.0 * overlap + quality * .15 + fitness
        if orientation == "landscape":
            score += 3
        scored.append((score, str(clip.get("id")), clip))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [entry[2] for entry in scored]


def download_verified(clip: dict[str, Any], destination: Path) -> Path:
    """Download only an approved SML MP4, bounded and hash-checked."""
    if not eligible(clip):
        raise SMLUnavailable("Clip is not an approved SML media asset")
    path = str(clip["clip_url"])
    if not CLIP_PATH.fullmatch(path):
        raise SMLUnavailable("Unsafe clip path")
    expected_sha = str(clip.get("sha256") or "").lower()
    if expected_sha and not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise SMLUnavailable("Invalid SML clip checksum")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(library_url() + path, timeout=45) as response, temporary.open("wb") as sink:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_CLIP_BYTES:
                    raise SMLUnavailable("SML clip exceeds safe transfer size")
                digest.update(chunk)
                sink.write(chunk)
        if size == 0 or (expected_sha and digest.hexdigest() != expected_sha):
            raise SMLUnavailable("SML clip is empty or failed integrity verification")
        temporary.replace(destination)
        return destination
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise SMLUnavailable("SML clip could not be downloaded") from exc
    finally:
        temporary.unlink(missing_ok=True)

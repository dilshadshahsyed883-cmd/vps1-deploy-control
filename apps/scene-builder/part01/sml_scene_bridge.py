"""Scene Builder adapter for Self Media Library's *authenticated V2 Editor API*.

SML owns approval, Drive restore, catalog, reservations and clip data.
Scene Builder owns only its job-scoped replacement copy. No V1/static bypass.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8147"  # override after live service discovery
MAX_CLIP_BYTES = 256 * 1024 * 1024
TOKEN = re.compile(r"[^\W_]+", flags=re.UNICODE)
CLIP_ID = re.compile(r"^CLP-[A-Za-z0-9_-]+$")
RES_ID = re.compile(r"^RES-[0-9a-f]{32}$")
STOPWORDS = frozenset({"a", "an", "and", "are", "as", "at", "by", "for", "from", "in",
                       "is", "it", "of", "on", "or", "the", "to", "with", "video", "clip", "scene"})


class SMLUnavailable(RuntimeError):
    """An unavailable V2 Editor API must leave existing Scene media untouched."""


def library_url() -> str:
    value = os.getenv("SCENE_SML_URL", DEFAULT_URL).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.path not in {"", "/"} or
            parsed.query or parsed.fragment):
        raise SMLUnavailable("SCENE_SML_URL must be an internal service origin")
    return value


def editor_token() -> str:
    # The SAME value must be provisioned separately to SML_EDITOR_API_TOKEN
    # on the SML service and SCENE_SML_EDITOR_TOKEN on the Scene service.
    value = os.getenv("SCENE_SML_EDITOR_TOKEN", "").strip()
    if not value:
        raise SMLUnavailable("SML V2 Editor API credential is not configured")
    return value


def _request(path: str, *, method: str = "GET", params: dict[str, object] | None = None,
             body: dict[str, object] | None = None) -> dict[str, Any]:
    url = library_url() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "X-SML-Editor-Token": editor_token()}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    try:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read(3 * 1024 * 1024 + 1)
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Expected object")
        return result
    except (OSError, ValueError, TimeoutError, urllib.error.URLError) as exc:
        # Do not log the bearer credential or leak the remote response body.
        raise SMLUnavailable("SML V2 editor service unavailable or access denied") from exc


def library_status() -> dict[str, Any]:
    # Reuse the existing authenticated search contract; total is not the page length.
    counts = {}
    for kind, label in (("video", "clips"), ("image", "images")):
        result = _request("/api/v1/media/search", params={"media_type": kind, "limit": 1})
        if not isinstance(result.get("items"), list) or not isinstance(result.get("total"), int):
            raise SMLUnavailable("SML catalog status is unavailable")
        counts[label] = max(0, result["total"])
    status = {"connected": True, **counts}
    # Search totals are searchable/available approved media, not every asset
    # the owner can see on the SML Library page.
    try:
        inventory = _request("/api/v1/media/inventory").get("counts") or {}
        if all(isinstance(inventory.get(kind), dict) for kind in ("video", "image")):
            status["inventory"] = inventory
    except SMLUnavailable:
        pass  # Compatible with previously deployed V2 Editor API versions.
    return status


def approved_clips(limit: int = 100, query: str = "") -> list[dict[str, Any]]:
    limit = max(1, min(100, int(limit)))
    result = _request("/api/v1/media/search", params={
        "media_type": "video", "query": query[:200], "limit": limit,
    })
    if not isinstance(result.get("items"), list):
        raise SMLUnavailable("SML V2 catalog format is unsupported")
    return [x for x in result["items"] if isinstance(x, dict) and eligible(x)]


def eligible(clip: dict[str, Any]) -> bool:
    clip_id = str(clip.get("id") or "")
    return (bool(CLIP_ID.fullmatch(clip_id))
            and str(clip.get("type") or "") == "video"
            and str(clip.get("content_url") or "") ==
            f"/api/v1/media/video/{clip_id}/content")


def terms(value: object) -> set[str]:
    return {word for word in TOKEN.findall(str(value or "").casefold())
            if len(word) >= 3 and word not in STOPWORDS}


def rank_clips(scene: dict[str, Any], clips: list[dict[str, Any]],
               used: set[str] | None = None) -> list[dict[str, Any]]:
    used = used or set()
    needle = terms(scene.get("query_override") or scene.get("keyword_used") or
                   scene.get("original_query") or scene.get("narration"))
    if not needle:
        return []  # No arbitrary clip: the existing Pexels result stays.
    desired = max(0.0, float(scene.get("final_end") or 0) -
                  float(scene.get("final_start") or 0))
    scored = []
    for clip in clips:
        if not eligible(clip) or str(clip.get("id")) in used:
            continue
        kw = clip.get("keywords") or []
        tags = " ".join(str(x.get("keyword", "")) if isinstance(x, dict) else str(x)
                        for x in kw)
        haystack = terms(tags + " " + str(clip.get("description") or "") + " " + str(clip.get("source_filename") or ""))
        overlap = len(needle & haystack)
        if not overlap:
            continue
        try:
            quality = min(100.0, max(0.0, float(clip.get("quality") or 0)))
            duration = max(0.0, float(clip.get("duration") or 0))
        except (TypeError, ValueError):
            continue
        fitness = max(0.0, 8.0 - abs(desired - duration) * 3) if desired else 0
        score = 100.0 * overlap / len(needle) + 6.0 * overlap + quality * .15 + fitness
        scored.append((score, str(clip.get("id")), clip))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored]


def reserve_for_scene(project_id: str, scene_id: str, query: str,
                      preferred_id: str, exclude_ids: set[str] | None = None) -> dict[str, Any] | None:
    if not CLIP_ID.fullmatch(preferred_id):
        raise SMLUnavailable("Invalid preferred SML clip ID")
    result = _request("/api/v1/media/reservations", method="POST", body={
        "project_id": project_id[:100], "scene_id": scene_id[:100],
        "query": query[:200], "media_type": "video",
        "count": 1, "exclude_ids": sorted(exclude_ids or set())[:500],
        "preferred_ids": [preferred_id],
    })
    selected = result.get("items") or []
    if not selected:
        return None
    item = selected[0]
    if not (isinstance(item, dict) and str(item.get("id") or "") == preferred_id
            and RES_ID.fullmatch(str(item.get("reservation_id") or ""))):
        raise SMLUnavailable("SML returned an invalid media reservation")
    return item


def transition_reservation(reservation_id: str, action: str) -> None:
    if not RES_ID.fullmatch(reservation_id) or action not in {"commit", "release"}:
        raise SMLUnavailable("Invalid SML reservation operation")
    _request(f"/api/v1/media/reservations/{reservation_id}/{action}", method="POST")


def download_verified(clip: dict[str, Any], destination: Path) -> Path:
    """Bounded authenticated V2 fetch; SML itself verifies restored Drive clips."""
    if not eligible(clip):
        raise SMLUnavailable("Not an approved V2 SML clip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    digest = hashlib.sha256()
    size = 0
    try:
        request = urllib.request.Request(
            library_url() + str(clip["content_url"]),
            headers={"X-SML-Editor-Token": editor_token()}, method="GET",
        )
        with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as sink:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_CLIP_BYTES:
                    raise SMLUnavailable("SML clip exceeds maximum media transfer size")
                digest.update(chunk)
                sink.write(chunk)
        expected_sha = str(clip.get("sha256") or "").lower()
        if size == 0 or (expected_sha and digest.hexdigest() != expected_sha):
            raise SMLUnavailable("SML media integrity verification failed")
        temporary.replace(destination)
        return destination
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise SMLUnavailable("SML media is temporarily unavailable") from exc
    finally:
        temporary.unlink(missing_ok=True)


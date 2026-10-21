"""Authenticated, read-only SML V2 media fetch for the independent Scene worker.

Never uses the public V1 /media/clips path. SML owns catalog, approval, Drive
restoration, and usage reservations. No tokens/remote errors are logged.
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

CLIP_ID = re.compile(r"CLP-[A-Za-z0-9_-]+\Z")
RES_ID = re.compile(r"RES-[0-9a-f]{32}\Z")
MAX_BYTES = 256 * 1024 * 1024
WORD = re.compile(r"[^\W_]+", re.UNICODE)
STOP = {"this", "that", "with", "from", "into", "the", "and", "for", "clip", "scene", "video"}


class SMLMediaUnavailable(Exception):
    pass


def _terms(text: object) -> set[str]:
    return {x for x in WORD.findall(str(text or "").casefold()) if len(x) >= 3 and x not in STOP}


class SMLWorkerClient:
    def __init__(self, origin: str | None = None, token: str | None = None):
        self.origin = str(origin if origin is not None else os.getenv(
            "SCENE_SML_URL", "http://127.0.0.1:8147")).strip().rstrip("/")
        self.token = str(token if token is not None else os.getenv(
            "SCENE_SML_EDITOR_TOKEN", "")).strip()
        parsed = urllib.parse.urlsplit(self.origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.path not in {"", "/"} or
                parsed.query or parsed.fragment):
            raise SMLMediaUnavailable("Invalid SML internal service origin")

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _json(self, method: str, path: str, *, params: dict | None = None,
              payload: dict | None = None) -> dict[str, Any]:
        if not self.enabled:
            raise SMLMediaUnavailable("SML editor token not configured")
        if params:
            path += "?" + urllib.parse.urlencode(params)
        headers = {"X-SML-Editor-Token": self.token, "Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        try:
            req = urllib.request.Request(self.origin + path, data=data,
                                         headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=12) as response:
                result = json.loads(response.read(3 * 1024 * 1024 + 1))
            if not isinstance(result, dict):
                raise ValueError("Invalid response")
            return result
        except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
            raise SMLMediaUnavailable("SML V2 editor API not reachable") from exc

    def ranked(self, query: str, *, used_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """Only return positively matching approved video candidates, no arbitrary defaults."""
        query = str(query or "").strip()[:200]
        needle = _terms(query)
        if not needle:
            return []
        result = self._json("GET", "/api/v1/media/search",
                            params={"query": query, "media_type": "video", "limit": 100})
        items = result.get("items")
        if not isinstance(items, list):
            raise SMLMediaUnavailable("SML V2 search response invalid")
        ranked = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("id") or "")
            if (not CLIP_ID.fullmatch(ident) or ident in (used_ids or set()) or
                    item.get("type") != "video" or
                    item.get("content_url") != f"/api/v1/media/video/{ident}/content"):
                continue
            keywords = " ".join(str(x.get("keyword", "")) if isinstance(x, dict) else str(x)
                                for x in (item.get("keywords") or []))
            matching = len(needle & _terms(keywords + " " + str(item.get("description") or "") + " " + str(item.get("source_filename") or "")))
            if not matching:
                continue
            try:
                quality = float(item.get("quality") or 0)
            except (ValueError, TypeError):
                quality = 0
            ranked.append((matching * 10 + max(0, min(100, quality)) / 100,
                           ident, item))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        return [x[2] for x in ranked]

    def reserve(self, project_id: str, scene_id: str, clip_id: str,
                used_ids: set[str] | None = None) -> str | None:
        if not CLIP_ID.fullmatch(clip_id):
            raise SMLMediaUnavailable("Invalid SML media ID")
        result = self._json("POST", "/api/v1/media/reservations", payload={
            "project_id": project_id[:100], "scene_id": scene_id[:100],
            "query": "", "media_type": "video", "count": 1,
            "preferred_ids": [clip_id], "exclude_ids": sorted(used_ids or set())[:500],
        })
        entries = result.get("items") or []
        if not entries:
            return None
        row = entries[0]
        if not isinstance(row, dict) or str(row.get("id") or "") != clip_id:
            raise SMLMediaUnavailable("SML reservation selected another media asset")
        reservation = str(row.get("reservation_id") or "")
        if not RES_ID.fullmatch(reservation):
            raise SMLMediaUnavailable("SML reservation ID invalid")
        return reservation

    def transition(self, reservation: str, action: str) -> None:
        if not RES_ID.fullmatch(reservation) or action not in {"commit", "release"}:
            raise SMLMediaUnavailable("Invalid SML reservation action")
        self._json("POST", f"/api/v1/media/reservations/{reservation}/{action}")

    def download(self, clip: dict[str, Any], destination: Path) -> str:
        ident = str(clip.get("id") or "")
        if (not self.enabled or not CLIP_ID.fullmatch(ident) or
                clip.get("content_url") != f"/api/v1/media/video/{ident}/content"):
            raise SMLMediaUnavailable("Unsafe SML media reference")
        expected = str(clip.get("sha256") or "").lower()
        if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise SMLMediaUnavailable("Invalid media checksum")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(destination.name + ".partial")
        size, digest = 0, hashlib.sha256()
        try:
            req = urllib.request.Request(self.origin + str(clip["content_url"]),
                                         headers={"X-SML-Editor-Token": self.token}, method="GET")
            with urllib.request.urlopen(req, timeout=60) as response, tmp.open("wb") as f:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise SMLMediaUnavailable("SML media exceeds transfer limit")
                    digest.update(chunk)
                    f.write(chunk)
            if size == 0 or (expected and digest.hexdigest() != expected):
                raise SMLMediaUnavailable("SML media checksum failed")
            tmp.replace(destination)
            return digest.hexdigest()
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise SMLMediaUnavailable("SML media unavailable for transfer") from exc
        finally:
            tmp.unlink(missing_ok=True)

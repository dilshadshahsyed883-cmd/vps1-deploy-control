"""Canonical public Song Uploader path over its existing isolated LUM song engine.

Only URL translation lives here. No second worker, queue, database or scheduler.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode

CANONICAL = "/song-uploader"
LEGACY = "/local-upload-manager/modes/song"


def upstream_path(public_path: str, raw_query: str = "") -> str:
    """Map the public request to the *existing* authenticated mode gateway."""
    if public_path in {CANONICAL, CANONICAL + "/"}:
        suffix = "/app"
    elif public_path.startswith(CANONICAL + "/"):
        suffix = public_path[len(CANONICAL):]
    else:
        raise ValueError("Not a Song Uploader route")
    if ("\\x00" in suffix or "\\" in suffix
            or any(part in {".", ".."} for part in suffix.split("/"))):
        raise ValueError("Invalid Song Uploader path")
    if suffix == "/app":
        pairs = [(k, v) for k, v in parse_qsl(raw_query, keep_blank_values=True)
                 if k not in {"embedded"}]
        if not any(k == "tab" for k, _ in pairs):
            pairs.append(("tab", "auto_engine"))
        pairs.append(("embedded", "1"))
        raw_query = urlencode(pairs)
    return LEGACY + suffix + (("?" + raw_query) if raw_query else "")


def public_location(value: str) -> str:
    """Keep redirects in the new URL namespace; never expose loopback."""
    for prefix in ("http://127.0.0.1:8015", "http://127.0.0.1:8018"):
        if value.startswith(prefix + LEGACY):
            value = value[len(prefix):]
            break
    if value.startswith(LEGACY):
        return CANONICAL + value[len(LEGACY):]
    return value


def public_cookie(value: str) -> str:
    """Session cookie must be sent on public Song paths, not only legacy ones."""
    return re.sub(r"(?i)(;\s*path=)" + re.escape(LEGACY) + r"(?=;|$)",
                  lambda match: match.group(1) + CANONICAL, value)


def public_html(payload: bytes) -> bytes:
    """Rewrite template-generated links/forms/assets while preserving app logic."""
    return payload.replace(LEGACY.encode("ascii"), CANONICAL.encode("ascii"))

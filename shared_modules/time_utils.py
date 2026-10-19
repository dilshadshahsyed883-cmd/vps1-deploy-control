"""Timezone-safe scheduling primitives extracted from Auto Stream V1.

Source: apps/short-stream/part01/auto_stream_engine.py
No engine, credential, database or network dependencies.
"""
from __future__ import annotations
from datetime import datetime, timezone

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def scheduled_utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scheduled_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("scheduled_at must include a timezone offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")

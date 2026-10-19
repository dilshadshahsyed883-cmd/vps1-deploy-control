"""Syntactic YouTube channel-ID check extracted from Auto Stream V1/V2.

An ID matching this expression does not establish channel ownership,
OAuth consent, real channel existence or livestream permissions.
"""
from __future__ import annotations
import re

CHANNEL_ID_PATTERN = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

def is_youtube_channel_id(value: str) -> bool:
    return isinstance(value, str) and CHANNEL_ID_PATTERN.fullmatch(value) is not None

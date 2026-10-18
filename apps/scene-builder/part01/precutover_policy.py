"""Strict, opt-in login/preset editing for Scene Builder pre-cutover.

No queue submissions, media processing, system settings, cleanup, external worker
start or service activation. This policy only narrows candidate-mode write denial;
normal session authentication is still enforced by app middleware.
"""
from __future__ import annotations
import re

_ALLOWED_EXACT = frozenset((
    ("POST", "/login"),
    ("POST", "/logout"),
    ("POST", "/api/presets"),
))
_PRESET_MUTATION = re.compile(r"\A/api/presets/[^/]+/(?:duplicate|rename)\Z")


def mutation_allowed(method: str, path: str, editing_enabled: bool) -> bool:
    if not editing_enabled:
        return False
    if (method, path) in _ALLOWED_EXACT:
        return True
    return method == "POST" and _PRESET_MUTATION.fullmatch(path) is not None

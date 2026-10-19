"""Strict candidate-write policy for Scene Builder.

Only the user-management POST endpoint is additionally permitted in candidate
mode, independent of the broader precutover editing flag. Its real route
requires a logged-in active administrator and a valid per-session CSRF token.
Do not admit queue submissions, media, settings, storage cleanup or workers.
"""
from __future__ import annotations
import re

_ALLOWED_EXACT = frozenset((
    ("POST", "/login"),
    ("POST", "/logout"),
    ("POST", "/api/presets"),
))
_PRESET_MUTATION = re.compile(r"\A/api/presets/[^/]+/(?:duplicate|rename)\Z")
_ADMIN_USER_MANAGEMENT = ("POST", "/user-management")


def mutation_allowed(method: str, path: str, editing_enabled: bool) -> bool:
    # The owner authorized administrator account management on the live shell.
    # This is an exact route match, never a blanket candidate-write bypass.
    if (method, path) == _ADMIN_USER_MANAGEMENT:
        return True
    if not editing_enabled:
        return False
    if (method, path) in _ALLOWED_EXACT:
        return True
    return method == "POST" and _PRESET_MUTATION.fullmatch(path) is not None

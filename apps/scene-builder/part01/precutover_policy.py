"""Strict candidate-write policy for Scene Builder.

Only exact owner-authorized user-management and Experiment Studio browser
mutations work while general precutover editing is disabled. Each route still
requires an active web session; browser mutations additionally enforce a
session-bound CSRF header. Queue, media, settings and workers remain denied.
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
    if method == "POST" and path == "/api/experiment-browser/profiles":
        return True
    # Limit the candidate exception to one profile ID and its action verbs.
    match = re.fullmatch(r"/api/experiment-browser/profiles/[0-9a-f]{24}(?:/(start|stop|navigate|action))?", path)
    if match and ((method == "DELETE" and match.group(1) is None)
                  or (method == "POST" and match.group(1) is not None)):
        return True
    if not editing_enabled:
        return False
    if (method, path) in _ALLOWED_EXACT:
        return True
    return method == "POST" and _PRESET_MUTATION.fullmatch(path) is not None

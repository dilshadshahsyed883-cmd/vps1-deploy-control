"""Strict candidate-write policy for Scene Builder.

Only exact owner-authorized user-management and Experiment Studio browser
mutations work while general precutover editing is disabled. Each route still
requires an active web session; browser mutations additionally enforce a
session-bound CSRF header. With the existing editing gate on, Scene submissions,
owned-job cancellation and recovery (including Resume) reach their authenticated handlers. Other queue, media, storage
and worker operations remain denied.
"""
from __future__ import annotations
import re

_ALLOWED_EXACT = frozenset((
    ("POST", "/login"),
    ("POST", "/logout"),
    ("POST", "/settings"),
    ("POST", "/api/internal-audio/settings"),
    ("POST", "/api/internal-audio/studio/process"),
    ("POST", "/api/presets"),
    ("POST", "/api/settings/pexels-keys"),
    ("POST", "/queue/submit"),
))
_PRESET_MUTATION = re.compile(r"\A/api/presets/[^/]+/(?:duplicate|rename)\Z")
_PROJECT_RECOVERY = re.compile(r"\A/queue/[0-9a-f]{32}/(?:cancel|delete|start|resume|restart-stage|restart|upload-drive)\Z")
_ADMIN_USER_MANAGEMENT = ("POST", "/user-management")


def mutation_allowed(method: str, path: str, editing_enabled: bool, auto_script_enabled: bool = False) -> bool:
    # Owner-approved Auto Script writes are scoped to its own APIs only.
    if auto_script_enabled and method == "POST":
        if path in ("/api/auto-script/channels", "/api/auto-script/tick"):
            return True
        if re.fullmatch(r"/api/auto-script/channels/[0-9]+/(?:toggle|run)", path):
            return True
        if re.fullmatch(r"/api/auto-script/items/[0-9]+/retry", path):
            return True
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
    # Existing Media Audit handlers still enforce project ownership and revisions.
    if method == "POST" and re.fullmatch(
            r"/api/audit-media/[0-9a-f]{32}/(?:sml-auto-match|approve-all|continue|"
            r"[A-Za-z0-9_-]+/(?:sml-select|replace|keyword|upload))", path):
        return True
    if (method, path) in _ALLOWED_EXACT:
        return True
    return method == "POST" and (_PRESET_MUTATION.fullmatch(path) is not None
                                 or _PROJECT_RECOVERY.fullmatch(path) is not None)

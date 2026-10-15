"""Synthetic release transaction: source flip, live-style verification and rollback.

PRODUCTION NOT AUTHORIZED. This module intentionally inherits fresh_vps.root_dir
so every write remains inside an explicit /tmp rehearsal root. It does not SSH,
restart services or modify Nginx. Injected callbacks model those effects in CI.
"""
from pathlib import Path
import re

from rebuild import fresh_vps

FULL_SHA = re.compile(r"[a-f0-9]{40}\Z")


class PromotionFailed(RuntimeError):
    """A candidate failed; the message reports whether rollback succeeded."""


def _managed_pointer(root, app):
    base = root / "srv/fresh-website"
    releases = base / "releases" / app
    pointer = base / "current" / app
    if not pointer.is_symlink():
        raise ValueError("initial installation requires separate approved bootstrap")
    if releases.is_symlink() or (base / "releases").is_symlink():
        raise ValueError("unsafe app release path")
    active = pointer.resolve(strict=True)
    if active.parent != releases.resolve() or active.is_symlink() or not active.is_dir():
        raise ValueError("current release pointer escaped managed application")
    if not FULL_SHA.fullmatch(active.name):
        raise ValueError("non-GitHub commit identifier in active release")
    if not (active / "_fresh_manifest.json").is_file():
        raise ValueError("active release manifest absent")
    return pointer, active


def transact(root, app, revision, restart, healthy, *, snapshot_confirmed=False):
    """Simulate per-app deployment and automatic rollback after health failure.

    restart(app) models service restart; healthy(app, sha) returns True/False.
    Caller supplies callbacks and only source-controlled, simulated /tmp roots.
    Data/queues/secrets and other app pointers are outside the write scope.
    """
    root = fresh_vps.root_dir(root)
    if not snapshot_confirmed:
        raise ValueError("verified snapshot required before cutover rehearsal")
    if app not in fresh_vps.managed_groups() or not FULL_SHA.fullmatch(revision):
        raise ValueError("offline or invalid app/revision")
    if not callable(restart) or not callable(healthy):
        raise ValueError("explicit restart and health callbacks required")
    pointer, old = _managed_pointer(root, app)
    old_sha = old.name
    if old_sha == revision:
        if healthy(app, revision) is not True:
            raise PromotionFailed("unchanged active source is unhealthy")
        return {"status": "NO_CHANGE_HEALTHY", "app": app, "revision": revision,
                "production_modified": False}
    # Require the old known-good health status before making a change.
    if healthy(app, old_sha) is not True:
        raise ValueError("existing release health failed: deployment blocked")

    try:
        result = fresh_vps.deploy_app(root, app, revision)
        if result["release"] != revision or pointer.resolve(strict=True).name != revision:
            raise RuntimeError("candidate release SHA readback mismatch")
        restart(app)
        if healthy(app, revision) is not True:
            raise RuntimeError("candidate did not pass application health")
        return {"status": "CANDIDATE_HEALTHY", "app": app,
                "revision": revision, "previous": old_sha,
                "verification": "synthetic_callbacks_only",
                "production_modified": False}
    except Exception as cause:
        # No generic filesystem cleanup, database rollback or Nginx reload.
        # Restore exact previous immutable release pointer and service.
        try:
            if pointer.is_symlink() and pointer.resolve(strict=True) != old:
                rolled = fresh_vps.rollback(root, app)
                if rolled["restored_revision"] != old_sha:
                    raise RuntimeError("rolled back to unexpected revision")
                restart(app)
            if pointer.resolve(strict=True) != old or healthy(app, old_sha) is not True:
                raise RuntimeError("original release was not restored healthy")
        except Exception as rollback_issue:
            raise PromotionFailed("CRITICAL: candidate failed and rollback did not verify") from rollback_issue
        raise PromotionFailed("candidate failed; prior healthy revision verified restored") from cause

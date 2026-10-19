#!/usr/bin/env python3
"""VPS_1 deployment-artifact garbage collection (never deletes user media).

Read-only by default. The guarded --apply entry point is intended for a
root-owned systemd timer on the approved host after an initial dry run.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

BASE = Path("/srv/fresh-website")
STATE = Path("/var/lib/vps-storage-manager")
DEPLOY_LOCK = Path("/run/lock/vps1-production.lock")
MIN_AGE_HOURS = 12
MIN_FREE_BYTES = 5_000_000_000
MAX_HISTORY = 120
APP_NAMES = frozenset({
    "scene-builder", "short-maker", "short-stream", "self-media-library",
    "video-merger", "voice-studio", "auto-caption-studio", "local-upload-manager",
})
REVISION = re.compile(r"^[a-f0-9]{40}(?:-[a-z0-9-]+)?$")
SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


def _within(item: Path, parent: Path) -> bool:
    try:
        return item.resolve(strict=True).is_relative_to(parent.resolve(strict=True))
    except (OSError, RuntimeError):
        return False


def _safe_directory(item: Path, parent: Path) -> bool:
    """Reject top-level symlinks and mounts; rmtree itself never follows links."""
    if item.is_symlink() or not item.is_dir() or not _within(item, parent):
        return False
    try:
        base_dev = parent.stat().st_dev
        if item.stat().st_dev != base_dev:
            return False
        for walk_root, dirs, files in os.walk(item, followlinks=False):
            for name in dirs + files:
                path = Path(walk_root) / name
                if path.is_symlink():
                    # Python virtualenvs have normal interpreter symlinks; never follow them.
                    continue
                if path.stat().st_dev != base_dev:
                    return False
        return True
    except (OSError, RuntimeError):
        return False


def _allocated_bytes(directory: Path) -> int:
    total = 0
    for walk_root, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(walk_root) / name).is_symlink()]
        for name in files:
            try:
                total += (Path(walk_root) / name).lstat().st_blocks * 512
            except OSError:
                pass
    return total


def _json_file(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def _unit_references(app: str, root: Path, unit_dir: Path) -> tuple[set[str], bool]:
    """Keep every revision referenced by a service; unreadable units fail closed."""
    references: set[str] = set()
    try:
        files = sorted(unit_dir.glob("vps1-*.service"))
        for file in files:
            if file.is_symlink() or not file.is_file():
                return set(), False
            raw = file.read_text(encoding="utf-8")
            for category in ("releases", "venvs"):
                prefix = str(root / category / app) + "/"
                for hit in re.findall(re.escape(prefix) + r"([a-f0-9-]{40,100})", raw):
                    references.add(hit.rstrip("/"))
        return references, True
    except (OSError, UnicodeError):
        return set(), False


def inventory(root: Path = BASE, now: float | None = None, unit_dir: Path | None = None) -> dict[str, Any]:
    """Conservative read-only inventory; unknown ownership is an exclusion."""
    now = time.time() if now is None else now
    unit_dir = Path("/etc/systemd/system") if unit_dir is None else unit_dir
    results: dict[str, Any] = {
        "schema": 1, "policy": "vps1-deployment-12h-v1",
        "retention_hours": MIN_AGE_HOURS, "timestamp": int(now),
        "candidates": [], "excluded": [], "protected": [], "estimated_bytes": 0,
    }
    releases_root = root / "releases"
    current_root = root / "current"
    state_root = root / "control" / "state"
    if any(not dir.is_dir() or dir.is_symlink() for dir in (root, releases_root, current_root, state_root)):
        results["blocked"] = "missing or unsafe deployment roots"
        return results
    deadline = now - MIN_AGE_HOURS * 3600
    for app in sorted(APP_NAMES):
        releases = releases_root / app
        venvs = root / "venvs" / app
        link = current_root / app
        record = _json_file(state_root / (app + ".json"))
        if not releases.is_dir():
            continue
        if releases.is_symlink() or (venvs.exists() and (venvs.is_symlink() or not venvs.is_dir())):
            results["excluded"].append({"app": app, "reason": "unsafe release or venv root"})
            continue
        if not link.is_symlink() or not record or record.get("status") != "ACTIVE":
            results["excluded"].append({"app": app, "reason": "missing current pointer or active manifest"})
            continue
        try:
            active = link.resolve(strict=True)
            active_rev = active.name
            if active.parent != releases.resolve(strict=True) or record.get("revision") != active_rev:
                raise ValueError("pointer/manifest mismatch")
            prev_value = (record.get("previous") or {}).get("current")
            previous = Path(prev_value).resolve(strict=True) if isinstance(prev_value, str) and prev_value else None
            if previous and previous.parent != releases.resolve(strict=True):
                raise ValueError("rollback pointer outside app")
            ref_names, units_ok = _unit_references(app, root, unit_dir)
            if not units_ok:
                raise ValueError("systemd units unavailable")
            if link.lstat().st_mtime > deadline:
                raise ValueError("current promotion still inside 12h grace")
        except (OSError, RuntimeError, ValueError) as exc:
            results["excluded"].append({"app": app, "reason": str(exc)})
            continue
        entries = sorted(
            (path for path in releases.iterdir() if path.is_dir() and not path.is_symlink()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        # Keep active, recorded prior release, and one additional recent fallback.
        protect = {active_rev} | ({previous.name} if previous else set()) | ref_names
        fallback = next((p.name for p in entries if p.name not in protect), None)
        if fallback:
            protect.add(fallback)
        results["protected"].append({"app": app, "revisions": sorted(protect)})
        for item in entries:
            name = item.name
            if name in protect:
                continue
            if not REVISION.fullmatch(name) or item.stat().st_mtime > deadline:
                results["excluded"].append({"app": app, "revision": name, "reason": "invalid revision or younger than 12h"})
                continue
            venv = venvs / name
            if not _safe_directory(item, releases):
                results["excluded"].append({"app": app, "revision": name, "reason": "unsafe release tree"})
                continue
            paths = [item]
            if venv.exists():
                if not _safe_directory(venv, venvs) or venv.stat().st_mtime > deadline:
                    results["excluded"].append({"app": app, "revision": name, "reason": "unsafe or recently used venv"})
                    continue
                paths.append(venv)
            count = sum(_allocated_bytes(path) for path in paths)
            results["candidates"].append({"app": app, "revision": name,
                                          "paths": [str(p) for p in paths], "bytes": count})
            results["estimated_bytes"] += count
    return results


def apply_plan(root: Path, plan: dict[str, Any], *, now: float | None = None,
               unit_dir: Path | None = None) -> dict[str, Any]:
    """Under deployment lock, recalculate eligibility before each deletion."""
    if plan.get("blocked"):
        raise RuntimeError("dry-run inventory blocked")
    freed = 0
    deleted: list[str] = []
    skipped: list[str] = []
    for candidate in plan["candidates"]:
        current_plan = inventory(root, now=now, unit_dir=unit_dir)
        eligible = {
            (c["app"], c["revision"]): c for c in current_plan["candidates"]
        }
        key = (candidate["app"], candidate["revision"])
        if key not in eligible or eligible[key]["paths"] != candidate["paths"]:
            skipped.append(candidate["app"] + "/" + candidate["revision"])
            continue
        for raw in eligible[key]["paths"]:
            path = Path(raw)
            parent = path.parent
            if not _safe_directory(path, parent):
                raise RuntimeError("last-minute deletion safety check failed")
            freed += _allocated_bytes(path)
            shutil.rmtree(path)
        deleted.append(candidate["app"] + "/" + candidate["revision"])
    return {"deleted": deleted, "skipped": skipped, "estimated_removed_bytes": freed}


def _record_audit(state: Path, payload: dict[str, Any]) -> None:
    state.mkdir(parents=True, exist_ok=True, mode=0o750)
    target = state / "deployment-gc-history.jsonl"
    if target.is_symlink():
        raise RuntimeError("unsafe audit log")
    previous = target.read_text(encoding="utf-8").splitlines()[-(MAX_HISTORY - 1):] if target.exists() else []
    new_text = "\n".join(previous + [json.dumps(payload, sort_keys=True)]) + "\n"
    temp = state / ("." + target.name + ".tmp")
    temp.write_text(new_text, encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, target)


@contextlib.contextmanager
def host_lock(path: Path = DEPLOY_LOCK):
    """Same flock as production.runtime; no concurrent deployment or rollback."""
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def execute(root: Path = BASE, *, apply: bool = False, state: Path = STATE,
            now: float | None = None, unit_dir: Path | None = None,
            test_mode: bool = False) -> dict[str, Any]:
    if apply and not test_mode and (root != BASE or os.geteuid() != 0):
        raise RuntimeError("apply requires approved production root and root identity")
    # Lock even dry-run in production so reports reflect a consistent pointer state.
    lock = root / ".storage-gc-test.lock" if test_mode else DEPLOY_LOCK
    with host_lock(lock):
        before = shutil.disk_usage(root).free
        plan = inventory(root, now=now, unit_dir=unit_dir)
        result: dict[str, Any] = {
            "mode": "apply" if apply else "dry-run", "inventory": plan,
            "free_before_bytes": before, "free_after_bytes": before,
        }
        if apply:
            if plan.get("blocked"):
                raise RuntimeError(plan["blocked"])
            if not test_mode and before < MIN_FREE_BYTES:
                raise RuntimeError("low-space incident: manual recovery required")
            result["outcome"] = apply_plan(root, plan, now=now, unit_dir=unit_dir)
            result["free_after_bytes"] = shutil.disk_usage(root).free
        if apply and not test_mode:
            _record_audit(state, {
                "timestamp": int(time.time()), "mode": "apply", "policy": plan["policy"],
                "deleted": result["outcome"]["deleted"], "skipped": result["outcome"]["skipped"],
                "free_before_bytes": before, "free_after_bytes": result["free_after_bytes"],
                "estimated_removed_bytes": result["outcome"]["estimated_removed_bytes"],
                "excluded": len(plan["excluded"]),
            })
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="guarded removal; default is dry-run")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = execute(apply=args.apply)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

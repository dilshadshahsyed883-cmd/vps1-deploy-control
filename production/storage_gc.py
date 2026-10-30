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
PUBLIC_STATUS = Path("/var/lib/vps1-storage-gc-public/status.json")
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
            if active.parent != releases.resolve(strict=True) or not REVISION.fullmatch(active_rev):
                raise ValueError("current pointer outside known release")
            recorded_rev = record.get("revision")
            if not isinstance(recorded_rev, str) or not REVISION.fullmatch(recorded_rev):
                raise ValueError("invalid recorded revision")
            mismatch = recorded_rev != active_rev
            if mismatch:
                # Separate release lanes can promote a web-only pointer while
                # the running service still uses the recorded Python revision.
                # Neither source of truth is rewritten by storage cleanup.
                recorded_release = releases / recorded_rev
                if recorded_release.is_symlink() or not recorded_release.is_dir():
                    raise ValueError("unverifiable recorded release")
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
            # Newest mtime first; deterministic name order breaks equal-mtime ties.
            key=lambda p: (-p.stat().st_mtime, p.name),
        )
        # Keep active, recorded prior release, and one additional recent fallback.
        protect = {active_rev, recorded_rev} | ({previous.name} if previous else set()) | ref_names
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
            if mismatch:
                # A pointer/manifest disagreement is not permission to delete
                # source releases. Reclaim only unused *paired* virtualenvs
                # after protecting both known revisions, rollback and units.
                if not venv.is_dir() or not _safe_directory(venv, venvs) or venv.stat().st_mtime > deadline:
                    results["excluded"].append({"app": app, "revision": name,
                                                 "reason": "unpaired, unsafe or recent venv"})
                    continue
                paths = [venv]
                scope = "venv-only-pointer-mismatch"
            else:
                paths = [item]
                if venv.exists():
                    if not _safe_directory(venv, venvs) or venv.stat().st_mtime > deadline:
                        results["excluded"].append({"app": app, "revision": name, "reason": "unsafe or recently used venv"})
                        continue
                    paths.append(venv)
                scope = "paired-release"
            count = sum(_allocated_bytes(path) for path in paths)
            results["candidates"].append({"app": app, "revision": name,
                                          "scope": scope, "paths": [str(p) for p in paths],
                                          "bytes": count})
            results["estimated_bytes"] += count
    return results


def _runtime_in_use(paths: list[Path], proc_root: Path = Path("/proc")) -> bool:
    """Block cleanup if any live process names, opens, or works inside a target."""
    targets = [str(path.resolve(strict=True)) for path in paths]
    try:
        for pid in proc_root.iterdir():
            if not pid.name.isdigit():
                continue
            try:
                # Check argv, environment, loaded libraries and file handles.
                # If any process introspection fails, do not delete.
                for name in ("cmdline", "environ", "maps"):
                    content = (pid / name).read_bytes()
                    if any(os.fsencode(target) in content for target in targets):
                        return True
                for name in ("cwd", "exe"):
                    with contextlib.suppress(FileNotFoundError):
                        linked = (pid / name).resolve(strict=True)
                        if any(linked == Path(target) or linked.is_relative_to(Path(target))
                               for target in targets):
                            return True
                for fd in (pid / "fd").iterdir():
                    with contextlib.suppress(FileNotFoundError):
                        linked = fd.resolve(strict=True)
                        if any(linked == Path(target) or linked.is_relative_to(Path(target))
                               for target in targets):
                            return True
            except FileNotFoundError:
                continue  # Process exited while /proc was traversed.
            except OSError:
                return True  # Unreadable process state: protect the candidate.
        return False
    except OSError:
        return True


def apply_plan(root: Path, plan: dict[str, Any], *, now: float | None = None,
               unit_dir: Path | None = None, test_mode: bool = False) -> dict[str, Any]:
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
        if not test_mode:
            service = "vps1-production-" + candidate["app"] + ".service"
            if subprocess.run(["systemctl", "is-active", "--quiet", service],
                              check=False, timeout=10).returncode != 0:
                skipped.append(candidate["app"] + "/" + candidate["revision"] + ":service-inactive")
                continue
            if _runtime_in_use([Path(p) for p in candidate["paths"]]):
                skipped.append(candidate["app"] + "/" + candidate["revision"] + ":process-in-use")
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


def prune_disposable_caches(now: float | None = None) -> dict[str, Any]:
    """Only unused aged Docker build cache and age-verified pip downloads."""
    now = time.time() if now is None else now
    results: dict[str, Any] = {"pip_removed_bytes": 0, "docker": "not-installed", "warnings": []}
    pip = Path("/home/ubuntu/.cache/pip")
    deadline = now - MIN_AGE_HOURS * 3600
    if pip.is_dir() and not pip.is_symlink():
        try:
            dev = pip.stat().st_dev
            for directory, dirs, files in os.walk(pip, followlinks=False):
                dirs[:] = [x for x in dirs if not (Path(directory) / x).is_symlink()]
                for name in files:
                    item = Path(directory) / name
                    try:
                        stat = item.lstat()
                        if (item.is_symlink() or not item.is_file() or
                                stat.st_dev != dev or stat.st_nlink != 1 or
                                stat.st_mtime > deadline or name.endswith(".lock")):
                            continue
                        results["pip_removed_bytes"] += stat.st_blocks * 512
                        item.unlink()
                    except OSError as exc:
                        results["warnings"].append("pip: " + type(exc).__name__)
        except OSError as exc:
            results["warnings"].append("pip inventory: " + type(exc).__name__)
    if shutil.which("docker"):
        try:
            done = subprocess.run(
                ["docker", "builder", "prune", "--all", "--force",
                 "--filter", "until=12h"], text=True, capture_output=True,
                check=False, timeout=600,
            )
            results["docker"] = "done" if done.returncode == 0 else "failed"
            if done.returncode:
                results["warnings"].append("docker cache cleanup failed")
        except (OSError, subprocess.TimeoutExpired):
            results["docker"] = "failed"
            results["warnings"].append("docker cache cleanup timeout/unavailable")
    return results


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
            result["outcome"] = apply_plan(root, plan, now=now, unit_dir=unit_dir, test_mode=test_mode)
            if not test_mode:
                result["cache"] = prune_disposable_caches(now=now)
            result["free_after_bytes"] = shutil.disk_usage(root).free
        if apply and not test_mode:
            _record_audit(state, {
                "timestamp": int(time.time()), "mode": "apply", "policy": plan["policy"],
                "deleted": result["outcome"]["deleted"], "skipped": result["outcome"]["skipped"],
                "free_before_bytes": before, "free_after_bytes": result["free_after_bytes"],
                "estimated_removed_bytes": result["outcome"]["estimated_removed_bytes"],
                "excluded": len(plan["excluded"]), "cache": result.get("cache", {}),
            })
        return result



def publish_readonly_status(*, state: Path = STATE, destination: Path = PUBLIC_STATUS,
                            root: Path = BASE, unit_dir: Path | None = None,
                            test_mode: bool = False) -> dict[str, Any]:
    """Export only redacted GC metrics: never expose audit paths, release IDs or user data.

    The installed root-only GC owns this producer. Its publicly readable output is
    a bounded metadata document, not a permission change to the private audit.
    Inventory is acquired through the existing deployment flock; if that lock or
    ownership check fails we publish no reclaimable-byte claim.
    """
    if not test_mode and os.geteuid() != 0:
        raise PermissionError("root-only verified GC snapshot exporter")
    last = None
    audit = state / "deployment-gc-history.jsonl"
    try:
        if audit.is_file() and not audit.is_symlink() and audit.stat().st_size <= 2_000_000:
            for line in reversed(audit.read_text(encoding="utf-8").splitlines()[-MAX_HISTORY:]):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("mode") != "apply":
                    continue
                when = record.get("timestamp")
                before, after = record.get("free_before_bytes"), record.get("free_after_bytes")
                if type(when) is not int or not 0 < when < 4_102_444_800:
                    continue
                deleted, skipped = record.get("deleted"), record.get("skipped")
                last = {
                    "timestamp": when,
                    "deleted_count": len(deleted) if isinstance(deleted, list) else None,
                    "skipped_count": len(skipped) if isinstance(skipped, list) else None,
                    "measured_freed_bytes": max(0, after - before)
                    if type(before) is int and type(after) is int and before >= 0 and after >= 0
                    else None,
                }
                break
    except (OSError, UnicodeError, ValueError, OverflowError, TypeError):
        pass

    eligible = None
    measured_at = None
    try:
        result = execute(root=root, apply=False, state=state, unit_dir=unit_dir,
                         test_mode=test_mode)
        plan = result.get("inventory", {})
        estimated = plan.get("estimated_bytes")
        if not plan.get("blocked") and type(estimated) is int and estimated >= 0:
            eligible = estimated
            measured_at = int(time.time())
    except (OSError, ValueError, RuntimeError, PermissionError, BlockingIOError):
        pass

    payload = {
        "schema": 1, "generated_at": int(time.time()),
        "last_cleanup": last, "eligible_bytes": eligible,
        "eligible_measured_at": measured_at,
    }
    parent = destination.parent
    if parent.is_symlink():
        raise RuntimeError("unsafe public GC status directory")
    if not test_mode:
        if not parent.is_dir():
            raise RuntimeError("public GC status directory not installed")
        info = parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("public GC status directory is not root-controlled")
    else:
        parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise RuntimeError("unsafe public GC status symlink")
    temp = parent / (".gc-status-" + str(os.getpid()) + ".tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, destination)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="guarded removal; default is dry-run")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--publish-status", action="store_true", help="root-only sanitized read-only telemetry")
    args = parser.parse_args()
    if args.publish_status:
        if args.apply:
            parser.error("--publish-status cannot delete or apply")
        print(json.dumps(publish_readonly_status(), sort_keys=True))
        return
    result = execute(apply=args.apply)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

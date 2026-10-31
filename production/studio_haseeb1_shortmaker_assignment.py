"""Guarded first-owner assignment of legacy Short Maker job/folder metadata.

Only six terminal, unassigned jobs and three legacy folders from the audited
inventory may be claimed. Never modify, move, or remove videos and uploads.
"""
from __future__ import annotations
import json
import os
import re
import stat
from pathlib import Path

from production.studio_haseeb1_assignment import TARGET, check_scene_account, require

TERMINAL = frozenset(("completed", "failed", "cancelled", "canceled"))
EXPECTED_JOBS = 6
EXPECTED_FOLDERS = 3


def read_original(path: Path):
    if path.is_symlink():
        require(False, "SHORTMAKER_OWNER_SYMLINK")
    if not path.exists():
        return None
    require(path.is_file(), "SHORTMAKER_OWNER_FILE_INVALID")
    return path.read_bytes()


def collect(root: Path):
    jobs_root, folder_root = root / "jobs", root / "vps_short_folders"
    require(jobs_root.is_dir() and not jobs_root.is_symlink(),
            "SHORTMAKER_JOBS_ROOT_UNTRUSTED")
    require(folder_root.is_dir() and not folder_root.is_symlink(),
            "SHORTMAKER_FOLDERS_ROOT_UNTRUSTED")
    updates = []
    jobs = folders = 0
    for entry in sorted(jobs_root.iterdir()):
        require(not entry.is_symlink(), "SHORTMAKER_JOB_SYMLINK")
        if entry.is_file():
            # Legacy staging locks are not jobs.
            continue
        require(entry.is_dir(), "SHORTMAKER_JOB_SPECIAL")
        state = entry / "state.json"
        original = read_original(state)
        if original is None:
            continue
        raw = json.loads(original)
        require(isinstance(raw, dict), "SHORTMAKER_JOB_JSON_INVALID")
        owner = str(raw.get("owner") or "").strip().casefold()
        require(owner in ("", TARGET), "SHORTMAKER_FOREIGN_JOB_OWNER")
        status = str(raw.get("status") or "").strip().casefold()
        require(status in TERMINAL, "SHORTMAKER_NONTERMINAL_JOB")
        jobs += 1
        if owner != TARGET:
            raw["owner"] = TARGET
            replacement = (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode()
            updates.append((state, original, replacement))
    for entry in sorted(folder_root.iterdir()):
        require(not entry.is_symlink(), "SHORTMAKER_FOLDER_SYMLINK")
        if entry.is_file():
            continue
        require(entry.is_dir(), "SHORTMAKER_FOLDER_SPECIAL")
        folders += 1
        marker = entry / ".studio_owner"
        original = read_original(marker)
        owner = original.decode("utf-8").strip().casefold() if original is not None else ""
        require(owner in ("", TARGET), "SHORTMAKER_FOREIGN_FOLDER_OWNER")
        if owner != TARGET:
            updates.append((marker, original, TARGET.encode("utf-8")))
    require(jobs == EXPECTED_JOBS, "SHORTMAKER_JOB_COUNT_CHANGED")
    require(folders == EXPECTED_FOLDERS, "SHORTMAKER_FOLDER_COUNT_CHANGED")
    return jobs, folders, updates


def safe_replace(path: Path, before: bytes | None, after: bytes | None):
    """Compare original bytes and preserve service-readable ownership/mode."""
    require(read_original(path) == before, "SHORTMAKER_OWNER_CONCURRENT_CHANGE")
    if after is None:
        # Only remove a migration-created marker on guarded compensation.
        require(before is not None, "SHORTMAKER_DELETE_WITHOUT_SOURCE")
        path.unlink()
        return
    parent = path.parent
    st = path.stat() if path.exists() else parent.stat()
    mode = stat.S_IMODE(st.st_mode) if path.exists() else 0o640
    temp = path.with_name("." + path.name + ".haseeb1-claim-tmp")
    require(not temp.exists() and not temp.is_symlink(), "SHORTMAKER_TEMP_EXISTS")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temp, st.st_uid, st.st_gid)
        os.chmod(temp, mode)
        require(read_original(path) == before, "SHORTMAKER_OWNER_CONCURRENT_CHANGE")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def install_shortmaker_claim(revision: str):
    import shutil
    from production.runtime import BASE, DATA, atomic_json
    require(re.fullmatch(r"[a-f0-9]{40}", revision) is not None,
            "INVALID_REVISION")
    require(shutil.disk_usage("/").free >= 5_000_000_000,
            "INSUFFICIENT_DISK_RESERVE")
    check_scene_account(DATA / "scene-builder/web/web.sqlite3")
    root = DATA / "short-maker"
    jobs, folders, updates = collect(root)
    journal = BASE / "transactions/haseeb1-shortmaker-firstclaim" / revision
    require(not journal.exists(), "TRANSACTION_ALREADY_EXISTS")
    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal / "intent.json", {
        "state": "PREFLIGHT", "target": TARGET, "jobs": jobs,
        "folders": folders, "changed_files": len(updates),
        "auto_script": "OFFLINE", "services": "UNCHANGED"})
    # Root-only original bytes, not media or input/output folders.
    for i, (_path, original, replacement) in enumerate(updates):
        if original is not None:
            backup = journal / "backups" / (str(i) + ".orig")
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with backup.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            backup.chmod(0o600)
    changed = []
    try:
        for path, before, after in updates:
            safe_replace(path, before, after)
            changed.append((path, before, after))
        after_jobs, after_folders, unresolved = collect(root)
        require(after_jobs == jobs and after_folders == folders and not unresolved,
                "SHORTMAKER_POST_OWNER_VERIFICATION_FAILED")
        atomic_json(journal / "receipt.json", {
            "state": "SHORTMAKER_ASSIGNED", "owner": TARGET,
            "jobs": jobs, "folders": folders,
            "services": "UNCHANGED", "media": "UNCHANGED",
            "auto_script": "OFFLINE"})
        print(json.dumps({
            "STUDIO_SHORTMAKER_ASSIGNMENT": "PASS", "owner": TARGET,
            "jobs": jobs, "folders": folders, "services": "UNCHANGED",
            "media": "UNCHANGED", "auto_script": "OFFLINE"}, sort_keys=True), flush=True)
    except BaseException:
        # Never overwrite an intervening application change during compensation.
        for path, before, after in reversed(changed):
            safe_replace(path, after, before)
        atomic_json(journal / "receipt.json", {
            "state": "COMPENSATED", "auto_script": "OFFLINE"})
        raise

"""Transactional passive Short Studio unit configuration (NO restarts).

Prepare four existing services to share an explicitly declared folder root,
while keeping cooperative locks and all destructive/scheduled features OFF.
A later, separately guarded activation must establish a durable admission
barrier, owner-wide idle, running-process mount identity and rollback proof.
No user data, media, queue, OAuth, uploads or original Drive files are touched.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path

from production import runtime
from production.shortstudio_cooperation_readonly import (
    SERVICES, inspect, inspect_live_processes, inspect_active_drain,
)

ROOT = runtime.DATA / "short-maker" / "vps_short_folders"
MAKER_DATA = runtime.DATA / "short-maker"
UPLOADER = runtime.DATA / "local-upload-manager" / "lum-shortstudio"
APP_DB = UPLOADER / "core/data/uploader.db"
ENGINE_DB = UPLOADER / "auto_engine_v6/auto_engine_v6.sqlite3"
MAKER_ENV = runtime.CONFIG / "production-short-maker.env"
UPLOADER_ENV = runtime.CONFIG / "production-local-upload-manager.env"
UNITS = Path("/etc/systemd/system")
JOURNALS = runtime.BASE / "transactions/shortstudio-passive-config"


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise RuntimeError("SHORT_STUDIO_PASSIVE_BLOCKED: " + reason)


def regular(path: Path, *, mode: int | None = None) -> bytes:
    require(not path.is_symlink() and path.is_file(), "managed file unavailable")
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
            and info.st_nlink == 1, "unexpected managed file ownership")
    if mode is not None:
        require(stat.S_IMODE(info.st_mode) == mode, "unexpected managed file mode")
    return path.read_bytes()


def env_patch(original: bytes, desired: dict[str, str]) -> bytes:
    """Retain existing secret lines byte-for-byte. Never replace a live value."""
    try:
        lines = original.decode("utf-8").splitlines(keepends=True)
    except UnicodeError as exc:
        raise RuntimeError("SHORT_STUDIO_PASSIVE_BLOCKED: invalid env encoding") from exc
    require(bool(lines), "environment unavailable")
    seen: dict[str, str] = {}
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if "=" not in value:
            continue
        key, stored = value.split("=", 1)
        key = key.strip()
        if key in desired:
            require(key not in seen, "duplicate coordination environment key")
            stored = stored.strip().strip('"').strip("'")
            require(stored == desired[key], "existing coordination key differs")
            seen[key] = stored
    # The frozen feature flags are never enabled by this preparation lane.
    for disabled in ("SHORT_MAKER_DAILY_PROFILES_ENABLED",
                     "SHORT_MAKER_UNUSED_FOLDER_DELETE_ENABLED",
                     "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED",
                     "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED"):
        for line in lines:
            match = re.match(r"^\s*" + disabled + r"\s*=\s*(.*)$", line)
            if match:
                require(match.group(1).strip().strip('"').strip("'") in ("", "0"),
                        "feature already enabled; passive install denied")
    additions = [k + "=" + v + "\n" for k, v in desired.items() if k not in seen]
    if not additions:
        return original
    return (original + (b"" if original.endswith(b"\n") else b"\n")
            + "".join(additions).encode("utf-8"))


def unit_patch(original: bytes, *, name: str, environment: Path) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeError as exc:
        raise RuntimeError("SHORT_STUDIO_PASSIVE_BLOCKED: invalid unit encoding") from exc
    require(text.count("EnvironmentFile=") == 1 and
            f"EnvironmentFile={environment}" in text,
            "unit does not use the approved managed environment")
    require(not re.search(r"(?m)^Environment=.+(?:SHORT_STUDIO_VPS_FOLDER_ROOT|COOPERATIVE_LOCK_ENABLED)", text),
            "unit contains a conflicting direct environment override")
    require(text.count("ReadWritePaths=") == 1, "unit sandbox is ambiguous")
    require("ProtectSystem=strict" in text and "User=ubuntu" in text,
            "unit protection/identity drift")
    shared = str(ROOT)
    if shared not in text.split("ReadWritePaths=", 1)[1].split("\n", 1)[0].split():
        text = re.sub(r"(?m)^(ReadWritePaths=[^\n]+)$",
                      lambda match: match.group(1) + " " + shared, text, count=1)
    # This may increase the sandbox mount only for three named Short Uploader
    # units. No unrelated worker's unit or effective running mount is changed.
    require(text.count("ReadWritePaths=") == 1, "sandbox mutation ambiguous")
    return text.encode("utf-8")


def atomic_owned(path: Path, payload: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(prefix=".short-passive-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(name, path)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(name):
            os.unlink(name)


def _active_pids() -> dict[str, int]:
    from production.shortstudio_cooperation_readonly import _systemd_main_pid
    return {role: _systemd_main_pid(unit) for role, unit in SERVICES.items()}


def _root_check() -> None:
    require(ROOT.is_dir() and not ROOT.is_symlink(), "shared root absent or unsafe")
    require(ROOT.resolve(strict=True) == ROOT and ROOT.stat().st_uid == 1000,
            "shared root ownership/identity drift")
    require(MAKER_DATA.is_dir() and not MAKER_DATA.is_symlink(),
            "Maker data ownership not available")
    require(APP_DB.is_file() and not APP_DB.is_symlink(), "uploader app DB unavailable")
    require(ENGINE_DB.is_file() and not ENGINE_DB.is_symlink(), "uploader engine DB unavailable")


def prepare(revision: str) -> None:
    """Exact SHA guarded by runtime.main; never stop/start a service."""
    require(re.fullmatch(r"[a-f0-9]{40}", revision) is not None, "revision invalid")
    _root_check()
    original_pids = _active_pids()
    for name in SERVICES.values():
        require(not (UNITS / (name + ".d")).exists(), "unverified unit drop-in")
    files: dict[Path, bytes] = {
        MAKER_ENV: regular(MAKER_ENV, mode=0o600),
        UPLOADER_ENV: regular(UPLOADER_ENV, mode=0o600),
    }
    for role in ("web", "worker", "scheduler"):
        path = UNITS / SERVICES[role]
        files[path] = regular(path, mode=0o644)
    maker_values = {
        "SHORT_STUDIO_VPS_FOLDER_ROOT": str(ROOT),
        "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED": "0",
        "SHORT_UPLOADER_APP_DB": str(APP_DB),
        "SHORT_UPLOADER_ENGINE_DB": str(ENGINE_DB),
    }
    uploader_values = {
        "SHORT_STUDIO_VPS_FOLDER_ROOT": str(ROOT),
        "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED": "0",
    }
    changed: dict[Path, bytes] = {
        MAKER_ENV: env_patch(files[MAKER_ENV], maker_values),
        UPLOADER_ENV: env_patch(files[UPLOADER_ENV], uploader_values),
    }
    for role in ("web", "worker", "scheduler"):
        path = UNITS / SERVICES[role]
        changed[path] = unit_patch(
            files[path], name=role, environment=UPLOADER_ENV)
    # Neither active PID nor systemd environment is changed. A future
    # production activation must prove a durable idle+admission barrier first.
    journal = JOURNALS / revision
    require(not journal.exists() and not journal.is_symlink(),
            "passive configuration transaction already exists")
    journal.mkdir(parents=True, mode=0o700)
    journal.chmod(0o700)
    originals = journal / "originals"
    originals.mkdir(mode=0o700)
    backup_names = {path: str(i) + ".bak" for i, path in enumerate(files)}
    import hashlib
    receipt = {"status": "PREPARED", "revision": revision,
               "files_changed": sum(changed[k] != files[k] for k in files),
               "services_restarted": False, "feature_flags_enabled": False,
               "originals": {path.name: hashlib.sha256(files[path]).hexdigest()
                             for path in files}}
    for path, old in files.items():
        fd = os.open(originals / backup_names[path],
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(old)
            stream.flush()
            os.fsync(stream.fileno())
    updated: list[Path] = []
    try:
        for path, new in changed.items():
            if files[path] != new:
                atomic_owned(path, new, 0o600 if path.suffix == ".env" else 0o644)
                updated.append(path)
        runtime.run(["systemctl", "daemon-reload"], quiet=True)
        current = inspect()
        require(current["same_host_root_proven"]
                and not current["all_guards_enabled"]
                and all(x["configuration_present"] and x["folder_root_safe"]
                        and not x["lock_enabled"]
                        for x in current["units"].values()),
                "passive four-unit host root proof failed")
        require(_active_pids() == original_pids,
                "running process changed during passive configuration")
        # Intentionally do not assert active jobs idle: NO work is interrupted.
        runtime.atomic_json(journal / "result.json", {
            **receipt, "status": "PASSIVE_READY",
            "common_root_configured": True,
            "running_mount_activation_verified": False,
            "active_upload_drain_verified": False,
            "retirement_enabled": False,
        })
        runtime.event(short_studio_passive="PASS", changed_files=len(updated),
                      shared_root_configured=True, live_flags="OFF",
                      stopped_services=0, restarted_services=0,
                      active_drain="NOT_CLAIMED", rollback="READY")
    except BaseException:
        failed = []
        for path in reversed(updated):
            try:
                atomic_owned(path, files[path],
                             0o600 if path.suffix == ".env" else 0o644)
            except (OSError, ValueError) as exc:
                failed.append(type(exc).__name__)
        try:
            runtime.run(["systemctl", "daemon-reload"], quiet=True)
        except (OSError, RuntimeError) as exc:
            failed.append(type(exc).__name__)
        runtime.atomic_json(journal / "result.json", {
            **receipt, "status": "ROLLBACK_FAILED" if failed else "ROLLED_BACK",
            "rollback_errors": failed,
        })
        if failed:
            raise RuntimeError("SHORT_STUDIO_PASSIVE_ROLLBACK_FAILED")
        raise

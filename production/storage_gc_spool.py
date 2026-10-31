"""Root-only, fail-closed alignment of the manual GC request spool.

This does not request or execute any cleanup. The fixed Scene candidate web
service is the only allowed writer. Never widen permissions to make a failed
identity check pass.
"""
from __future__ import annotations

import fcntl
import grp
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile

UNIT = "vps1-scene-candidate.service"
DIR = Path("/run/vps1-storage-gc-manual")
CONF = Path("/etc/tmpfiles.d/vps1-storage-gc-manual.conf")
TEMPLATE = Path("infrastructure/tmpfiles.d/vps1-storage-gc-manual.conf")
OLD_USER = "ubuntu"


def parse_identity(properties: str, status: str, *, get_user=pwd.getpwnam):
    """Prove systemd User matches effective UID of the live service MainPID."""
    items = {}
    for line in properties.splitlines():
        key, sep, value = line.partition("=")
        if sep != "=" or key in items or key not in ("User", "MainPID", "ActiveState"):
            raise RuntimeError("Scene service identity cannot be verified")
        items[key] = value.strip()
    if items.get("ActiveState") != "active" or not re.fullmatch(r"[1-9][0-9]*", items.get("MainPID", "")):
        raise RuntimeError("Scene service is not actively identifiable")
    name = items.get("User", "")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name) or name == "root":
        raise RuntimeError("Scene web service identity is not an approved non-root account")
    account = get_user(name)
    if not 0 < account.pw_uid < 65534:
        raise RuntimeError("Scene web account is root, nobody or invalid")
    lines = [line.split() for line in status.splitlines() if line.startswith("Uid:")]
    if len(lines) != 1 or len(lines[0]) != 5 or any(not item.isdecimal() for item in lines[0][1:]):
        raise RuntimeError("Scene worker effective UID is unavailable")
    if int(lines[0][2]) != account.pw_uid or int(lines[0][1]) != account.pw_uid:
        raise RuntimeError("Systemd identity does not match the live Scene worker")
    group = grp.getgrgid(account.pw_gid).gr_name
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", group):
        raise RuntimeError("Scene web service group is invalid")
    return name, group, account.pw_uid, account.pw_gid, int(items["MainPID"])


def identity():
    response = subprocess.run(
        ["systemctl", "show", UNIT, "--property=User", "--property=MainPID",
         "--property=ActiveState", "--no-pager"],
        capture_output=True, text=True, timeout=12, check=True)
    properties = response.stdout
    pid_match = re.search(r"^MainPID=([1-9][0-9]*)$", properties, re.MULTILINE)
    if not pid_match:
        raise RuntimeError("No live Scene service PID")
    pid = int(pid_match.group(1))
    proc = Path("/proc") / str(pid) / "status"
    if proc.is_symlink():
        raise RuntimeError("Unsafe proc identity")
    status = proc.read_text(encoding="ascii")
    resolved = parse_identity(properties, status)
    # Double-check the systemd MainPID after /proc read to guard PID changes.
    confirm = subprocess.run(
        ["systemctl", "show", UNIT, "--property=MainPID", "--no-pager"],
        capture_output=True, text=True, timeout=12, check=True).stdout.strip()
    if confirm != "MainPID=" + str(pid):
        raise RuntimeError("Scene worker PID changed while verifying spool identity")
    return resolved


def _checked_dir(path: Path, allowed: set[int]) -> None:
    current = path.lstat()
    if not stat.S_ISDIR(current.st_mode) or current.st_uid not in allowed or current.st_mode & 0o077:
        raise RuntimeError("Manual GC spool is not a private managed directory")


def provision() -> None:
    if os.geteuid() != 0:
        raise PermissionError("Storage GC spool alignment is root-only")
    name, group, uid, gid, _ = identity()
    old_uid = pwd.getpwnam(OLD_USER).pw_uid
    private = {old_uid, uid}
    _checked_dir(DIR, private)
    if (DIR / "request").exists() or (DIR / "request").is_symlink():
        raise RuntimeError("Pending manual request must be handled before spool alignment")
    for path in DIR.iterdir():
        if path.name not in {"request.lock", "last-request"} or path.is_symlink():
            raise RuntimeError("Unexpected item in guarded spool")
        item = path.lstat()
        if not stat.S_ISREG(item.st_mode) or item.st_uid not in private or item.st_mode & 0o077:
            raise RuntimeError("Unsafe existing manual spool marker")

    template = TEMPLATE.read_text(encoding="utf-8")
    expected = "d /run/vps1-storage-gc-manual 0700 ubuntu ubuntu -\n"
    if template != expected:
        raise RuntimeError("Manual spool tmpfiles template changed unexpectedly")

    lockpath = DIR / "request.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lockpath, flags, 0o600)
    try:
        lock = os.fstat(fd)
        if not stat.S_ISREG(lock.st_mode) or lock.st_uid not in private | {0} or lock.st_mode & 0o077:
            raise RuntimeError("Manual request lock is not private")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (DIR / "request").exists() or (DIR / "request").is_symlink():
            raise RuntimeError("Manual request appeared during spool verification")
        # Retain a boot-persistent, exact 0700 owner rather than a one-off chown.
        new_conf = expected.replace("ubuntu ubuntu", name + " " + group)
        CONF.parent.mkdir(parents=True, exist_ok=True)
        old_conf = CONF.read_bytes() if CONF.exists() and not CONF.is_symlink() else None
        if CONF.is_symlink():
            raise RuntimeError("Unsafe manual spool configuration")
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                            dir=CONF.parent, prefix=".gc-spool-", delete=False) as fp:
                temp = Path(fp.name)
                fp.write(new_conf)
                fp.flush()
                os.fsync(fp.fileno())
            os.chmod(temp, 0o644)
            os.replace(temp, CONF)
            subprocess.run(["systemd-tmpfiles", "--create", str(CONF)],
                           check=True, timeout=15, capture_output=True)
            for childname in ("request.lock", "last-request"):
                child = DIR / childname
                if child.exists() or child.is_symlink():
                    # Hold the exact non-symlink inode while fixing metadata:
                    # Linux chmod(follow_symlinks=False) is not supported.
                    child_fd = os.open(child, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        existing = os.fstat(child_fd)
                        if (not stat.S_ISREG(existing.st_mode) or
                                existing.st_uid not in private | {0}):
                            raise RuntimeError("Manual spool marker changed during repair")
                        os.fchown(child_fd, uid, gid)
                        os.fchmod(child_fd, 0o600)
                    finally:
                        os.close(child_fd)
            _checked_dir(DIR, {uid})
            mode = DIR.lstat()
            if (mode.st_mode & 0o777) != 0o700:
                raise RuntimeError("Manual spool must retain exact 0700 mode")
            if any((DIR / child).exists() and (DIR / child).lstat().st_uid != uid
                   for child in ("request.lock", "last-request")):
                raise RuntimeError("Manual spool marker ownership mismatch")
        except BaseException:
            # Never broaden permissions on failure. Restore boot-time owner
            # template when provisioning fails and leave cleanup unrequested.
            if old_conf is not None:
                CONF.write_bytes(old_conf)
                os.chmod(CONF, 0o644)
            raise
        finally:
            if temp and temp.exists():
                temp.unlink()
        print("MANUAL_GC_SPOOL=PRIVATE_SERVICE_IDENTITY_VERIFIED NO_CLEANUP_TRIGGERED", flush=True)
    finally:
        os.close(fd)


if __name__ == "__main__":
    provision()

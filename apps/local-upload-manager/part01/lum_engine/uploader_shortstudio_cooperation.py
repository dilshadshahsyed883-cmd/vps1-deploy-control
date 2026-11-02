"""Short Uploader opt-in participant in the Short Studio common folder lease.

This deliberately mirrors the Short Maker lease primitive; the offline suite
asserts that their lock implementation stays identical. Both default OFF.
Uploader HTTP, scheduler and worker entrypoints must all use this module for
retirement even to be considered for a separate release. No deletion route or
live deployment toggle is enabled by this source change alone.
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import stat
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import HTTPException

LOCK_FILENAME = ".short-studio-folder-cooperation.lock"


class ShortStudioFolderLease:
    def __init__(self, folder_root, *, enabled=False, timeout_seconds=8.0):
        self.root = Path(folder_root)
        self.enabled = bool(enabled)
        self.timeout = float(timeout_seconds)
        if not 0 < self.timeout <= 60:
            raise ValueError("Lock wait must be between 0 and 60 seconds")

    def _open(self):
        if (not self.root.is_absolute() or not self.root.is_dir()
                or self.root.is_symlink()):
            raise HTTPException(503, detail="Verified shared studio folder root unavailable")
        # Opening parent through O_DIRECTORY/O_NOFOLLOW prevents a symlink
        # substitution of the path component we control. Real production
        # deployment must independently verify its parent/mount identity.
        parent_fd = os.open(str(self.root),
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(LOCK_FILENAME,
                         os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                         0o600, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        identity = os.fstat(fd)
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
            os.close(fd)
            raise HTTPException(503, detail="Unsafe shared studio coordination file")
        return fd

    @contextmanager
    def _lease(self, kind, *, require_enabled=True):
        if require_enabled and not self.enabled:
            # Disabled shared guard is *only* valid for non-destructive Maker
            # operations. Retirement must never infer readiness from this.
            raise HTTPException(503, detail="Studio cooperation protocol is disabled")
        descriptor = self._open()
        acquired = False
        try:
            deadline = time.monotonic() + self.timeout
            mode = fcntl.LOCK_SH if kind == "shared" else fcntl.LOCK_EX
            while True:
                try:
                    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise HTTPException(503, detail="Studio operation busy; retry after active work")
                    time.sleep(0.025)
            yield
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def producer(self):
        """Protect a Short Maker write/worker only when installed/opted in."""
        if not self.enabled:
            yield
            return
        with self._lease("shared"):
            yield

    @contextmanager
    def retire(self):
        """Candidate exclusive lease; NOT by itself a deployment approval."""
        with self._lease("exclusive"):
            yield
    @asynccontextmanager
    async def producer_async(self):
        """Nonblocking shared lease, cancelled waiters never retain the lock."""
        if not self.enabled:
            yield
            return
        descriptor = self._open()
        acquired = False
        try:
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise HTTPException(
                            503, detail="Studio operation busy; retry after active work")
                    await asyncio.sleep(0.025)
            yield
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


# Public opt-in entrypoints. Nothing runs when disabled. The exact same
# SHORT_STUDIO_VPS_FOLDER_ROOT (not just the same lock filename) is required
# for every service instance, verified by guarded release checks.
import asyncio
from contextlib import asynccontextmanager

UPLOADER_COOPERATION_ENABLED = (
    os.environ.get("LUM_ENGINE_INSTANCE") == "shortstudio"
    and os.environ.get("LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED", "0") == "1"
)
if UPLOADER_COOPERATION_ENABLED and not os.environ.get("SHORT_STUDIO_VPS_FOLDER_ROOT"):
    raise RuntimeError("Shared Short Studio root must be explicitly configured before uploader lock activation")
UPLOADER_SHORT_ROOT = Path(os.environ.get(
    "SHORT_STUDIO_VPS_FOLDER_ROOT",
    "/var/lib/fresh-website/short-maker/vps_short_folders",
))
_uploader_lease = ShortStudioFolderLease(
    UPLOADER_SHORT_ROOT, enabled=UPLOADER_COOPERATION_ENABLED)


@contextmanager
def uploader_shortstudio_guard():
    """Use in the synchronous scheduler / worker around complete operations."""
    with _uploader_lease.producer():
        yield


@asynccontextmanager
async def uploader_shortstudio_guard_async():
    """Use the identical cancellation-safe shared lease in HTTP middleware."""
    async with _uploader_lease.producer_async():
        yield

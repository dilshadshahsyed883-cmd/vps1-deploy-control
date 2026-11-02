"""Shared-file protocol for Short Studio folder operations (stage-only).

The file lock serializes *cooperating* processes on a single local filesystem.
It provides neither distributed consensus nor proof that Short Uploader's web,
scheduler, queue workers, post-upload cleanup and every Maker writer take it.
Never activate retirement on the strength of a lock file alone.

Make this a separate opt-in from the daily profile scheduler. Production defaults
to OFF. Not a replacement for existing per-job and per-profile owner checks.
"""
from __future__ import annotations

import errno
import fcntl
import os
import stat
import time
from contextlib import contextmanager
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

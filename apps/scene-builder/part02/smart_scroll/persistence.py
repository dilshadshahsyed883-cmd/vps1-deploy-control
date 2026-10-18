"""Crash-safe JSON/text persistence helpers.

Writes are performed through a temporary file followed by an atomic replace.
An optional backup copy is kept so the app can recover from partial/corrupt
state after power loss or a forced close.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any


def _flush_and_sync(handle) -> None:
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass



def user_data_dir(app_name: str = "Auto Caption Studio") -> Path:
    """Return a user-writable persistent data directory.

    Windows packaged apps may run from a read-only location, so queue/settings
    state must not depend on the ZIP/EXE folder being writable.
    """
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    folder = root / app_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder

def atomic_write_text(path: str | Path, text: str, *, backup_path: str | Path | None = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")

    with temp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        _flush_and_sync(handle)

    backup = Path(backup_path) if backup_path is not None else None
    previous_available = bool(backup is not None and target.exists())
    if previous_available and backup is not None:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup_temp = backup.with_name(backup.name + ".tmp")
        with target.open("rb") as source, backup_temp.open("wb") as destination:
            shutil.copyfileobj(source, destination)
            _flush_and_sync(destination)
        os.replace(backup_temp, backup)

    os.replace(temp, target)

    # On the first successful save, seed the backup with the valid new file.
    if backup is not None and not previous_available:
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup_temp = backup.with_name(backup.name + ".tmp")
        with target.open("rb") as source, backup_temp.open("wb") as destination:
            shutil.copyfileobj(source, destination)
            _flush_and_sync(destination)
        os.replace(backup_temp, backup)
    return target


def atomic_write_json(
    path: str | Path,
    data: Any,
    *,
    backup_path: str | Path | None = None,
    indent: int | None = 2,
) -> Path:
    payload = json.dumps(data, ensure_ascii=False, indent=indent)
    return atomic_write_text(path, payload, backup_path=backup_path)


def load_json_with_backup(path: str | Path, backup_path: str | Path | None = None) -> tuple[Any, Path]:
    candidates = [Path(path)]
    if backup_path is not None:
        candidates.append(Path(backup_path))
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8")), candidate
        except Exception as exc:  # keep trying the backup
            last_error = exc
    if last_error is not None:
        raise last_error
    raise FileNotFoundError(str(path))


class AsyncAtomicJsonWriter:
    """Single background writer that coalesces repeated saves per target path.

    UI code can submit fresh snapshots without blocking on JSON serialization,
    backup copying, fsync, or disk latency. The newest pending snapshot for a
    path replaces older pending snapshots. ``flush`` is used during clean exit.
    """

    def __init__(self) -> None:
        import threading

        self._condition = threading.Condition()
        self._pending: dict[str, tuple[Path, Any, Path | None, int | None]] = {}
        self._active = False
        self._stopping = False
        self._last_error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="acs-persistence-writer", daemon=True)
        self._thread.start()

    def submit(
        self,
        path: str | Path,
        data: Any,
        *,
        backup_path: str | Path | None = None,
        indent: int | None = 2,
    ) -> None:
        target = Path(path)
        backup = Path(backup_path) if backup_path is not None else None
        key = str(target.absolute())
        with self._condition:
            if self._stopping:
                raise RuntimeError("Persistence writer is closed.")
            self._pending[key] = (target, data, backup, indent)
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping and not self._pending:
                    return
                _key, task = self._pending.popitem()
                self._active = True
            path, data, backup, indent = task
            try:
                atomic_write_json(path, data, backup_path=backup, indent=indent)
                with self._condition:
                    self._last_error = None
            except Exception as exc:
                with self._condition:
                    self._last_error = exc
            finally:
                with self._condition:
                    self._active = False
                    self._condition.notify_all()

    def is_idle(self) -> bool:
        with self._condition:
            return not self._active and not self._pending

    def last_error(self) -> Exception | None:
        with self._condition:
            return self._last_error

    def flush(self, timeout: float = 10.0) -> bool:
        import time

        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._active or self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(0.25, remaining))
            return self._last_error is None

    def close(self, timeout: float = 10.0) -> bool:
        ok = self.flush(timeout=timeout)
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout=max(0.0, float(timeout)))
        return ok and not self._thread.is_alive()

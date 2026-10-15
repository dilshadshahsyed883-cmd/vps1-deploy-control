from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
import os
import queue
import signal
import subprocess
import time
from typing import Any, Callable, Sequence


class ManagedProcessError(RuntimeError):
    pass


class ManagedProcessCancelled(ManagedProcessError):
    pass


CancelCheck = Callable[[], bool]
Heartbeat = Callable[[], None]
Progress = Callable[[int, str], None]


def _terminate_pid_tree(pid: int, grace_seconds: float = 3.0) -> None:
    if pid <= 0:
        return
    if os.name == "posix":
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return
        try:
            if pgid == pid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + max(0.1, grace_seconds)
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        try:
            if pgid == pid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=max(2.0, grace_seconds + 1.0),
        )
    except Exception:
        pass


def run_cancellable_command(
    command: Sequence[str],
    *,
    cancel_requested: CancelCheck,
    heartbeat: Heartbeat,
    timeout: float | None = None,
    poll_seconds: float = 0.25,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    proc = subprocess.Popen(
        [str(item) for item in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    try:
        while proc.poll() is None:
            heartbeat()
            if cancel_requested():
                _terminate_pid_tree(proc.pid)
                proc.wait(timeout=5)
                raise ManagedProcessCancelled("Job cancelled while external process was running")
            if timeout is not None and time.monotonic() - started > timeout:
                _terminate_pid_tree(proc.pid)
                proc.wait(timeout=5)
                raise ManagedProcessError(
                    f"Process timed out after {timeout:.1f}s: {Path(str(command[0])).name}"
                )
            time.sleep(max(0.05, poll_seconds))
        stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=int(proc.returncode or 0),
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        if proc.poll() is None:
            _terminate_pid_tree(proc.pid)


def run_render_in_subprocess(
    settings: Any,
    *,
    progress: Progress,
    cancel_requested: CancelCheck,
    heartbeat: Heartbeat,
    poll_seconds: float = 0.25,
    cancel_grace_seconds: float = 3.0,
) -> None:
    import multiprocessing as mp

    from .render_process import render_entry

    if is_dataclass(settings):
        payload = asdict(settings)
    elif isinstance(settings, dict):
        payload = dict(settings)
    else:
        raise TypeError("settings must be a dataclass or dictionary")

    context = mp.get_context("spawn")
    messages = context.Queue()
    cancel_event = context.Event()
    process = context.Process(
        target=render_entry,
        args=(payload, messages, cancel_event),
        name="auto-caption-render",
        daemon=False,
    )
    process.start()
    result: dict[str, Any] | None = None
    try:
        while process.is_alive():
            heartbeat()
            while True:
                try:
                    item = messages.get_nowait()
                except queue.Empty:
                    break
                if item.get("type") == "progress":
                    progress(int(item.get("progress") or 0), str(item.get("message") or ""))
                elif item.get("type") in {"result", "error"}:
                    result = dict(item)
            if cancel_requested():
                cancel_event.set()
                deadline = time.monotonic() + max(0.1, cancel_grace_seconds)
                while process.is_alive() and time.monotonic() < deadline:
                    heartbeat()
                    process.join(timeout=0.1)
                if process.is_alive():
                    _terminate_pid_tree(process.pid, grace_seconds=1.0)
                    process.join(timeout=3.0)
                raise ManagedProcessCancelled("Render/Whisper process cancelled")
            process.join(timeout=max(0.05, poll_seconds))

        while True:
            try:
                item = messages.get_nowait()
            except queue.Empty:
                break
            if item.get("type") == "progress":
                progress(int(item.get("progress") or 0), str(item.get("message") or ""))
            elif item.get("type") in {"result", "error"}:
                result = dict(item)

        if result and result.get("type") == "error":
            raise ManagedProcessError(str(result.get("error") or "Render subprocess failed"))
        if process.exitcode != 0:
            raise ManagedProcessError(f"Render subprocess exited with code {process.exitcode}")
        if not result or result.get("type") != "result":
            raise ManagedProcessError("Render subprocess exited without a completion result")
    finally:
        if process.is_alive():
            _terminate_pid_tree(process.pid, grace_seconds=1.0)
            process.join(timeout=3.0)
        messages.close()
        messages.join_thread()

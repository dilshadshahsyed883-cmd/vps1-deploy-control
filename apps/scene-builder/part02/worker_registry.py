from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LaneState:
    name: str
    capacity: int
    active: int = 0
    total_started: int = 0
    total_completed: int = 0
    total_failed: int = 0
    last_job_id: str = ""
    last_project: str = ""
    last_message: str = "Idle"
    last_started_at: str = ""
    last_finished_at: str = ""
    busy_seconds: float = 0.0


class WorkerRegistry:
    """Thread-safe runtime telemetry + independent stage capacity gates.

    A project may wait for one lane while another project uses a different lane.
    This is what lets transcription, stock acquisition, rendering, compression and
    upload overlap safely without allowing one stage to consume the entire VPS.
    """

    def __init__(self, capacities: dict[str, int]):
        self._lock = threading.RLock()
        self._states = {name: LaneState(name=name, capacity=max(1, int(cap))) for name, cap in capacities.items()}
        self._semaphores = {name: threading.BoundedSemaphore(state.capacity) for name, state in self._states.items()}
        self.started_monotonic = time.monotonic()
        self.started_at = _iso()

    @contextmanager
    def lane(self, name: str, job_id: str, project: str = "", message: str = ""):
        sem = self._semaphores[name]
        sem.acquire()
        started = time.monotonic()
        with self._lock:
            state = self._states[name]
            state.active += 1
            state.total_started += 1
            state.last_job_id = job_id
            state.last_project = project
            state.last_message = message or "Running"
            state.last_started_at = _iso()
        try:
            yield
        except Exception:
            with self._lock:
                self._states[name].total_failed += 1
            raise
        else:
            with self._lock:
                self._states[name].total_completed += 1
        finally:
            elapsed = max(0.0, time.monotonic() - started)
            with self._lock:
                state = self._states[name]
                state.active = max(0, state.active - 1)
                state.busy_seconds += elapsed
                state.last_finished_at = _iso()
                if state.active == 0:
                    state.last_message = "Idle"
            sem.release()

    def note_external(self, name: str, *, active: int = 0, message: str = "") -> None:
        """Expose an external editing worker in the same stats model."""
        with self._lock:
            if name not in self._states:
                self._states[name] = LaneState(name=name, capacity=max(1, active or 1))
            state = self._states[name]
            state.active = max(0, int(active))
            if message:
                state.last_message = message

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lanes = [asdict(self._states[name]) for name in sorted(self._states)]
        return {
            "started_at": self.started_at,
            "uptime_seconds": int(max(0, time.monotonic() - self.started_monotonic)),
            "lanes": lanes,
            "active_total": sum(int(row["active"]) for row in lanes),
            "capacity_total": sum(int(row["capacity"]) for row in lanes),
        }


def env_capacity(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except Exception:
        return default

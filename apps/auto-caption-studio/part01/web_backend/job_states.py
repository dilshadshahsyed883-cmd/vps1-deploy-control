from __future__ import annotations


HELD = "held"
QUEUED = "queued"
RUNNING = "running"
INTERRUPTED = "interrupted"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

ALL_STATES = {
    HELD,
    QUEUED,
    RUNNING,
    INTERRUPTED,
    COMPLETED,
    FAILED,
    CANCELLED,
}

TERMINAL_STATES = {COMPLETED, FAILED, CANCELLED}
RETRYABLE_STATES = {FAILED, CANCELLED, INTERRUPTED}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    HELD: {QUEUED, FAILED, CANCELLED},
    QUEUED: {RUNNING, CANCELLED},
    RUNNING: {QUEUED, COMPLETED, FAILED, CANCELLED, INTERRUPTED},
    INTERRUPTED: {HELD, QUEUED, COMPLETED, FAILED, CANCELLED},
    FAILED: {HELD, QUEUED},
    CANCELLED: {HELD, QUEUED},
    COMPLETED: set(),
}


class InvalidJobTransition(RuntimeError):
    pass


def validate_transition(current: str, target: str) -> None:
    if current == target:
        return
    if current not in ALL_STATES or target not in ALL_STATES:
        raise InvalidJobTransition(f"Unknown job state transition: {current!r} -> {target!r}")
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidJobTransition(f"Invalid job state transition: {current} -> {target}")

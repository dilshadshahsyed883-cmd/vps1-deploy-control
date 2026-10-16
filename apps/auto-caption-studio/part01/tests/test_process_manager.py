from __future__ import annotations

import sys
import time

import pytest


def test_external_process_cancellation_terminates_process_group():
    from web_backend.process_manager import ManagedProcessCancelled, run_cancellable_command

    started = time.monotonic()
    heartbeats = []

    with pytest.raises(ManagedProcessCancelled):
        run_cancellable_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cancel_requested=lambda: time.monotonic() - started > 0.2,
            heartbeat=lambda: heartbeats.append(time.monotonic()),
            timeout=10,
            poll_seconds=0.05,
        )
    assert heartbeats
    assert time.monotonic() - started < 5


def test_external_process_success_reports_output():
    from web_backend.process_manager import run_cancellable_command

    result = run_cancellable_command(
        [sys.executable, "-c", "print('ok')"],
        cancel_requested=lambda: False,
        heartbeat=lambda: None,
        timeout=5,
        poll_seconds=0.05,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"

"""Compatibility import for the single authoritative Phase 1 worker.

Production and legacy callers both resolve to worker_v2. There is no second
queue-claim or render implementation in this module.
"""

from .worker_v2 import process_job, run_once

__all__ = ["process_job", "run_once"]

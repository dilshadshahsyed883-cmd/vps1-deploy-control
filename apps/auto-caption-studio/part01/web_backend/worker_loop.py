from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid

from .queue_store import reconcile_stale_jobs
from .worker_v2 import run_once


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


def main() -> None:
    shutdown = threading.Event()

    def request_shutdown(signum, _frame) -> None:
        logging.warning("Worker shutdown requested by signal %s", signum)
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    worker_id = (
        f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:10]}"
    )
    recovered = reconcile_stale_jobs(auto_requeue=True)
    if recovered:
        logging.warning("Startup recovery reconciled jobs: %s", recovered)
    logging.info("Auto Caption production worker started: %s", worker_id)

    while not shutdown.is_set():
        try:
            result = run_once(worker_id, shutdown_requested=shutdown.is_set)
            if result.get("status") == "idle":
                shutdown.wait(2.0)
            else:
                logging.info("Worker result: %s", result)
        except Exception:
            logging.exception("Worker iteration failed")
            shutdown.wait(5.0)
    logging.info("Auto Caption production worker stopped: %s", worker_id)


if __name__ == "__main__":
    main()

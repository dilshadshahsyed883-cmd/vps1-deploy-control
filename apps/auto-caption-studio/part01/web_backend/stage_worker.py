from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import uuid

from .compression_worker import compress_job
from .database import initialize_storage
from .pipeline_v3 import claim_stage, publish_job
from .queue_store import reconcile_stale_jobs
from .worker_runtime import update_worker_runtime
from .worker_v2 import process_job


STAGES = {
    "render": ("ready_render", process_job),
    "compress": ("ready_compress", compress_job),
    "upload": ("ready_upload", publish_job),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=sorted(STAGES))
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    initialize_storage()
    reconcile_stale_jobs(auto_requeue=True)
    shutdown = threading.Event()

    def request_shutdown(signum, _frame) -> None:
        logging.warning("%s worker shutdown requested by signal %s", args.stage, signum)
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    worker_id = f"{args.stage}-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    queue_stage, processor = STAGES[args.stage]
    runtime_lock = threading.Lock()
    runtime = {"state": "idle", "current_job_id": "", "message": "Waiting for work"}

    def set_runtime(state: str, current_job_id: str = "", message: str = "") -> None:
        with runtime_lock:
            runtime.update(state=state, current_job_id=current_job_id, message=message)
            current = dict(runtime)
        update_worker_runtime(args.stage, worker_id, **current)

    def pulse() -> None:
        while not shutdown.wait(5.0):
            with runtime_lock:
                current = dict(runtime)
            try:
                update_worker_runtime(args.stage, worker_id, **current)
            except Exception:
                logging.exception("Unable to update %s runtime heartbeat", args.stage)

    set_runtime("idle", message="Waiting for work")
    heartbeat_thread = threading.Thread(
        target=pulse, name=f"{args.stage}-runtime-heartbeat", daemon=True
    )
    heartbeat_thread.start()
    try:
        while not shutdown.is_set():
            item = claim_stage(queue_stage, worker_id)
            if item is None:
                state = "paused" if _is_paused(args.stage) else "idle"
                set_runtime(
                    state,
                    message=(
                        "Paused after current" if state == "paused" else "Waiting for work"
                    ),
                )
                shutdown.wait(1.0)
                continue
            job_id = str(item["id"])
            set_runtime(
                "working", current_job_id=job_id,
                message=str(item.get("message") or "Processing"),
            )
            try:
                processor(item, worker_id, shutdown_requested=shutdown.is_set)
            except Exception:
                logging.exception("%s worker failed outside stage finalization", args.stage)
            set_runtime("idle", message="Waiting for work")
    finally:
        set_runtime("stopped", message="Service stopped")
        heartbeat_thread.join(timeout=6)
    return 0


def _is_paused(stage: str) -> bool:
    from .database import connect

    with connect() as db:
        row = db.execute(
            "SELECT paused FROM acs_pipeline_controls WHERE name=?", (stage,)
        ).fetchone()
    return bool(row and int(row["paused"] or 0))


if __name__ == "__main__":
    raise SystemExit(main())

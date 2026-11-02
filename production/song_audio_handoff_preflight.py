"""Read-only, bounded Song Maker handoff drain before atomic Song Audio cutover.

Never changes SQLite rows, accepts a synthetic client identity, skips a pending
handoff, or relaxes the original all-owner idle gate. No filenames, usernames,
job IDs or Google Drive paths are printed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

from production import runtime

BUSY_HANDOFF = "Audio Downloader has active Song Maker handoffs; release postponed"
DRAIN_SECONDS = 180
POLL_SECONDS = 12


def handoff_summary(audio_db: Path) -> dict[str, int]:
    """Assess whether the already-running legacy worker can naturally drain."""
    if audio_db.is_symlink() or not audio_db.is_file():
        raise RuntimeError("Song Audio queue missing during read-only handoff inspection")
    with sqlite3.connect(audio_db.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        db.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        required = {
            "status", "song_maker_status", "auto_song_maker", "auto_upload",
            "download_thumbnail", "song_maker_owner", "local_file",
            "local_thumbnail", "song_maker_next_retry_at",
        }
        if not required <= columns:
            raise RuntimeError("Song Audio handoff schema incomplete")
        rows = db.execute("""
            SELECT status, song_maker_status, auto_song_maker, auto_upload,
                   download_thumbnail, song_maker_owner, local_file,
                   local_thumbnail, song_maker_next_retry_at
              FROM jobs
             WHERE song_maker_status IN ('pending','sending')
        """).fetchall()
    now = datetime.now(timezone.utc)
    summary = {"pending": 0, "sending": 0, "eligible": 0,
               "ineligible": 0, "due": 0, "future_retry": 0,
               "invalid_retry": 0,
               # A row can have multiple reasons. Never log owner or path.
               "job_not_completed": 0, "auto_handoff_off": 0,
               "drive_upload_on": 0, "thumbnail_off": 0,
               "song_owner_missing": 0, "audio_pointer_missing": 0,
               "thumbnail_pointer_missing": 0}
    for status, phase, auto, upload, thumbnail, owner, audio, image, retry in rows:
        summary[phase] += 1
        reasons = {
            "job_not_completed": status != "completed",
            "auto_handoff_off": auto != 1,
            "drive_upload_on": upload != 0,
            "thumbnail_off": thumbnail != 1,
            "song_owner_missing": not bool(owner),
            "audio_pointer_missing": not bool(audio),
            "thumbnail_pointer_missing": not bool(image),
        }
        if any(reasons.values()):
            summary["ineligible"] += 1
            for key, present in reasons.items():
                if present:
                    summary[key] += 1
            continue
        summary["eligible"] += 1
        if not retry:
            summary["due"] += 1
            continue
        try:
            when = datetime.fromisoformat(retry)
            if when.tzinfo is None:
                raise ValueError("naive retry timestamp")
        except (ValueError, TypeError):
            summary["invalid_retry"] += 1
            continue
        summary["future_retry" if when > now else "due"] += 1
    return summary


def require_drain(state: Path, audio_db: Path, *,
                  timeout_seconds: float = DRAIN_SECONDS,
                  poll_seconds: float = POLL_SECONDS) -> dict:
    """Preserve the original preflight; only wait for its real success.

    A permanently ineligible handoff cannot drain using the existing worker.
    Fail without changing its rows. Only the exact known handoff-busy error
    permits retries. Any Voice synthesis/queue corruption fails immediately.
    """
    if timeout_seconds < 0 or poll_seconds <= 0:
        raise ValueError("Unsafe Song Audio drain interval")
    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    while True:
        try:
            runtime.require_voice_release_idle(state, audio_db=audio_db)
            return {"handoff_drain": "PASS", "rechecks": attempts}
        except RuntimeError as exc:
            if str(exc) != BUSY_HANDOFF:
                raise
        summary = handoff_summary(audio_db)
        runtime.event(song_audio_handoff_readonly=summary,
                      original_queue="PRESERVED",
                      release_gate="WAITING_FOR_NATURAL_HANDOFF_DRAIN")
        if (summary["ineligible"] or summary["invalid_retry"] or
                not (summary["pending"] + summary["sending"])):
            raise RuntimeError(
                "Song Audio handoff cannot naturally drain: "
                "ineligible/invalid legacy rows; no queue mutation attempted")
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise RuntimeError(
                "Song Audio handoff did not drain within guarded window; "
                "original jobs and worker preserved")
        attempts += 1
        time.sleep(min(poll_seconds, remain))

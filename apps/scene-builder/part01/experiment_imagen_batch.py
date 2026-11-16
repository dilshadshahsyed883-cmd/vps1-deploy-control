"""Admin-only Imagen batch queue and durable VPS PNG output store.

No Salad credentials or raw prompts are written to logs. Queue state and PNG files
live in the persistent Scene Builder data directory, outside the GPU container.
Batch IDs/Prompt IDs are validated before they become path segments. A SQLite
lease prevents duplicate processing when more than one web process is running.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import io
import os
import re
import secrets
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

PROMPT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
BATCH_ID = re.compile(r"[0-9a-f]{32}\Z")
PNG_HEADER = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_BATCH_PROMPTS = 1000
# Keep Sana's serial GPU worker fed without exhausting its 16-slot API queue.
# These are upper bounds per web queue cycle, not GPU parallelism settings.
MAX_BATCH_INFLIGHT = 8
MAX_BATCH_POLL_PER_TICK = 8
MAX_BATCH_SUBMIT_PER_TICK = 8
GPU_UNAVAILABLE_PAUSE_SECONDS = 20
MAX_TEXT_BYTES = 256 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024
MAX_VPS_BYTES = 15 * 1024 * 1024 * 1024
MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024


def parse_prompt_lines(value: str) -> list[tuple[str, str]]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise HTTPException(422, "Batch prompt text is too large")
    prompts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for number, line in enumerate(value.splitlines(), 1):
        if not line.strip():
            continue
        if ":" not in line:
            raise HTTPException(422, f"Line {number}: use PromptID: Prompt text")
        identifier, prompt = (part.strip() for part in line.split(":", 1))
        if not PROMPT_ID.fullmatch(identifier):
            raise HTTPException(422, f"Line {number}: invalid Prompt ID (letters, numbers, _ or -)")
        if identifier.casefold() in seen:
            raise HTTPException(422, f"Line {number}: duplicate Prompt ID {identifier}")
        if not 1 <= len(prompt) <= 1500:
            raise HTTPException(422, f"Line {number}: prompt must contain 1–1500 characters")
        prompts.append((identifier, prompt))
        seen.add(identifier.casefold())
        if len(prompts) > MAX_BATCH_PROMPTS:
            raise HTTPException(422, "Maximum 1000 prompts per batch")
    if not prompts:
        raise HTTPException(422, "Provide at least one PromptID: Prompt text line")
    return prompts


def validate_png(data: bytes) -> None:
    """Read entire PNG structure and chunk CRCs (without image decoders)."""
    if len(data) < 57 or len(data) > MAX_IMAGE_BYTES or not data.startswith(PNG_HEADER):
        raise HTTPException(502, "GPU output is not a valid PNG")
    at, width, height, seen_idat, ended = 8, 0, 0, False, False
    while at + 12 <= len(data):
        n = struct.unpack_from(">I", data, at)[0]
        if n > MAX_IMAGE_BYTES or at + n + 12 > len(data):
            break
        kind = data[at + 4:at + 8]
        payload = data[at + 8:at + 8 + n]
        crc = struct.unpack_from(">I", data, at + 8 + n)[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            break
        if at == 8:
            if kind != b"IHDR" or n != 13:
                break
            width, height = struct.unpack_from(">II", payload, 0)
            if not (0 < width <= 8192 and 0 < height <= 8192):
                break
        if kind == b"IDAT":
            seen_idat = True
        at += n + 12
        if kind == b"IEND" and n == 0:
            ended = at == len(data)
            break
    if not (seen_idat and ended):
        raise HTTPException(502, "GPU output PNG is damaged or incomplete")


class ImagenBatchQueue:
    def __init__(self, bridge):
        self.bridge = bridge
        self.root = bridge.root / "imagen-results"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.stop_event = threading.Event()
        self.wakeup = threading.Event()
        self.thread = None
        self.gpu_retry_after = 0.0
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS imagen_batches (
              id TEXT PRIMARY KEY, config_owner TEXT NOT NULL, gateway TEXT NOT NULL,
              width INTEGER NOT NULL, height INTEGER NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS imagen_batch_items (
              batch_id TEXT NOT NULL, prompt_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              remote_job_id TEXT, error TEXT NOT NULL DEFAULT '',
              attempts INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
              lease_token TEXT, lease_until INTEGER NOT NULL DEFAULT 0,
              size INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(batch_id,prompt_id),
              FOREIGN KEY(batch_id) REFERENCES imagen_batches(id)
            );
            CREATE INDEX IF NOT EXISTS ix_imagen_batch_claim
              ON imagen_batch_items(state,retry_at,lease_until);
            CREATE TABLE IF NOT EXISTS imagen_single_jobs (
              job_id TEXT PRIMARY KEY, config_owner TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending', retry_at INTEGER NOT NULL DEFAULT 0,
              lease_token TEXT, lease_until INTEGER NOT NULL DEFAULT 0,
              saved_count INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS imagen_single_media (
              job_id TEXT NOT NULL, media_index INTEGER NOT NULL, size INTEGER NOT NULL,
              sha256 TEXT NOT NULL, PRIMARY KEY(job_id,media_index)
            );
            CREATE TABLE IF NOT EXISTS imagen_deleted_outputs (
              job_id TEXT NOT NULL, media_index INTEGER NOT NULL,
              PRIMARY KEY(job_id,media_index)
            );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.bridge.db, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="imagen-vps-output-queue", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wakeup.set()

    def _run(self):
        while not self.stop_event.is_set():
            processed = False
            try:
                processed = self.tick()
            except Exception:
                # Supervisor must remain alive; failed items keep their DB state
                # and can be retried or recovered by another process.
                import logging
                logging.getLogger(__name__).exception("Imagen background queue cycle failed")
            # No fixed 2s penalty between ready submissions. Idle loops remain
            # bounded; SQLite retry_at prevents repeated GPU status polling.
            self.wakeup.wait(0.25 if processed else 1.0)
            self.wakeup.clear()

    def submit(self, config_owner: str, prompts: str, width=1024, height=576):
        items = parse_prompt_lines(prompts)
        try:
            width, height = int(width), int(height)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(422, "Invalid resolution") from None
        if not (512 <= width <= 1024 and 512 <= height <= 1024 and
                width % 8 == 0 and height % 8 == 0):
            raise HTTPException(422, "Batch resolution must be 512–1024 and divisible by 8")
        cfg = self.bridge.config("imagen", config_owner, reveal=True)
        if not cfg["endpoint"] or not cfg["api_key"]:
            raise HTTPException(503, "Save Imagen GPU Config first")
        if "qwen-image-edit" in str(cfg.get("image_ref", "")).lower():
            raise HTTPException(422, "Qwen Image Edit cannot generate text-only batch images")
        batch_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO imagen_batches(id,config_owner,gateway,width,height) VALUES(?,?,?,?,?)",
                       (batch_id, config_owner, cfg["endpoint"], width, height))
            db.executemany(
                "INSERT INTO imagen_batch_items(batch_id,prompt_id,ordinal,prompt) VALUES(?,?,?,?)",
                [(batch_id, pid, i, prompt) for i, (pid, prompt) in enumerate(items)]
            )
        self.wakeup.set()
        return {"batch_id": batch_id, "count": len(items), "status": "queued"}

    def _get_batch(self, batch_id):
        if not BATCH_ID.fullmatch(batch_id):
            raise HTTPException(404, "Batch not found")
        with self.connect() as db:
            row = db.execute("SELECT * FROM imagen_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Batch not found")
        return dict(row)

    def list_batches(self):
        with self.connect() as db:
            rows = db.execute("""
              SELECT b.id,b.created_at,COUNT(i.prompt_id) AS total,
              SUM(CASE WHEN i.state='saved' THEN 1 ELSE 0 END) AS saved,
              SUM(CASE WHEN i.state='failed' THEN 1 ELSE 0 END) AS failed
              FROM imagen_batches b JOIN imagen_batch_items i ON b.id=i.batch_id
              GROUP BY b.id ORDER BY b.created_at DESC,b.id DESC LIMIT 40
            """).fetchall()
        return {"items": [dict(row) for row in rows]}

    def details(self, batch_id):
        batch = self._get_batch(batch_id)
        with self.connect() as db:
            items = db.execute("""
              SELECT prompt_id,ordinal,prompt,state,error,size,sha256,remote_job_id
              FROM imagen_batch_items WHERE batch_id=? ORDER BY ordinal
            """, (batch_id,)).fetchall()
        return {"batch_id": batch_id, "created_at": batch["created_at"],
                "items": [{**dict(row), "filename": row["prompt_id"] + ".png"} for row in items]}

    def retry(self, batch_id, identifiers):
        self._get_batch(batch_id)
        if not isinstance(identifiers, list) or not identifiers or len(identifiers) > MAX_BATCH_PROMPTS:
            raise HTTPException(422, "Select failed prompt IDs to retry")
        if any(not isinstance(i, str) or not PROMPT_ID.fullmatch(i) for i in identifiers):
            raise HTTPException(422, "Invalid Prompt ID")
        if len(set(identifiers)) != len(identifiers):
            raise HTTPException(422, "Duplicate Prompt ID selection")
        with self.connect() as db:
            rows = db.execute("SELECT prompt_id,state FROM imagen_batch_items WHERE batch_id=?",
                              (batch_id,)).fetchall()
            current = {row["prompt_id"]: row["state"] for row in rows}
            if any(current.get(i) != "failed" for i in identifiers):
                raise HTTPException(409, "Only failed prompts can be retried")
            db.executemany("""
              UPDATE imagen_batch_items SET state='pending',remote_job_id=NULL,error='',
                  attempts=0,retry_at=0,lease_token=NULL,lease_until=0
              WHERE batch_id=? AND prompt_id=? AND state='failed'
            """, [(batch_id, i) for i in identifiers])
        self.wakeup.set()
        return {"retrying": len(identifiers)}

    def track_single(self, config_owner: str, job_id: str):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO imagen_single_jobs(job_id,config_owner) VALUES(?,?)",
                       (job_id, config_owner))
        self.wakeup.set()

    def _claim(self, table: str, *, phase="poll"):
        now = int(time.time())
        token = secrets.token_hex(12)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if table == "batch":
                if phase not in ("poll", "submit"):
                    raise ValueError("Unknown Imagen batch queue phase")
                if phase == "submit":
                    # Count leased submissions as well as jobs already accepted by
                    # the GPU. The SQLite write lock makes this cap safe when
                    # multiple website processes share the same database.
                    active = db.execute("""
                      SELECT COUNT(*) FROM imagen_batch_items
                      WHERE (remote_job_id IS NOT NULL
                             AND state IN ('submitted','waiting_gpu'))
                         OR (remote_job_id IS NULL
                             AND state IN ('pending','waiting_gpu') AND lease_until>?)
                    """, (now,)).fetchone()[0]
                    if active >= MAX_BATCH_INFLIGHT:
                        return (None, None)
                    state_filter = "i.remote_job_id IS NULL AND i.state IN ('pending','waiting_gpu')"
                else:
                    state_filter = "i.remote_job_id IS NOT NULL AND i.state IN ('submitted','waiting_gpu')"
                row = db.execute(f"""
                  SELECT i.*,b.config_owner,b.gateway,b.width,b.height FROM imagen_batch_items i
                  JOIN imagen_batches b ON i.batch_id=b.id
                  WHERE {state_filter}
                    AND i.retry_at<=? AND i.lease_until<=?
                  ORDER BY b.created_at,i.ordinal LIMIT 1
                """, (now, now)).fetchone()
                if row:
                    db.execute("""
                      UPDATE imagen_batch_items SET lease_token=?,lease_until=?
                      WHERE batch_id=? AND prompt_id=?
                    """, (token, now + 150, row["batch_id"], row["prompt_id"]))
            else:
                row = db.execute("""
                  SELECT * FROM imagen_single_jobs
                  WHERE state IN ('pending','waiting_gpu') AND retry_at<=? AND lease_until<=?
                  ORDER BY rowid LIMIT 1
                """, (now, now)).fetchone()
                if row:
                    db.execute("UPDATE imagen_single_jobs SET lease_token=?,lease_until=? WHERE job_id=?",
                               (token, now + 150, row["job_id"]))
        return (dict(row), token) if row else (None, None)

    def _batch_update(self, row, token, state, *, job_id=None, error="", delay=0, increment=False,
                      size=0, sha256=""):
        with self.connect() as db:
            db.execute("""
              UPDATE imagen_batch_items SET state=?,error=?,retry_at=?,
                remote_job_id=COALESCE(?,remote_job_id),
                attempts=attempts+?,size=CASE WHEN ? > 0 THEN ? ELSE size END,
                sha256=CASE WHEN ? != '' THEN ? ELSE sha256 END,
                lease_token=NULL,lease_until=0
              WHERE batch_id=? AND prompt_id=? AND lease_token=?
            """, (state, error[:250], int(time.time()) + delay, job_id,
                  1 if increment else 0, size, size, sha256, sha256,
                  row["batch_id"], row["prompt_id"], token))

    def _single_update(self, row, token, state, *, delay=0, error="", saved_count=0):
        with self.connect() as db:
            db.execute("""
              UPDATE imagen_single_jobs SET state=?,error=?,retry_at=?,
                saved_count=MAX(saved_count,?),lease_token=NULL,lease_until=0
              WHERE job_id=? AND lease_token=?
            """, (state, error[:250], int(time.time()) + delay, saved_count,
                  row["job_id"], token))

    def _path(self, kind: str, job_or_batch_id: str, identifier: str) -> Path:
        if kind == "batch":
            if not BATCH_ID.fullmatch(job_or_batch_id) or not PROMPT_ID.fullmatch(identifier):
                raise HTTPException(404, "Image not found")
            return self.root / "batches" / job_or_batch_id / (identifier + ".png")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_or_batch_id):
            raise HTTPException(404, "Image not found")
        if not re.fullmatch(r"scene_[0-9]{3}", identifier):
            raise HTTPException(404, "Image not found")
        return self.root / "single" / job_or_batch_id / (identifier + ".png")

    def _store(self, path: Path, data: bytes) -> tuple[int, str]:
        validate_png(data)
        size, digest = len(data), hashlib.sha256(data).hexdigest()
        if path.is_file():
            existing = path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise HTTPException(409, "A different PNG is already saved with this ID")
            return size, digest
        if shutil.disk_usage(self.root).free < size + MIN_FREE_BYTES:
            raise HTTPException(507, "VPS image storage free-space guard reached")
        with self.connect() as db:
            rows = db.execute("""
              SELECT COALESCE(SUM(size),0) FROM imagen_batch_items WHERE state='saved'
            """).fetchone()[0]
            single = db.execute("SELECT COALESCE(SUM(size),0) FROM imagen_single_media").fetchone()[0]
            if rows + single + size > MAX_VPS_BYTES:
                raise HTTPException(507, "VPS Imagen storage limit reached")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temp = path.with_name("." + path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with open(temp, "xb") as stream:
                os.chmod(temp, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Existing saved outputs must never be overwritten.
            if path.exists():
                raise HTTPException(409, "Image already saved")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return size, digest

    def _run_batch(self, row, token):
        owner, job_id = row["config_owner"], row["remote_job_id"]
        if row["gateway"] != self.bridge.config("imagen", owner, reveal=True)["endpoint"]:
            self._batch_update(row, token, "waiting_gpu", delay=30,
                               error="Configured Salad gateway changed; restore original to continue")
            return
        try:
            if not job_id:
                result = self.bridge.create("imagen", owner, {
                    "prompt": row["prompt"], "mode": "generate", "width": row["width"],
                    "height": row["height"], "count": 1,
                })
                job_id = result["id"]
                self._batch_update(row, token, "submitted", job_id=job_id, increment=True)
                return
            status = self.bridge.status("imagen", owner, job_id)
            if status.get("worker_error"):
                if "HTTP 404" in status["worker_error"]:
                    self._batch_update(row, token, "failed", error="GPU job was lost after worker restart")
                else:
                    self._batch_update(row, token, "waiting_gpu", delay=20, error="GPU status unavailable")
                    self.gpu_retry_after = max(self.gpu_retry_after,
                                               time.monotonic() + GPU_UNAVAILABLE_PAUSE_SECONDS)
                return
            state = str(status.get("status", "")).lower()
            if state in ("completed", "succeeded", "done"):
                media = self.bridge.media("imagen", owner, job_id, index=0)
                content = media.body
                size, digest = self._store(self._path("batch", row["batch_id"], row["prompt_id"]), content)
                self._batch_update(row, token, "saved", size=size, sha256=digest)
            elif state in ("failed", "cancelled", "interrupted", "error"):
                self._batch_update(row, token, "failed", error=str(status.get("error") or state))
            else:
                self._batch_update(row, token, "submitted", delay=2)
        except HTTPException as exc:
            if exc.status_code in (502, 503, 504, 507):
                self._batch_update(row, token, "waiting_gpu", delay=20,
                                   error=str(exc.detail)[:120])
                self.gpu_retry_after = max(self.gpu_retry_after,
                                           time.monotonic() + GPU_UNAVAILABLE_PAUSE_SECONDS)
            else:
                self._batch_update(row, token, "failed", error=str(exc.detail))
        except (OSError, ValueError, TypeError) as exc:
            self._batch_update(row, token, "failed", error=type(exc).__name__)

    def _run_single(self, row, token):
        owner, job_id = row["config_owner"], row["job_id"]
        try:
            status = self.bridge.status("imagen", owner, job_id)
            if status.get("worker_error"):
                if "HTTP 404" in status["worker_error"]:
                    self._single_update(row, token, "failed", error="Worker discarded the job before VPS transfer")
                else:
                    self._single_update(row, token, "waiting_gpu", delay=20,
                                        error="GPU status unavailable")
                return
            state = str(status.get("status", "")).lower()
            if state in ("failed", "interrupted", "cancelled", "error"):
                self._single_update(row, token, "failed", error=str(status.get("error") or state))
                return
            if state not in ("completed", "succeeded", "done"):
                self._single_update(row, token, "pending", delay=5)
                return
            count = min(8, int(status.get("output_count") or 0))
            if count < 1:
                self._single_update(row, token, "waiting_gpu", delay=10, error="Output not yet reported")
                return
            for index in range(count):
                with self.connect() as db:
                    exists = db.execute(
                        "SELECT 1 FROM imagen_single_media WHERE job_id=? AND media_index=?",
                        (job_id, index)).fetchone()
                with self.connect() as db:
                    removed = db.execute("SELECT 1 FROM imagen_deleted_outputs WHERE job_id=? AND media_index=?",
                                         (job_id, index)).fetchone()
                if exists or removed:
                    continue
                response = self.bridge.media("imagen", owner, job_id, index=index)
                path = self._path("single", job_id, f"scene_{index+1:03d}")
                size, digest = self._store(path, response.body)
                with self.connect() as db:
                    db.execute("INSERT OR IGNORE INTO imagen_single_media(job_id,media_index,size,sha256) VALUES(?,?,?,?)",
                               (job_id, index, size, digest))
            self._single_update(row, token, "saved", saved_count=count)
        except HTTPException as exc:
            state = "waiting_gpu" if exc.status_code in (502, 503, 504, 507) else "failed"
            self._single_update(row, token, state, delay=20 if state=="waiting_gpu" else 0,
                                error=str(exc.detail))
        except (OSError, ValueError, TypeError) as exc:
            self._single_update(row, token, "failed", error=type(exc).__name__)

    def tick(self):
        # Reconcile completed/in-flight GPU jobs first, then keep up to eight
        # additional jobs ahead of the serial Sana worker. Separate phases avoid
        # immediately re-polling a job submitted in this same cycle.
        # Each claim still uses a durable SQLite lease across web processes.
        processed = False
        for _ in range(MAX_BATCH_POLL_PER_TICK):
            row, token = self._claim("batch", phase="poll")
            if not row:
                break
            processed = True
            self._run_batch(row, token)
        if time.monotonic() >= self.gpu_retry_after:
            for _ in range(MAX_BATCH_SUBMIT_PER_TICK):
                if time.monotonic() < self.gpu_retry_after:
                    break
                row, token = self._claim("batch", phase="submit")
                if not row:
                    break
                processed = True
                self._run_batch(row, token)
        # Manual single-image output transfers continue independently.
        row, token = self._claim("single")
        if row:
            processed = True
            self._run_single(row, token)
        return processed

    def _read_verified(self, path: Path, expected: str) -> bytes:
        try:
            data = path.read_bytes()
        except OSError:
            raise HTTPException(503, "Saved image temporarily unavailable") from None
        validate_png(data)
        if hashlib.sha256(data).hexdigest() != expected:
            raise HTTPException(502, "Saved VPS PNG checksum mismatch")
        return data

    @staticmethod
    def _png_response(data: bytes, filename: str, preview=False) -> Response:
        return Response(
            content=data, media_type="image/png",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                     "X-VPS-Media-Saved": "true",
                     "Content-Disposition": f'{"inline" if preview else "attachment"}; filename="{filename}"'}
        )

    def batch_media(self, batch_id, prompt_id, *, preview=False):
        self._get_batch(batch_id)
        if not PROMPT_ID.fullmatch(prompt_id):
            raise HTTPException(404, "Image not found")
        with self.connect() as db:
            row = db.execute("""SELECT sha256 FROM imagen_batch_items
              WHERE batch_id=? AND prompt_id=? AND state='saved'""", (batch_id, prompt_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Image not yet saved to VPS")
        return self._png_response(self._read_verified(self._path("batch", batch_id, prompt_id),
                                                      row["sha256"]), prompt_id + ".png", preview)

    def local_job_media(self, job_id: str, index: int, *, preview=False):
        if index < 0 or index > 7:
            return None
        with self.connect() as db:
            if db.execute("SELECT 1 FROM imagen_deleted_outputs WHERE job_id=? AND media_index=?",
                          (job_id, index)).fetchone():
                raise HTTPException(404, "Output was deleted")
            batch = db.execute("""SELECT batch_id,prompt_id,sha256 FROM imagen_batch_items
                 WHERE remote_job_id=? AND state='saved'""", (job_id,)).fetchone()
            if batch and index == 0:
                return self.batch_media(batch["batch_id"], batch["prompt_id"], preview=preview)
            row = db.execute("""SELECT sha256 FROM imagen_single_media
                 WHERE job_id=? AND media_index=?""", (job_id, index)).fetchone()
        if row:
            filename = f"scene_{index+1:03d}.png"
            return self._png_response(self._read_verified(self._path("single", job_id,
                                        f"scene_{index+1:03d}"), row["sha256"]), filename, preview)
        return None

    def saved_count(self, job_id):
        with self.connect() as db:
            batch = db.execute("""SELECT COUNT(*) FROM imagen_batch_items
              WHERE remote_job_id=? AND state='saved'""", (job_id,)).fetchone()[0]
            singles = db.execute("SELECT COUNT(*) FROM imagen_single_media WHERE job_id=?",
                                 (job_id,)).fetchone()[0]
        return max(batch, singles)


    def visible_indices(self, job_id: str, count: int) -> list[int]:
        """Preserve original output indices after individual deletions."""
        try:
            count = max(0, min(8, int(count or 0)))
        except (TypeError, ValueError, OverflowError):
            count = 0
        with self.connect() as db:
            singles = db.execute("SELECT media_index FROM imagen_single_media WHERE job_id=?",
                                 (job_id,)).fetchall()
            batch = db.execute("SELECT 1 FROM imagen_batch_items WHERE remote_job_id=? AND state='saved'",
                               (job_id,)).fetchone()
            hidden = db.execute("SELECT media_index FROM imagen_deleted_outputs WHERE job_id=?",
                                (job_id,)).fetchall()
        found = set(range(count)) | {row["media_index"] for row in singles}
        if batch:
            found.add(0)
        return sorted((found - {row["media_index"] for row in hidden}) & set(range(8)))

    @staticmethod
    def _remove_file(path: Path):
        if path.is_symlink():
            raise HTTPException(409, "Cannot delete a linked image")
        path.unlink(missing_ok=True)

    def delete_output(self, owner: str, job_id: str, index: int):
        self.bridge.require_job("imagen", owner, job_id)
        if index < 0 or index > 7:
            raise HTTPException(404, "Output not found")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM gpu_jobs WHERE studio='imagen' AND owner=? AND id=?",
                             (owner, job_id)).fetchone()
            terminal = ("completed", "succeeded", "done", "failed", "error", "cancelled", "canceled")
            if row is None:
                raise HTTPException(404, "Job not found")
            if row["status"].lower() not in terminal:
                raise HTTPException(409, "Finish or cancel the job before deleting an output")
            if db.execute("SELECT 1 FROM imagen_deleted_outputs WHERE job_id=? AND media_index=?",
                          (job_id, index)).fetchone():
                raise HTTPException(404, "Output already deleted")
            batch = db.execute("""
                SELECT i.batch_id,i.prompt_id,i.state,i.lease_until FROM imagen_batch_items i
                JOIN imagen_batches b ON b.id=i.batch_id
                WHERE i.remote_job_id=? AND b.config_owner=?
            """, (job_id, owner)).fetchone()
            if batch:
                if index != 0 or batch["state"] != "saved" or batch["lease_until"] > int(time.time()):
                    raise HTTPException(409, "Batch output is not ready for deletion")
                db.execute("""UPDATE imagen_batch_items SET state='deleted',size=0,sha256='',error=''
                              WHERE batch_id=? AND prompt_id=?""",
                           (batch["batch_id"], batch["prompt_id"]))
                path = self._path("batch", batch["batch_id"], batch["prompt_id"])
            else:
                single = db.execute("SELECT lease_until FROM imagen_single_jobs WHERE job_id=? AND config_owner=?",
                                    (job_id, owner)).fetchone()
                if single and single["lease_until"] > int(time.time()):
                    raise HTTPException(409, "VPS transfer in progress; retry shortly")
                db.execute("DELETE FROM imagen_single_media WHERE job_id=? AND media_index=?",
                           (job_id, index))
                path = self._path("single", job_id, f"scene_{index+1:03d}")
            db.execute("INSERT INTO imagen_deleted_outputs(job_id,media_index) VALUES(?,?)",
                       (job_id, index))
        self._remove_file(path)
        return {"deleted": True, "job_id": job_id, "index": index}

    def delete_job(self, owner: str, job_id: str):
        """Delete a terminal job and any linked local images; never drop running work."""
        self.bridge.require_job("imagen", owner, job_id)
        files = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            status = db.execute("SELECT status FROM gpu_jobs WHERE studio='imagen' AND owner=? AND id=?",
                                (owner, job_id)).fetchone()
            if status is None:
                raise HTTPException(404, "Job not found")
            if status["status"].lower() not in (
                "completed", "succeeded", "done", "failed", "error", "cancelled", "canceled"
            ):
                raise HTTPException(409, "Finish or cancel the GPU job before deleting it")
            batch_items = db.execute("""
                SELECT i.batch_id,i.prompt_id,i.state,i.lease_until FROM imagen_batch_items i
                JOIN imagen_batches b ON b.id=i.batch_id
                WHERE i.remote_job_id=? AND b.config_owner=?
            """, (job_id, owner)).fetchall()
            if any(x["state"] not in ("saved", "failed", "deleted") or
                   x["lease_until"] > int(time.time()) for x in batch_items):
                raise HTTPException(409, "Batch job is still active")
            single = db.execute("SELECT lease_until FROM imagen_single_jobs WHERE job_id=? AND config_owner=?",
                                (job_id, owner)).fetchone()
            if single and single["lease_until"] > int(time.time()):
                raise HTTPException(409, "VPS image transfer in progress; retry shortly")
            files = [self._path("batch", x["batch_id"], x["prompt_id"])
                     for x in batch_items if x["state"] == "saved"]
            db.execute("""UPDATE imagen_batch_items SET state='deleted',size=0,sha256='',
                          error='',remote_job_id=NULL WHERE remote_job_id=?
                          AND batch_id IN (SELECT id FROM imagen_batches WHERE config_owner=?)""",
                       (job_id, owner))
            db.execute("DELETE FROM imagen_single_media WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM imagen_single_jobs WHERE job_id=? AND config_owner=?",
                       (job_id, owner))
            db.execute("DELETE FROM imagen_deleted_outputs WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM gpu_jobs WHERE studio='imagen' AND owner=? AND id=?",
                       (owner, job_id))
        for path in files:
            self._remove_file(path)
        directory = self.root / "single" / job_id
        if directory.is_symlink():
            raise HTTPException(409, "Cannot delete linked directory")
        if directory.is_dir():
            shutil.rmtree(directory)
        return {"deleted": True, "job_id": job_id}

    def delete_batch(self, owner: str, batch_id: str):
        """Remove pending/terminal batch and saved files; active submissions are protected."""
        self._get_batch(batch_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute("SELECT config_owner FROM imagen_batches WHERE id=?",
                               (batch_id,)).fetchone()
            if batch is None or batch["config_owner"] != owner:
                raise HTTPException(404, "Batch not found")
            items = db.execute(
                "SELECT state,remote_job_id,lease_until FROM imagen_batch_items WHERE batch_id=?",
                (batch_id,)
            ).fetchall()
            if any(
                (row["state"] not in ("pending", "saved", "failed", "deleted")
                 and not (row["state"] == "waiting_gpu" and not row["remote_job_id"]))
                or row["lease_until"] > int(time.time())
                for row in items
            ):
                raise HTTPException(409, "Batch has active GPU jobs; finish them before deleting")
            for job_id in {row["remote_job_id"] for row in items if row["remote_job_id"]}:
                db.execute("DELETE FROM imagen_deleted_outputs WHERE job_id=?", (job_id,))
                db.execute("DELETE FROM gpu_jobs WHERE studio='imagen' AND owner=? AND id=?",
                           (owner, job_id))
            db.execute("DELETE FROM imagen_batch_items WHERE batch_id=?", (batch_id,))
            db.execute("DELETE FROM imagen_batches WHERE id=?", (batch_id,))
        directory = self.root / "batches" / batch_id
        if directory.is_symlink():
            raise HTTPException(409, "Cannot delete linked directory")
        if directory.is_dir():
            shutil.rmtree(directory)
        return {"deleted": True, "batch_id": batch_id}

    def download_zip(self, batch_id, ids=None):
        self._get_batch(batch_id)
        if ids is not None:
            if not ids or len(ids) > MAX_BATCH_PROMPTS or any(
                    not PROMPT_ID.fullmatch(i) for i in ids) or len(set(ids)) != len(ids):
                raise HTTPException(422, "Invalid selected Prompt IDs")
        with self.connect() as db:
            rows = db.execute("""SELECT prompt_id,size,sha256 FROM imagen_batch_items
              WHERE batch_id=? AND state='saved' ORDER BY ordinal""", (batch_id,)).fetchall()
        selected = [row for row in rows if ids is None or row["prompt_id"] in ids]
        if not selected or (ids is not None and len(selected) != len(ids)):
            raise HTTPException(409, "Selected images are not yet saved on VPS")
        if sum(row["size"] for row in selected) > MAX_ZIP_BYTES:
            raise HTTPException(413, "ZIP exceeds 2 GB safety limit; select fewer images")
        temp = tempfile.SpooledTemporaryFile(max_size=1024*1024, mode="w+b")
        try:
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=4) as zipped:
                for row in selected:
                    pid = row["prompt_id"]
                    data = self._read_verified(self._path("batch", batch_id, pid), row["sha256"])
                    zipped.writestr(pid + ".png", data)
            temp.seek(0)
        except Exception:
            temp.close()
            raise

        def chunks():
            try:
                while True:
                    data = temp.read(1024 * 1024)
                    if not data:
                        break
                    yield data
            finally:
                temp.close()

        return StreamingResponse(
            chunks(), media_type="application/zip",
            headers={"Cache-Control": "private, no-store",
                     "X-Content-Type-Options": "nosniff",
                     "Content-Disposition": f'attachment; filename="imagen-{batch_id[:12]}.zip"'}
        )

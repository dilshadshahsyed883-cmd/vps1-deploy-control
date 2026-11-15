"""Persistent owner-isolated HeyGen upload outbox.

Uploads made while the GPU worker is offline stay private on the VPS. Automatic
retry is allowed ONLY after the worker advertises idempotent request IDs, so a
lost response cannot create duplicate videos. Does not touch Concat queues.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import HTTPException

OUTBOX_MAX_PER_OWNER = 50
MAX_IMAGE = 10 * 1024 ** 2
MAX_AUDIO = 80 * 1024 ** 2
WAIT_SECONDS = 15


class HeygenOutbox:
    def __init__(self, root: Path, connection, client_factory, *, auto_start=True):
        self.root = Path(root) / "outbox"
        if self.root.is_symlink():
            raise RuntimeError("Unsafe HeyGen outbox")
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.connection, self.client_factory = connection, client_factory
        self.inflight = set()
        self.guard = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.auto_start = auto_start
        with self.connection() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS heygen_outbox(
                owner TEXT NOT NULL, id TEXT NOT NULL, image_ext TEXT NOT NULL,
                audio_ext TEXT NOT NULL, state TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                PRIMARY KEY(owner,id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS heygen_queue(
                owner TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'running')""")
            # All in-flight uploads can be replayed idempotently after restart.
            con.execute("UPDATE heygen_outbox SET state='waiting_network' WHERE state='uploading'")
            pending = bool(con.execute("SELECT 1 FROM heygen_outbox WHERE state IN ('waiting_network','waiting_upgrade') LIMIT 1").fetchone())
        if pending and auto_start:
            self.start()

    def folder(self, owner: str, job_id: str) -> Path:
        if not owner or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("Invalid owner or job ID")
        namespace = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        user = self.root / namespace
        if user.is_symlink():
            raise ValueError("Unsafe owner directory")
        user.mkdir(mode=0o700, exist_ok=True)
        return user / job_id

    def state(self, owner: str) -> str:
        with self.connection() as con:
            row = con.execute("SELECT state FROM heygen_queue WHERE owner=?", (owner,)).fetchone()
            return row["state"] if row else "running"

    def control(self, owner: str, action: str) -> dict:
        next_state = {"start": "running", "pause": "paused", "resume": "running"}.get(action)
        if next_state is None:
            raise HTTPException(404, "Unknown queue action")
        with self.connection() as con:
            con.execute("""INSERT INTO heygen_queue(owner,state) VALUES(?,?)
                ON CONFLICT(owner) DO UPDATE SET state=excluded.state""", (owner, next_state))
        if next_state == "running":
            self.start()
        return {"state": next_state}

    @staticmethod
    def _save(upload, dest: Path, limit: int):
        if dest.is_symlink():
            raise ValueError("Unsafe upload path")
        upload.file.seek(0)
        total = 0
        with dest.open("xb") as out:
            for chunk in iter(lambda: upload.file.read(1024 * 1024), b""):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, "Upload exceeds HeyGen spool limit")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if not total:
            raise HTTPException(422, "Empty upload")

    def add(self, owner: str, image, audio, image_ext: str, audio_ext: str):
        with self.connection() as con:
            count = con.execute("SELECT COUNT(*) AS n FROM heygen_outbox "
                                "WHERE owner=? AND state NOT IN ('sent','cancelled')", (owner,)).fetchone()["n"]
        if count >= OUTBOX_MAX_PER_OWNER:
            raise HTTPException(429, "HeyGen waiting queue limit reached")
        job_id = secrets.token_hex(16)
        folder = self.folder(owner, job_id)
        folder.mkdir(mode=0o700)
        try:
            self._save(image, folder / ("image" + image_ext), MAX_IMAGE)
            self._save(audio, folder / ("audio" + audio_ext), MAX_AUDIO)
            with self.connection() as con:
                created_at = datetime.now(timezone.utc).isoformat()
                con.execute("INSERT INTO jobs(owner,id,created_at) VALUES(?,?,?)",
                            (owner, job_id, created_at))
                con.execute("""INSERT INTO heygen_outbox(owner,id,image_ext,audio_ext,state,created_at)
                    VALUES(?,?,?,?,'waiting_network',?)""",
                    (owner, job_id, image_ext, audio_ext, created_at))
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        if self.auto_start:
            self.start()
        return {"id": job_id, "status": "waiting_network"}

    def snapshot(self, owner: str) -> dict:
        with self.connection() as con:
            rows = con.execute("SELECT id,state,error FROM heygen_outbox WHERE owner=?", (owner,)).fetchall()
        return {row["id"]: {"state": row["state"], "error": row["error"]} for row in rows}

    def action(self, owner: str, job_id: str, action: str):
        folder = self.folder(owner, job_id)
        # Serialize with GPU dispatch so cancel/delete cannot overtake upload.
        if not folder.exists():
            with self.connection() as con:
                row = con.execute("SELECT state FROM heygen_outbox WHERE owner=? AND id=?",
                                  (owner, job_id)).fetchone()
            if not row or row["state"] == "sent":
                return None
            raise HTTPException(409, "Missing queued media")
        with (folder / ".dispatch.lock").open("a+b") as locked:
            fcntl.flock(locked, fcntl.LOCK_EX)
            with self.connection() as con:
                row = con.execute("SELECT state FROM heygen_outbox WHERE owner=? AND id=?",
                                  (owner, job_id)).fetchone()
                if not row or row["state"] == "sent":
                    return None
                if row["state"] == "uploading":
                    raise HTTPException(409, "Upload in progress; retry after status changes")
                if action == "cancel" and row["state"] != "cancelled":
                    con.execute("UPDATE heygen_outbox SET state='cancelled',error='' "
                                "WHERE owner=? AND id=?", (owner, job_id))
                    return {"id": job_id, "status": "cancelled"}
                if action == "retry" and row["state"] in {"cancelled", "waiting_network", "waiting_upgrade"}:
                    con.execute("UPDATE heygen_outbox SET state='waiting_network',error='' "
                                "WHERE owner=? AND id=?", (owner, job_id))
                    self.start()
                    return {"id": job_id, "status": "waiting_network"}
                if action == "delete" and row["state"] in {"cancelled", "waiting_network", "waiting_upgrade"}:
                    con.execute("DELETE FROM heygen_outbox WHERE owner=? AND id=?", (owner, job_id))
                    con.execute("DELETE FROM jobs WHERE owner=? AND id=?", (owner, job_id))
                    # Remote was never positively acknowledged; any ambiguous
                    # upload can only have occurred with a retry-safe ID.
                    shutil.rmtree(folder, ignore_errors=True)
                    return {"deleted": True}
        raise HTTPException(409, "Action unavailable for pending HeyGen job")

    def forget(self, owner: str, job_id: str):
        folder = self.folder(owner, job_id)
        if folder.exists():
            shutil.rmtree(folder)
        with self.connection() as con:
            con.execute("DELETE FROM heygen_outbox WHERE owner=? AND id=?", (owner, job_id))

    def start(self):
        if not self.auto_start:
            return
        with self.guard:
            if self.thread is not None and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._loop, name="heygen-outbox-watch", daemon=True)
            self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                with self.connection() as con:
                    rows = con.execute("SELECT owner,id FROM heygen_outbox "
                                       "WHERE state IN ('waiting_network','waiting_upgrade') ORDER BY created_at LIMIT 50").fetchall()
                for row in rows:
                    if self.state(row["owner"]) == "running":
                        self.dispatch(row["owner"], row["id"])
            except (OSError, sqlite3.Error):
                pass
            self.stop_event.wait(WAIT_SECONDS)

    def dispatch(self, owner: str, job_id: str):
        if self.state(owner) != "running":
            return
        key = (owner, job_id)
        with self.guard:
            if key in self.inflight:
                return
            self.inflight.add(key)
        try:
            self._dispatch(owner, job_id)
        finally:
            with self.guard:
                self.inflight.discard(key)

    def _dispatch(self, owner: str, job_id: str):
        folder = self.folder(owner, job_id)
        if not folder.exists() or folder.is_symlink():
            return
        lockpath = folder / ".dispatch.lock"
        with lockpath.open("a+b") as locked:
            fcntl.flock(locked, fcntl.LOCK_EX)
            with self.connection() as con:
                row = con.execute("SELECT * FROM heygen_outbox WHERE owner=? AND id=?",
                                  (owner, job_id)).fetchone()
                if not row or row["state"] not in {"waiting_network", "waiting_upgrade", "uploading"}:
                    return
            try:
                with self.client_factory(owner, timeout=600) as client:
                    health = client.get("api/v1/engine", timeout=15)
                    health.raise_for_status()
                    engine = health.json()
                    if not engine.get("supports_idempotent_submit"):
                        with self.connection() as con:
                            con.execute("UPDATE heygen_outbox SET state='waiting_upgrade',"
                                        "error='Deploy HeyGen worker with retry-safe uploads' "
                                        "WHERE owner=? AND id=?", (owner, job_id))
                        return
                    if not (engine.get("entrypoint_found") and engine.get("models_ready")):
                        return
                    with self.connection() as con:
                        con.execute("UPDATE heygen_outbox SET state='uploading',error='' "
                                    "WHERE owner=? AND id=?", (owner, job_id))
                    image = folder / ("image" + row["image_ext"])
                    audio = folder / ("audio" + row["audio_ext"])
                    with image.open("rb") as photo, audio.open("rb") as speech:
                        response = client.post("api/v1/jobs",
                            headers={"X-Heygen-Request-ID": job_id},
                            files={
                                "image": (image.name, photo, "application/octet-stream"),
                                "audio": (audio.name, speech, "application/octet-stream"),
                            }, timeout=600)
                    response.raise_for_status()
                    if response.json().get("id") != job_id:
                        raise ValueError("GPU worker did not honor idempotent request ID")
                with self.connection() as con:
                    con.execute("UPDATE heygen_outbox SET state='sent',error='' "
                                "WHERE owner=? AND id=?", (owner, job_id))
                # Safe because a replay of this ID resolves to exactly one GPU job.
                for name in ("image" + row["image_ext"], "audio" + row["audio_ext"]):
                    (folder / name).unlink(missing_ok=True)
            except (HTTPException, httpx.HTTPError, OSError, ValueError, KeyError):
                with self.connection() as con:
                    con.execute("UPDATE heygen_outbox SET state='waiting_network',"
                                "error='GPU unavailable; retrying safely' "
                                "WHERE owner=? AND id=? AND state='uploading'", (owner, job_id))

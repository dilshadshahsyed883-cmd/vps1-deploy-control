"""Owner-reviewed face similarity search for SML video clips.

Uses the existing on-device OpenCV models and the same people identities as
image Face Recognition. Samples a bounded number of frames from each clip.
Never changes video files, contacts external recognition APIs or assigns an
identity without explicit human approval.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

from face_people import ENGINE_VERSION, MIN_COSINE, _engine, _inference_lock

VIDEO_ENGINE = ENGINE_VERSION + "-video-frames-v1"
_video_scan_lock = threading.Lock()
_video_index_lock = threading.RLock()


class VideoPerson(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    clip_id: str = Field(min_length=1, max_length=128)
    face_index: int = Field(ge=0, le=319)


class VideoBatch(BaseModel):
    clip_ids: list[str] = Field(min_length=1, max_length=500)


def _extract_video_faces(root: Path, path: Path, duration: float) -> list[dict]:
    """CPU-only, bounded sampling; timestamps are relative to the clip."""
    cv2, np, _Image, _ImageOps, detector, recognizer = _engine(root)
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise ValueError("Video cannot be decoded")
    output = []
    try:
        measured = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if (duration <= 0 or not math.isfinite(duration)) and 1 <= measured <= 240 and count > 0:
            duration = min(count / measured, 86400)
        if not 0 < duration <= 86400:
            raise ValueError("Video duration unavailable")
        samples = min(8, max(3, math.ceil(duration)))
        for frame_index in range(samples):
            at = min(max(0.0, duration - 0.05), (frame_index + 0.5) * duration / samples)
            capture.set(cv2.CAP_PROP_POS_MSEC, at * 1000)
            success, pixels = capture.read()
            if not success or pixels is None:
                continue
            height, width = pixels.shape[:2]
            if min(height, width) < 32:
                continue
            scale = min(1.0, 960 / max(height, width))
            if scale < 1:
                pixels = cv2.resize(pixels, (max(32, int(width * scale)), max(32, int(height * scale))))
            height, width = pixels.shape[:2]
            with _inference_lock:
                detector.setInputSize((width, height))
                _result, detected = detector.detect(pixels)
                for face in ([] if detected is None else detected[:40]):
                    x, y, w, h = [float(value) for value in face[:4]]
                    if w < 24 or h < 24:
                        continue
                    aligned = recognizer.alignCrop(pixels, face)
                    vector = recognizer.feature(aligned).reshape(-1).astype("float32")
                    magnitude = float(np.linalg.norm(vector))
                    if magnitude < 1e-8:
                        continue
                    vector /= magnitude
                    output.append({
                        "face_index": len(output),
                        "frame_index": frame_index,
                        "time_seconds": round(at, 3),
                        "bbox": [max(0., min(1., x / width)), max(0., min(1., y / height)),
                                 max(0., min(1., w / width)), max(0., min(1., h / height))],
                        "embedding": vector.astype(float).tolist(),
                    })
    finally:
        capture.release()
    return output


def register_video_face_people(app, *, data_dir, db, now_iso):
    root = Path(data_dir)
    video_root = (root / "clips").resolve()

    @app.on_event("startup")
    def initialize_video_faces():
        with db() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(people)")}
            if "reference_media_type" not in columns:
                conn.execute("ALTER TABLE people ADD COLUMN reference_media_type TEXT NOT NULL DEFAULT 'image'")
            if "reference_clip_id" not in columns:
                conn.execute("ALTER TABLE people ADD COLUMN reference_clip_id TEXT")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS clip_face_scans (
                  clip_id TEXT PRIMARY KEY, clip_sha TEXT NOT NULL,
                  engine TEXT NOT NULL, scanned_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clip_face_detections (
                  clip_id TEXT NOT NULL, face_index INTEGER NOT NULL,
                  frame_index INTEGER NOT NULL, time_seconds REAL NOT NULL,
                  bbox_json TEXT NOT NULL, embedding_json TEXT NOT NULL,
                  PRIMARY KEY(clip_id, face_index)
                );
                CREATE TABLE IF NOT EXISTS person_video_matches (
                  person_id TEXT NOT NULL, clip_id TEXT NOT NULL,
                  face_index INTEGER NOT NULL, time_seconds REAL NOT NULL,
                  similarity REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                  added_keyword INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                  PRIMARY KEY(person_id, clip_id)
                );
                CREATE TABLE IF NOT EXISTS person_video_scans (
                  person_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                  scanned INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                  error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_person_video_matches_state
                  ON person_video_matches(person_id, state);
            """)
            conn.execute("UPDATE person_video_scans SET status='interrupted', "
                         "error='Scan interrupted by restart' WHERE status='running'")

    def clip_row(conn, clip_id):
        item = conn.execute(
            "SELECT id,filename,thumb_filename,sha256,duration,deleted_at FROM clips WHERE id=?",
            (clip_id,),
        ).fetchone()
        if not item or item["deleted_at"]:
            raise HTTPException(404, "Video clip not found")
        return item

    def clip_faces(clip_id: str) -> list[dict]:
        # Serialize cache writes: concurrent reference selection and library
        # scanning must not clear one another's face rows.
        with _video_index_lock:
            with db() as conn:
                clip = clip_row(conn, clip_id)
                path = (video_root / clip["filename"]).resolve()
                if video_root not in path.parents or not path.is_file():
                    raise HTTPException(404, "Video content missing")
                st = path.stat()
                signature = clip["sha256"] or f"{st.st_size}:{st.st_mtime_ns}"
                prior = conn.execute(
                    "SELECT clip_sha,engine FROM clip_face_scans WHERE clip_id=?", (clip_id,)
                ).fetchone()
                if prior and prior["clip_sha"] == signature and prior["engine"] == VIDEO_ENGINE:
                    return [{
                        "face_index": row["face_index"],
                        "frame_index": row["frame_index"],
                        "time_seconds": row["time_seconds"],
                        "bbox": json.loads(row["bbox_json"]),
                        "embedding": json.loads(row["embedding_json"]),
                    } for row in conn.execute(
                        "SELECT * FROM clip_face_detections WHERE clip_id=? ORDER BY face_index",
                        (clip_id,),
                    )]
            try:
                faces = _extract_video_faces(root, path, float(clip["duration"]))
            except (OSError, ValueError) as exc:
                raise HTTPException(422, "Could not decode video for face scanning") from exc
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc
            with db() as conn:
                current = clip_row(conn, clip_id)
                st2 = path.stat() if path.is_file() else None
                signature2 = (current["sha256"] or f"{st2.st_size}:{st2.st_mtime_ns}") if st2 else ""
                if signature2 != signature:
                    raise HTTPException(409, "Video changed during scan")
                conn.execute("DELETE FROM clip_face_detections WHERE clip_id=?", (clip_id,))
                conn.executemany(
                    "INSERT INTO clip_face_detections "
                    "(clip_id,face_index,frame_index,time_seconds,bbox_json,embedding_json) "
                    "VALUES (?,?,?,?,?,?)",
                    [(clip_id, face["face_index"], face["frame_index"], face["time_seconds"],
                      json.dumps(face["bbox"]), json.dumps(face["embedding"])) for face in faces],
                )
                conn.execute(
                    "INSERT INTO clip_face_scans(clip_id,clip_sha,engine,scanned_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(clip_id) DO UPDATE SET clip_sha=excluded.clip_sha,"
                    "engine=excluded.engine,scanned_at=excluded.scanned_at",
                    (clip_id, signature, VIDEO_ENGINE, now_iso()),
                )
            return faces

    @app.get("/api/v1/faces/clips/{clip_id}")
    def inspect_clip(clip_id: str):
        faces = clip_faces(clip_id)
        return {"clip_id": clip_id, "faces": [
            {key: face[key] for key in ("face_index", "frame_index", "time_seconds", "bbox")}
            for face in faces
        ]}

    @app.post("/api/v1/people/video-reference")
    def create_video_person(body: VideoPerson):
        name = " ".join(body.name.split()).strip()
        if not name or len(name) > 100:
            raise HTTPException(422, "Enter a person name")
        faces = clip_faces(body.clip_id)
        chosen = next((f for f in faces if f["face_index"] == body.face_index), None)
        if chosen is None:
            raise HTTPException(422, "Select a detected reference face")
        person_id = uuid.uuid4().hex
        with db() as conn:
            clip_row(conn, body.clip_id)
            conn.execute(
                "INSERT INTO people "
                "(id,name,reference_image_id,reference_face_index,reference_embedding,"
                "created_at,reference_media_type,reference_clip_id) VALUES (?,?,?,?,?,?,?,?)",
                (person_id, name, "", body.face_index,
                 json.dumps(chosen["embedding"]), now_iso(), "video", body.clip_id),
            )
        return {"id": person_id, "name": name, "reference_clip_id": body.clip_id}

    def run_scan(person_id):
        try:
            with db() as conn:
                person = conn.execute(
                    "SELECT reference_embedding FROM people WHERE id=?", (person_id,)
                ).fetchone()
                if not person:
                    return
                clips = [row["id"] for row in conn.execute(
                    "SELECT id FROM clips WHERE deleted_at IS NULL ORDER BY id"
                )]
                conn.execute(
                    "UPDATE person_video_scans SET status='running',scanned=0,total=?,error='',"
                    "updated_at=? WHERE person_id=?", (len(clips), now_iso(), person_id)
                )
                reference = json.loads(person["reference_embedding"])
            import numpy as np
            vector = np.asarray(reference, dtype=np.float32)
            for index, clip_id in enumerate(clips, 1):
                try:
                    faces = clip_faces(clip_id)
                except HTTPException as exc:
                    if exc.status_code in (404, 409, 422):
                        faces = []
                    else:
                        raise
                best = None
                for face in faces:
                    score = float(np.dot(vector, np.asarray(face["embedding"], dtype=np.float32)))
                    if score >= MIN_COSINE and (best is None or score > best[0]):
                        best = (score, face)
                if best:
                    score, face = best
                    with db() as conn:
                        # Rejected and approved decisions are never reset by rescans.
                        conn.execute(
                            "INSERT INTO person_video_matches "
                            "(person_id,clip_id,face_index,time_seconds,similarity,state,updated_at) "
                            "VALUES (?,?,?,?,?,'pending',?) "
                            "ON CONFLICT(person_id,clip_id) DO UPDATE SET "
                            "face_index=excluded.face_index,time_seconds=excluded.time_seconds,"
                            "similarity=excluded.similarity,updated_at=excluded.updated_at "
                            "WHERE person_video_matches.state='pending'",
                            (person_id, clip_id, face["face_index"], face["time_seconds"],
                             round(score, 5), now_iso()),
                        )
                if index % 10 == 0 or index == len(clips):
                    with db() as conn:
                        conn.execute("UPDATE person_video_scans SET scanned=?,updated_at=? "
                                     "WHERE person_id=?", (index, now_iso(), person_id))
            with db() as conn:
                conn.execute("UPDATE person_video_scans SET status='completed',scanned=total,"
                             "updated_at=? WHERE person_id=?", (now_iso(), person_id))
        except Exception as exc:
            with db() as conn:
                conn.execute("UPDATE person_video_scans SET status='failed',error=?,"
                             "updated_at=? WHERE person_id=?", (str(exc)[:500], now_iso(), person_id))
        finally:
            _video_scan_lock.release()

    @app.post("/api/v1/people/{person_id}/video-scan")
    def start_video_scan(person_id: str):
        with db() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise HTTPException(404, "Person not found")
        if not _video_scan_lock.acquire(blocking=False):
            raise HTTPException(409, "Another video face scan is running")
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO person_video_scans(person_id,status,updated_at) VALUES (?,'queued',?) "
                    "ON CONFLICT(person_id) DO UPDATE SET status='queued',scanned=0,"
                    "total=0,error='',updated_at=excluded.updated_at",
                    (person_id, now_iso()),
                )
            threading.Thread(target=run_scan, args=(person_id,), daemon=True).start()
        except Exception:
            _video_scan_lock.release()
            raise
        return {"queued": True, "person_id": person_id}

    @app.get("/api/v1/people/{person_id}/video-scan")
    def video_scan_status(person_id: str):
        with db() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise HTTPException(404, "Person not found")
            state = conn.execute(
                "SELECT * FROM person_video_scans WHERE person_id=?", (person_id,)
            ).fetchone()
        return dict(state) if state else {
            "person_id": person_id, "status": "not_started", "scanned": 0, "total": 0
        }

    @app.get("/api/v1/people/{person_id}/video-matches")
    def video_matches(person_id: str, state: str = "pending"):
        if state not in ("pending", "approved", "rejected", "all"):
            raise HTTPException(400, "Invalid match state")
        with db() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise HTTPException(404, "Person not found")
            filter_sql = "" if state == "all" else " AND m.state=?"
            params = (person_id,) if state == "all" else (person_id, state)
            rows = conn.execute(
                "SELECT m.clip_id,m.face_index,m.time_seconds,m.similarity,m.state,"
                "c.thumb_filename,c.filename FROM person_video_matches m "
                "JOIN clips c ON c.id=m.clip_id WHERE m.person_id=? AND c.deleted_at IS NULL"
                + filter_sql + " ORDER BY m.similarity DESC LIMIT 2000",
                params,
            ).fetchall()
        return {"items": [
            {"clip_id": row["clip_id"], "face_index": row["face_index"],
             "time_seconds": row["time_seconds"], "similarity": row["similarity"],
             "state": row["state"],
             "thumb_url": "/media/thumbs/" + row["thumb_filename"] if row["thumb_filename"] else None}
            for row in rows
        ]}

    def decide(person_id, body, action):
        ids = list(dict.fromkeys(body.clip_ids))
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            person = conn.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
            if not person:
                raise HTTPException(404, "Person not found")
            for clip_id in ids:
                clip_row(conn, clip_id)
                if not conn.execute(
                    "SELECT 1 FROM person_video_matches WHERE person_id=? AND clip_id=?",
                    (person_id, clip_id),
                ).fetchone():
                    raise HTTPException(409, "Clip is not a match candidate: " + clip_id)
            for clip_id in ids:
                existing = conn.execute(
                    "SELECT state,added_keyword FROM person_video_matches "
                    "WHERE person_id=? AND clip_id=?", (person_id, clip_id)
                ).fetchone()
                if action == "approve":
                    preexisting = conn.execute(
                        "SELECT 1 FROM clip_keywords WHERE clip_id=? AND keyword=? COLLATE NOCASE",
                        (clip_id, person["name"]),
                    ).fetchone()
                    if not preexisting:
                        conn.execute(
                            "INSERT INTO clip_keywords(clip_id,keyword,source,confidence,created_at) "
                            "VALUES (?,?,'face-review',1.0,?)",
                            (clip_id, person["name"], now_iso()),
                        )
                    added = int(bool(existing["added_keyword"] or not preexisting))
                    state = "approved"
                else:
                    if existing["added_keyword"]:
                        # Never delete tags which predated this approval.
                        conn.execute(
                            "DELETE FROM clip_keywords WHERE clip_id=? AND keyword=? "
                            "COLLATE NOCASE AND source='face-review'",
                            (clip_id, person["name"]),
                        )
                    added = 0
                    state = "rejected" if action == "reject" else "pending"
                conn.execute(
                    "UPDATE person_video_matches SET state=?,added_keyword=?,updated_at=? "
                    "WHERE person_id=? AND clip_id=?",
                    (state, added, now_iso(), person_id, clip_id),
                )
                conn.execute("UPDATE clips SET updated_at=? WHERE id=?", (now_iso(), clip_id))
        return {"updated": len(ids), "state": action}

    @app.post("/api/v1/people/{person_id}/video-approve")
    def approve_videos(person_id: str, body: VideoBatch):
        return decide(person_id, body, "approve")

    @app.post("/api/v1/people/{person_id}/video-reject")
    def reject_videos(person_id: str, body: VideoBatch):
        return decide(person_id, body, "reject")

    @app.post("/api/v1/people/{person_id}/video-undo")
    def undo_videos(person_id: str, body: VideoBatch):
        return decide(person_id, body, "undo")

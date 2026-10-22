"""Opt-in, approval-gated people tagging for the local SML image library.

Detection and embeddings run on VPS CPU. Only owner-named people are searchable.
Biometric vectors, reference choices and decisions remain under SML_DATA_ROOT.
Never send owner media to a third-party API or alter original image filenames.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

from fastapi import HTTPException
from pydantic import BaseModel, Field

# Official OpenCV Zoo models, sha256 pinned; downloaded on first explicit use.
# The upstream files are model artifacts, not code. Never execute downloads.
MODEL_REV = "d4938dfc9d4ec5d098bfa33e98b3f3345a236586"
MODEL_BASE = "https://huggingface.co/opencv/opencv_zoo/resolve/" + MODEL_REV + "/models/"
MODELS = {
    "detector": (
        "face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        MODEL_BASE + "face_detection_yunet/face_detection_yunet_2023mar.onnx",
    ),
    "recognizer": (
        "face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        MODEL_BASE + "face_recognition_sface/face_recognition_sface_2021dec.onnx",
    ),
}
ENGINE_VERSION = "opencv-yunet-sface-20261022-v1"
# Calibrated scores are NOT probabilities. Higher is more similar.
MIN_COSINE = 0.47
HIGH_COSINE = 0.60
DOWNLOAD_MAX = 50 * 1024 * 1024
_model_lock = threading.RLock()
_inference_lock = threading.RLock()
_scan_lock = threading.Lock()
_engine_instance = None


class CreatePerson(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    image_id: str = Field(min_length=1, max_length=128)
    face_index: int = Field(ge=0, le=40)


class MatchBatch(BaseModel):
    image_ids: list[str] = Field(min_length=1, max_length=500)


def _verified_model(folder: Path, key: str) -> Path:
    name, expected, url = MODELS[key]
    target = folder / name
    if target.is_file() and _sha256(target) == expected:
        return target
    target.unlink(missing_ok=True)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = folder / (name + "." + uuid.uuid4().hex + ".part")
    try:
        request = Request(url, headers={"User-Agent": "SML-Face-Models/1.0"})
        count = 0
        with urlopen(request, timeout=60) as response, stage.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > DOWNLOAD_MAX:
                    raise RuntimeError("Face model download exceeds safe size")
                output.write(chunk)
        if _sha256(stage) != expected:
            raise RuntimeError("Face model checksum mismatch")
        stage.replace(target)
        return target
    finally:
        stage.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _engine(root: Path):
    global _engine_instance
    with _model_lock:
        if _engine_instance is not None:
            return _engine_instance
        try:
            import cv2
            import numpy as np
            from PIL import Image, ImageOps
        except ImportError as exc:
            raise RuntimeError("Face recognition requires opencv-python-headless, Pillow and NumPy") from exc
        models = root / "face_models"
        detector_path = _verified_model(models, "detector")
        recognizer_path = _verified_model(models, "recognizer")
        detector = cv2.FaceDetectorYN.create(str(detector_path), "", (320, 320), 0.82, 0.3, 100)
        recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), "")
        _engine_instance = (cv2, np, Image, ImageOps, detector, recognizer)
        return _engine_instance


def _extract_faces(root: Path, path: Path) -> list[dict]:
    cv2, np, Image, ImageOps, detector, recognizer = _engine(root)
    with Image.open(path) as source:
        source = ImageOps.exif_transpose(source)
        if source.width * source.height > 40_000_000:
            raise ValueError("Image too large for face scanning")
        source.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        pixels = cv2.cvtColor(np.asarray(source.convert("RGB")), cv2.COLOR_RGB2BGR)
    height, width = pixels.shape[:2]
    if min(height, width) < 32:
        return []
    output = []
    with _inference_lock:
        detector.setInputSize((width, height))
        _, detections = detector.detect(pixels)
        for face in ([] if detections is None else detections[:40]):
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
                "bbox": [
                    max(0.0, min(1.0, x / width)),
                    max(0.0, min(1.0, y / height)),
                    max(0.0, min(1.0, w / width)),
                    max(0.0, min(1.0, h / height)),
                ],
                "embedding": vector.astype(float).tolist(),
            })
    return output


def register_face_people(app, *, data_dir, db, now_iso):
    root = Path(data_dir)
    image_root = root / "images"

    @app.on_event("startup")
    def initialize_face_people():
        with db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS people (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL,
                  reference_image_id TEXT NOT NULL, reference_face_index INTEGER NOT NULL,
                  reference_embedding TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS image_face_scans (
                  image_id TEXT PRIMARY KEY, image_sha TEXT NOT NULL,
                  engine TEXT NOT NULL, scanned_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS image_faces (
                  image_id TEXT NOT NULL, face_index INTEGER NOT NULL,
                  bbox_json TEXT NOT NULL, embedding_json TEXT NOT NULL,
                  PRIMARY KEY(image_id, face_index)
                );
                CREATE TABLE IF NOT EXISTS people_matches (
                  person_id TEXT NOT NULL, image_id TEXT NOT NULL,
                  face_index INTEGER NOT NULL, similarity REAL NOT NULL,
                  state TEXT NOT NULL DEFAULT 'pending',
                  added_keyword INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(person_id,image_id,face_index)
                );
                CREATE TABLE IF NOT EXISTS people_scans (
                  person_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                  scanned INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                  error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_people_matches_status
                  ON people_matches(person_id,state);
            """)
            # A service restart never presents a partially finished scan as done.
            conn.execute("UPDATE people_scans SET status='interrupted',error='Scan interrupted by restart' WHERE status='running'")

    def _image_row(conn, ident):
        row = conn.execute(
            "SELECT id,stored_name,sha256,deleted_at FROM image_assets WHERE id=?",
            (ident,),
        ).fetchone()
        if not row or row["deleted_at"]:
            raise HTTPException(404, "Image not found")
        return row

    def _image_faces(ident: str) -> list[dict]:
        with db() as conn:
            row = _image_row(conn, ident)
            recorded = conn.execute(
                "SELECT image_sha,engine FROM image_face_scans WHERE image_id=?", (ident,)
            ).fetchone()
            if recorded and recorded["image_sha"] == row["sha256"] and recorded["engine"] == ENGINE_VERSION:
                return [
                    {"face_index": face["face_index"],
                     "bbox": json.loads(face["bbox_json"]),
                     "embedding": json.loads(face["embedding_json"])}
                    for face in conn.execute(
                        "SELECT * FROM image_faces WHERE image_id=? ORDER BY face_index", (ident,)
                    )
                ]
            # The stored filename is generated by SML, but still validate containment.
            image_path = (image_root / row["stored_name"]).resolve()
            if image_root.resolve() not in image_path.parents or not image_path.is_file():
                raise HTTPException(404, "Image content missing")
        try:
            faces = _extract_faces(root, image_path)
        except (OSError, ValueError) as exc:
            raise HTTPException(422, "Could not decode image for face scanning") from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        with db() as conn:
            current = _image_row(conn, ident)
            if current["sha256"] != row["sha256"]:
                raise HTTPException(409, "Image changed during face scan")
            conn.execute("DELETE FROM image_faces WHERE image_id=?", (ident,))
            conn.executemany(
                "INSERT INTO image_faces(image_id,face_index,bbox_json,embedding_json) VALUES (?,?,?,?)",
                [(ident, f["face_index"], json.dumps(f["bbox"]), json.dumps(f["embedding"])) for f in faces]
            )
            conn.execute(
                "INSERT INTO image_face_scans(image_id,image_sha,engine,scanned_at) VALUES (?,?,?,?) "
                "ON CONFLICT(image_id) DO UPDATE SET image_sha=excluded.image_sha,engine=excluded.engine,scanned_at=excluded.scanned_at",
                (ident, row["sha256"], ENGINE_VERSION, now_iso())
            )
        return faces

    @app.get("/api/v1/faces/images/{image_id}")
    def inspect_faces(image_id: str):
        faces = _image_faces(image_id)
        return {"image_id": image_id, "faces": [
            {"face_index": f["face_index"], "bbox": f["bbox"]} for f in faces
        ]}

    @app.post("/api/v1/people")
    def create_person(body: CreatePerson):
        name = " ".join(body.name.split()).strip()
        if not name or len(name) > 100:
            raise HTTPException(400, "Enter a person name (1-100 characters)")
        faces = _image_faces(body.image_id)
        if body.face_index >= len(faces):
            raise HTTPException(422, "Select a detected face in the reference image")
        ident = uuid.uuid4().hex
        with db() as conn:
            _image_row(conn, body.image_id)
            conn.execute(
                "INSERT INTO people(id,name,reference_image_id,reference_face_index,reference_embedding,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (ident, name, body.image_id, body.face_index,
                 json.dumps(faces[body.face_index]["embedding"]), now_iso())
            )
        return {"id": ident, "name": name, "reference_image_id": body.image_id}

    @app.get("/api/v1/people")
    def list_people():
        with db() as conn:
            rows = conn.execute("""
                SELECT p.id,p.name,p.reference_image_id,p.reference_face_index,p.created_at,
                  (SELECT COUNT(DISTINCT m.image_id) FROM people_matches m
                   WHERE m.person_id=p.id AND m.state='approved') AS approved,
                  (SELECT COUNT(DISTINCT m.image_id) FROM people_matches m
                   WHERE m.person_id=p.id AND m.state='pending') AS pending
                FROM people p ORDER BY p.created_at DESC
            """).fetchall()
        return {"items": [dict(row) for row in rows]}

    def _scan(person_id: str):
        try:
            with db() as conn:
                person = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
                if not person:
                    return
                refs = [
                    (row["id"], row["sha256"])
                    for row in conn.execute(
                        "SELECT id,sha256 FROM image_assets WHERE deleted_at IS NULL ORDER BY id"
                    )
                ]
                conn.execute(
                    "UPDATE people_scans SET status='running',scanned=0,total=?,error='',updated_at=? WHERE person_id=?",
                    (len(refs), now_iso(), person_id)
                )
                reference = json.loads(person["reference_embedding"])
            import numpy as np
            vector = np.asarray(reference, dtype=np.float32)
            for index, (image_id, image_sha) in enumerate(refs, 1):
                try:
                    faces = _image_faces(image_id)
                except HTTPException as exc:
                    # One corrupt/missing media file must not stop all images.
                    if exc.status_code in (404, 409, 422):
                        faces = []
                    else:
                        raise
                for face in faces:
                    score = float(np.dot(vector, np.asarray(face["embedding"], dtype=np.float32)))
                    if score < MIN_COSINE:
                        continue
                    with db() as conn:
                        # Never overwrite the user's prior rejection or approval.
                        conn.execute("""
                            INSERT OR IGNORE INTO people_matches
                              (person_id,image_id,face_index,similarity,state,updated_at)
                            VALUES (?,?,?,?,'pending',?)
                        """, (person_id, image_id, face["face_index"], round(score, 5), now_iso()))
                if index % 10 == 0 or index == len(refs):
                    with db() as conn:
                        conn.execute(
                            "UPDATE people_scans SET scanned=?,updated_at=? WHERE person_id=?",
                            (index, now_iso(), person_id)
                        )
            with db() as conn:
                conn.execute(
                    "UPDATE people_scans SET status='completed',scanned=total,updated_at=? WHERE person_id=?",
                    (now_iso(), person_id)
                )
        except Exception as exc:
            with db() as conn:
                conn.execute(
                    "UPDATE people_scans SET status='failed',error=?,updated_at=? WHERE person_id=?",
                    (str(exc)[:500], now_iso(), person_id)
                )
        finally:
            _scan_lock.release()

    @app.post("/api/v1/people/{person_id}/scan")
    def start_scan(person_id: str):
        with db() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise HTTPException(404, "Person not found")
        if not _scan_lock.acquire(blocking=False):
            raise HTTPException(409, "Another person scan is currently running")
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO people_scans(person_id,status,updated_at) VALUES (?,'queued',?) "
                    "ON CONFLICT(person_id) DO UPDATE SET status='queued',scanned=0,total=0,error='',updated_at=excluded.updated_at",
                    (person_id, now_iso())
                )
            threading.Thread(target=_scan, args=(person_id,), daemon=True).start()
        except Exception:
            _scan_lock.release()
            raise
        return {"queued": True, "person_id": person_id}

    @app.get("/api/v1/people/{person_id}/scan")
    def scan_status(person_id: str):
        with db() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise HTTPException(404, "Person not found")
            row = conn.execute("SELECT * FROM people_scans WHERE person_id=?", (person_id,)).fetchone()
        return dict(row) if row else {"person_id": person_id, "status": "not_started", "scanned": 0, "total": 0}

    @app.get("/api/v1/people/{person_id}/matches")
    def person_matches(person_id: str, state: str = "pending"):
        if state not in ("pending", "approved", "rejected", "all"):
            raise HTTPException(400, "Invalid match state")
        with db() as conn:
            if not conn.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise HTTPException(404, "Person not found")
            where = "" if state == "all" else " AND m.state=?"
            params = (person_id,) if state == "all" else (person_id, state)
            rows = conn.execute("""
                SELECT m.image_id,m.face_index,m.similarity,m.state,m.added_keyword,
                       i.original_name
                FROM people_matches m JOIN image_assets i ON i.id=m.image_id
                WHERE m.person_id=? AND i.deleted_at IS NULL
            """ + where + " ORDER BY m.similarity DESC LIMIT 2000", params).fetchall()
        return {"items": [
            {**dict(row), "thumb_url": "/api/v1/images/" + row["image_id"] + "/preview"}
            for row in rows
        ]}

    def _decide(person_id: str, body: MatchBatch, action: str):
        ids = list(dict.fromkeys(body.image_ids))
        with db() as conn:
            person = conn.execute("SELECT id,name FROM people WHERE id=?", (person_id,)).fetchone()
            if not person:
                raise HTTPException(404, "Person not found")
            # Validate entire batch before making any changes.
            for ident in ids:
                _image_row(conn, ident)
                if not conn.execute(
                    "SELECT 1 FROM people_matches WHERE person_id=? AND image_id=?",
                    (person_id, ident)
                ).fetchone():
                    raise HTTPException(409, "Image is not a match candidate: " + ident)
            for ident in ids:
                rows = conn.execute(
                    "SELECT state,added_keyword FROM people_matches WHERE person_id=? AND image_id=?",
                    (person_id, ident)
                ).fetchall()
                image = conn.execute(
                    "SELECT keywords_json FROM image_assets WHERE id=?", (ident,)
                ).fetchone()
                keywords = json.loads(image["keywords_json"])
                has_tag = any(x.casefold() == person["name"].casefold() for x in keywords)
                if action == "approve":
                    inserted = not has_tag
                    if inserted:
                        keywords.append(person["name"])
                    conn.execute(
                        "UPDATE people_matches SET state='approved',added_keyword=?,updated_at=? "
                        "WHERE person_id=? AND image_id=?",
                        (int(inserted or any(r["added_keyword"] for r in rows)), now_iso(), person_id, ident)
                    )
                elif action == "reject":
                    # Undo previously approved tags only if this feature inserted them.
                    was_ours = any(r["state"] == "approved" and r["added_keyword"] for r in rows)
                    if was_ours:
                        keywords = [x for x in keywords if x.casefold() != person["name"].casefold()]
                    conn.execute(
                        "UPDATE people_matches SET state='rejected',added_keyword=0,updated_at=? "
                        "WHERE person_id=? AND image_id=?",
                        (now_iso(), person_id, ident)
                    )
                else:
                    # Undo returns approved/rejected decisions to a pending queue.
                    was_ours = any(r["state"] == "approved" and r["added_keyword"] for r in rows)
                    if was_ours:
                        keywords = [x for x in keywords if x.casefold() != person["name"].casefold()]
                    conn.execute(
                        "UPDATE people_matches SET state='pending',added_keyword=0,updated_at=? "
                        "WHERE person_id=? AND image_id=?",
                        (now_iso(), person_id, ident)
                    )
                conn.execute(
                    "UPDATE image_assets SET keywords_json=?,updated_at=? WHERE id=?",
                    (json.dumps(keywords, ensure_ascii=False), now_iso(), ident)
                )
        return {"updated": len(ids), "state": action}

    @app.post("/api/v1/people/{person_id}/approve")
    def approve_matches(person_id: str, body: MatchBatch):
        return _decide(person_id, body, "approve")

    @app.post("/api/v1/people/{person_id}/reject")
    def reject_matches(person_id: str, body: MatchBatch):
        return _decide(person_id, body, "reject")

    @app.post("/api/v1/people/{person_id}/undo")
    def undo_matches(person_id: str, body: MatchBatch):
        return _decide(person_id, body, "undo")

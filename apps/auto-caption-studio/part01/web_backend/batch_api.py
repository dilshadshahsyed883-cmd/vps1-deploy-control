from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Principal, get_current_principal, project_for_principal
from .database import connect, initialize_storage, now_iso
from .job_states import HELD, QUEUED
from .queue_store import create_job_record, transition_job_in_db


router = APIRouter(prefix="/api/batches", tags=["desktop-batches"])


class BatchCreate(BaseModel):
    expected_count: int = Field(ge=1, le=10000)
    owner_id: str = "desktop-client"
    category: str = "Uncategorized"
    settings: dict = Field(default_factory=dict)


class StageItem(BaseModel):
    project_id: str
    client_item_id: str
    project_name: str
    category: str = "Uncategorized"
    settings: dict


class FinalizeBatch(BaseModel):
    expected_count: int = Field(ge=1, le=10000)


def ensure_batch_schema() -> None:
    initialize_storage()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _project_input_files(project_path: Path) -> tuple[Path, Path]:
    input_dir = project_path / "input"
    txt_files = sorted(input_dir.glob("*.txt")) if input_dir.is_dir() else []
    audio_files: list[Path] = []
    if input_dir.is_dir():
        for extension in ("*.mp3", "*.wav", "*.m4a", "*.aac", "*.flac", "*.ogg"):
            audio_files.extend(input_dir.glob(extension))
    audio_files = sorted(audio_files)
    if len(txt_files) != 1:
        raise HTTPException(
            status_code=409,
            detail=f"Exactly one clean TXT is required; found {len(txt_files)}.",
        )
    if not audio_files:
        raise HTTPException(status_code=409, detail="Narration audio is missing.")
    if txt_files[0].stat().st_size <= 0 or audio_files[0].stat().st_size <= 0:
        raise HTTPException(status_code=409, detail="Uploaded TXT/audio is empty.")
    return txt_files[0], audio_files[0]


def _batch_for_principal(
    db: sqlite3.Connection,
    batch_id: str,
    principal: Principal,
) -> sqlite3.Row:
    row = db.execute("SELECT * FROM upload_batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Batch not found.")
    if not principal.is_admin and row["owner_id"] != principal.username:
        raise HTTPException(status_code=403, detail="Batch access denied.")
    return row


@router.post("")
def create_batch(
    payload: BatchCreate,
    principal: Principal = Depends(get_current_principal),
) -> dict:
    ensure_batch_schema()
    batch_id = uuid.uuid4().hex
    ts = now_iso()
    category = (payload.category or "Uncategorized").strip() or "Uncategorized"
    with connect() as db:
        db.execute(
            """
            INSERT INTO upload_batches (
                id, owner_id, status, expected_count, staged_count,
                verified_count, category, settings_json, created_at, updated_at
            ) VALUES (?, ?, 'uploading', ?, 0, 0, ?, ?, ?, ?)
            """,
            (batch_id, principal.username, payload.expected_count, category,
             json.dumps(payload.settings or {}, ensure_ascii=False, separators=(",", ":")), ts, ts),
        )
        db.commit()
    return {
        "id": batch_id,
        "status": "uploading",
        "expected_count": payload.expected_count,
        "staged_count": 0,
        "verified_count": 0,
    }


@router.post("/{batch_id}/stage")
def stage_batch_item(
    batch_id: str,
    payload: StageItem,
    principal: Principal = Depends(get_current_principal),
) -> dict:
    ensure_batch_schema()
    project = project_for_principal(payload.project_id, principal)
    project_path = Path(project["project_path"])
    script_path, audio_path = _project_input_files(project_path)
    script_hash = _file_sha256(script_path)
    audio_hash = _file_sha256(audio_path)
    ts = now_iso()

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        batch = _batch_for_principal(db, batch_id, principal)
        if batch["status"] != "uploading":
            raise HTTPException(
                status_code=409,
                detail=f"Batch cannot accept items in status {batch['status']}.",
            )
        duplicate = db.execute(
            "SELECT * FROM batch_items WHERE batch_id=? AND client_item_id=?",
            (batch_id, payload.client_item_id),
        ).fetchone()
        if duplicate is not None:
            db.commit()
            return {
                "batch_id": batch_id,
                "item_id": duplicate["id"],
                "job_id": duplicate["job_id"],
                "status": "vps_ready",
                "upload_verified": True,
                "idempotent": True,
            }

        settings = dict(payload.settings or {})
        settings["_render_mode"] = "full"
        settings["project_name"] = payload.project_name
        settings["category"] = payload.category or "Uncategorized"
        job_id = create_job_record(
            db,
            project=project,
            settings=settings,
            initial_status=HELD,
            batch_id=batch_id,
            client_item_id=payload.client_item_id,
            category=payload.category or "Uncategorized",
            upload_verified=True,
        )
        item_id = uuid.uuid4().hex
        item_order = int(db.execute(
            "SELECT COALESCE(MAX(item_order),0)+1 FROM batch_items WHERE batch_id=?",
            (batch_id,),
        ).fetchone()[0])
        db.execute(
            """
            INSERT INTO batch_items (
                id, batch_id, client_item_id, project_id, job_id,
                project_name, category, upload_verified, item_order,
                script_sha256, audio_sha256, script_size, audio_size,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                batch_id,
                payload.client_item_id,
                payload.project_id,
                job_id,
                payload.project_name,
                payload.category or "Uncategorized",
                item_order,
                script_hash,
                audio_hash,
                script_path.stat().st_size,
                audio_path.stat().st_size,
                ts,
                ts,
            ),
        )
        counts = db.execute(
            """
            SELECT COUNT(*) AS staged,
                   SUM(CASE WHEN upload_verified=1 THEN 1 ELSE 0 END) AS verified
            FROM batch_items WHERE batch_id=?
            """,
            (batch_id,),
        ).fetchone()
        db.execute(
            """
            UPDATE upload_batches
            SET staged_count=?, verified_count=?, updated_at=? WHERE id=?
            """,
            (int(counts["staged"] or 0), int(counts["verified"] or 0), ts, batch_id),
        )
        db.commit()

    return {
        "batch_id": batch_id,
        "item_id": item_id,
        "job_id": job_id,
        "status": "vps_ready",
        "upload_verified": True,
        "script_sha256": script_hash,
        "audio_sha256": audio_hash,
    }


@router.post("/{batch_id}/finalize")
def finalize_batch(
    batch_id: str,
    payload: FinalizeBatch,
    principal: Principal = Depends(get_current_principal),
) -> dict:
    ensure_batch_schema()
    ts = now_iso()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        batch = _batch_for_principal(db, batch_id, principal)
        counts = db.execute(
            """
            SELECT COUNT(*) AS staged,
                   SUM(CASE WHEN upload_verified=1 THEN 1 ELSE 0 END) AS verified
            FROM batch_items WHERE batch_id=?
            """,
            (batch_id,),
        ).fetchone()
        staged = int(counts["staged"] or 0)
        verified = int(counts["verified"] or 0)
        if payload.expected_count != int(batch["expected_count"]):
            raise HTTPException(
                status_code=409,
                detail="Desktop expected count does not match the created batch.",
            )
        if staged != payload.expected_count or verified != payload.expected_count:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Batch incomplete: expected {payload.expected_count}, "
                    f"staged {staged}, verified {verified}."
                ),
            )
        job_rows = db.execute(
            """
            SELECT j.id FROM jobs j
            JOIN batch_items bi ON bi.job_id=j.id
            WHERE bi.batch_id=? AND j.status=? AND bi.upload_verified=1
              AND COALESCE(j.drive_input_status,'not_required') IN ('not_required','verified')
            ORDER BY bi.created_at
            """,
            (batch_id, HELD),
        ).fetchall()
        for row in job_rows:
            transition_job_in_db(
                db,
                str(row["id"]),
                QUEUED,
                stage="queued",
                progress=0,
                message="Queued after complete batch verification",
                event_type="batch_released",
                detail={"batch_id": batch_id},
            )
        db.execute(
            """
            UPDATE upload_batches
            SET status=CASE WHEN status='processing' THEN 'processing' ELSE 'accepted' END,
                staged_count=?, verified_count=?,
                finalized_at=?, updated_at=? WHERE id=?
            """,
            (staged, verified, ts, ts, batch_id),
        )
        db.commit()
    return {
        "id": batch_id,
        "status": "accepted",
        "expected_count": payload.expected_count,
        "staged_count": staged,
        "verified_count": verified,
        "safe_to_shutdown_pc": True,
        "message": (
            f"{verified}/{payload.expected_count} projects uploaded and verified. "
            "VPS queue is persistent. You may safely shut down the PC."
        ),
    }


@router.get("/{batch_id}")
def get_batch(
    batch_id: str,
    principal: Principal = Depends(get_current_principal),
) -> dict:
    ensure_batch_schema()
    with connect() as db:
        batch = _batch_for_principal(db, batch_id, principal)
        items = db.execute(
            """
            SELECT bi.*, j.status AS job_status, j.progress AS job_progress,
                   j.current_stage AS job_stage, j.message AS job_message,
                   j.error_text AS job_error, j.attempt AS job_attempt,
                   j.drive_remote_path, j.drive_web_url, j.drive_verified, j.pipeline_stage
            FROM batch_items bi JOIN jobs j ON j.id=bi.job_id
            WHERE bi.batch_id=? ORDER BY bi.item_order, bi.created_at, bi.id
            """,
            (batch_id,),
        ).fetchall()
    return {
        **dict(batch),
        "items": [dict(row) for row in items],
        "safe_to_shutdown_pc": (
            batch["status"] == "accepted"
            and int(batch["verified_count"]) == int(batch["expected_count"])
        ),
    }

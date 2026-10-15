from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .auth import Principal, get_current_principal, job_for_principal
# Algrow Voice is excluded from fresh installations. Do not report success
# for control actions targeting a service that is deliberately absent.
import os
if os.getenv("FRESH_WEBSITE_INSTALL", "0") == "1":
    def set_algrow_paused(_paused: bool, _actor: str) -> None:
        raise RuntimeError("Algrow Voice is excluded from the fresh website")
else:
    from .algrow_bridge import set_paused as set_algrow_paused
from .config import app_root
from .database import connect, initialize_storage, now_iso
from .job_states import InvalidJobTransition
from .queue_store import request_cancel, retry_job


router = APIRouter(prefix="/api/acs-control", tags=["acs-control"])
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class ConfirmBatch(BaseModel):
    confirmation: str = ""


def _audit(db, principal: Principal, action: str, target_type: str, target_id: str, detail: dict | None = None) -> None:
    db.execute(
        """INSERT INTO acs_control_audit(actor,action,target_type,target_id,detail_json,created_at)
           VALUES(?,?,?,?,?,?)""",
        (principal.username, action, target_type, target_id, json.dumps(detail or {}, sort_keys=True), now_iso()),
    )


def _batch_for_principal(db, batch_id: str, principal: Principal) -> dict:
    row = db.execute("SELECT * FROM upload_batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    item = dict(row)
    if not principal.is_admin and str(item["owner_id"]) != principal.username:
        raise HTTPException(status_code=403, detail="Batch access denied")
    return item


@router.get("/snapshot")
def snapshot(
    q: str = Query(default="", max_length=200),
    status: str = Query(default="", max_length=40),
    batch_id: str = Query(default="", max_length=100),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
) -> dict:
    initialize_storage()
    where: list[str] = []
    params: list[object] = []
    if not principal.is_admin:
        where.append("p.owner_id=?")
        params.append(principal.username)
    if q.strip():
        token = f"%{q.strip()}%"
        where.append("(p.name LIKE ? OR p.id LIKE ? OR j.id LIKE ? OR j.batch_id LIKE ?)")
        params.extend([token, token, token, token])
    if status.strip():
        where.append("j.status=?")
        params.append(status.strip())
    if batch_id.strip():
        where.append("j.batch_id=?")
        params.append(batch_id.strip())
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    with connect() as db:
        summary_scope = "" if principal.is_admin else "WHERE p.owner_id=?"
        summary_params: tuple[object, ...] = () if principal.is_admin else (principal.username,)
        summary = {str(row["status"]): int(row["n"]) for row in db.execute(
            f"""SELECT j.status,COUNT(*) n FROM jobs j JOIN projects p ON p.id=j.project_id
                {summary_scope} GROUP BY j.status""", summary_params
        ).fetchall()}
        total = int(db.execute(
            f"SELECT COUNT(*) FROM jobs j JOIN projects p ON p.id=j.project_id {where_sql}", params
        ).fetchone()[0])
        jobs = [dict(row) for row in db.execute(
            f"""SELECT j.id,j.project_id,p.name AS project_name,p.owner_id,j.batch_id,j.status,
                       j.progress,j.stage_progress,j.current_stage,j.pipeline_stage,j.message,j.error_text,
                       j.cancel_requested,j.cancel_after_upload_attempt,j.attempt,j.priority,j.queue_sequence,
                       j.drive_status,j.drive_remote_path,j.drive_web_url,j.local_cleanup_status,
                       j.cleanup_error,j.created_at,j.updated_at
                FROM jobs j JOIN projects p ON p.id=j.project_id
                {where_sql}
                ORDER BY j.updated_at DESC,j.id DESC LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()]

        batch_scope = "WHERE b.owner_id=?" if not principal.is_admin else ""
        batch_params: tuple[object, ...] = (principal.username,) if not principal.is_admin else ()
        batches = [dict(row) for row in db.execute(
            f"""SELECT b.id,b.owner_id,b.status,b.control_state,b.category,b.expected_count,
                       b.staged_count,b.verified_count,b.settings_version,b.fg_name,b.fg_sha256,
                       b.created_at,b.updated_at,
                       COUNT(j.id) AS project_count,
                       SUM(CASE WHEN j.status='queued' THEN 1 ELSE 0 END) AS queued_count,
                       SUM(CASE WHEN j.status='running' THEN 1 ELSE 0 END) AS running_count,
                       SUM(CASE WHEN j.status='failed' THEN 1 ELSE 0 END) AS failed_count,
                       SUM(CASE WHEN j.status='completed' THEN 1 ELSE 0 END) AS completed_count,
                       SUM(CASE WHEN j.status='cancelled' THEN 1 ELSE 0 END) AS cancelled_count
                FROM upload_batches b LEFT JOIN jobs j ON j.batch_id=b.id
                {batch_scope}
                GROUP BY b.id ORDER BY b.created_at DESC LIMIT 100""",
            batch_params,
        ).fetchall()]
        controls = [dict(row) for row in db.execute(
            "SELECT name,paused,actor,updated_at FROM acs_pipeline_controls ORDER BY name"
        ).fetchall()]
        assets = [dict(row) for row in db.execute(
            """SELECT name,category,relative_path,sha256,size_bytes,width,height,status,modified_at
               FROM shared_fg_assets ORDER BY category,name"""
        ).fetchall()]
        cleanup = {str(row["name"]): {"value": str(row["value"]), "updated_at": str(row["updated_at"])}
                   for row in db.execute("SELECT name,value,updated_at FROM cleanup_runtime").fetchall()}
        cleanup_counts = {str(row["local_cleanup_status"]): int(row["n"])
                          for row in db.execute(
                              f"""SELECT j.local_cleanup_status,COUNT(*) n FROM jobs j
                                  JOIN projects p ON p.id=j.project_id {summary_scope}
                                  GROUP BY j.local_cleanup_status""", summary_params
                          ).fetchall()}
        audit_scope = "" if principal.is_admin else "WHERE actor=?"
        audit = [dict(row) for row in db.execute(
            """SELECT id,actor,action,target_type,target_id,detail_json,created_at
               FROM acs_control_audit """ + audit_scope + " ORDER BY id DESC LIMIT 30",
            () if principal.is_admin else (principal.username,),
        ).fetchall()]

    for row in audit:
        try:
            row["detail"] = json.loads(row.pop("detail_json") or "{}")
        except json.JSONDecodeError:
            row["detail"] = {}
    return {
        "ok": True, "is_admin": principal.is_admin, "updated_at": now_iso(),
        "jobs": {"items": jobs, "total": total, "limit": limit, "offset": offset}, "summary": summary,
        "batches": batches, "pipeline_controls": controls, "fg_assets": assets,
        "cleanup": {"runtime": cleanup, "counts": cleanup_counts}, "audit": audit,
    }


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: str, principal: Principal = Depends(get_current_principal)) -> dict:
    initialize_storage()
    with connect() as db:
        batch = _batch_for_principal(db, batch_id, principal)
        counts = {str(row["status"]): int(row["n"]) for row in db.execute(
            "SELECT status,COUNT(*) n FROM jobs WHERE batch_id=? GROUP BY status", (batch_id,)
        ).fetchall()}
    try:
        settings = json.loads(str(batch.get("settings_json") or "{}"))
    except json.JSONDecodeError:
        settings = {"_error": "Stored settings JSON is invalid"}
    batch["settings"] = settings
    batch.pop("settings_json", None)
    return {"ok": True, "batch": batch, "counts": counts}


@router.get("/fg/{asset_name:path}/preview")
def fg_preview(asset_name: str, _principal: Principal = Depends(get_current_principal)) -> FileResponse:
    initialize_storage()
    with connect() as db:
        row = db.execute(
            "SELECT relative_path,status FROM shared_fg_assets WHERE name=?", (asset_name,)
        ).fetchone()
    if row is None or str(row["status"]) != "available":
        raise HTTPException(status_code=404, detail="FG asset is unavailable")
    root = (app_root() / "shared" / "fg").resolve()
    target = (root / str(row["relative_path"])).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid FG asset path") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="FG file is missing")
    return FileResponse(target)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, principal: Principal = Depends(get_current_principal)) -> dict:
    job_for_principal(job_id, principal)
    result = request_cancel(job_id)
    with connect() as db:
        _audit(db, principal, "cancel_project", "job", job_id, {"result_status": result["status"]})
        db.commit()
    return {"ok": True, "job": result}


@router.post("/jobs/{job_id}/retry")
def retry_failed_job(job_id: str, principal: Principal = Depends(get_current_principal)) -> dict:
    job_for_principal(job_id, principal)
    try:
        result = retry_job(job_id)
    except InvalidJobTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    with connect() as db:
        _audit(db, principal, "retry_project", "job", job_id, {"attempt": result["attempt"]})
        db.commit()
    return {"ok": True, "job": result}


def _set_batch_state(batch_id: str, state: str, principal: Principal) -> dict:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        batch = _batch_for_principal(db, batch_id, principal)
        if str(batch["status"]) in TERMINAL:
            raise HTTPException(status_code=409, detail=f"Batch is already {batch['status']}")
        db.execute("UPDATE upload_batches SET control_state=?,updated_at=? WHERE id=?", (state, now_iso(), batch_id))
        if state == "paused":
            db.execute("UPDATE scheduler_state SET value='',updated_at=? WHERE name='active_batch_id' AND value=?", (now_iso(), batch_id))
        _audit(db, principal, f"{state}_batch", "batch", batch_id)
        db.commit()
    return {"ok": True, "batch_id": batch_id, "control_state": state}


@router.post("/batches/{batch_id}/pause")
def pause_batch(batch_id: str, principal: Principal = Depends(get_current_principal)) -> dict:
    return _set_batch_state(batch_id, "paused", principal)


@router.post("/batches/{batch_id}/resume")
def resume_batch(batch_id: str, principal: Principal = Depends(get_current_principal)) -> dict:
    return _set_batch_state(batch_id, "active", principal)


@router.post("/batches/{batch_id}/cancel-remaining")
def cancel_remaining_batch(
    batch_id: str, payload: ConfirmBatch,
    principal: Principal = Depends(get_current_principal),
) -> dict:
    if payload.confirmation != batch_id:
        raise HTTPException(status_code=400, detail="Confirmation must exactly match the batch ID")
    with connect() as db:
        _batch_for_principal(db, batch_id, principal)
        job_ids = [str(row["id"]) for row in db.execute(
            "SELECT id FROM jobs WHERE batch_id=? AND status NOT IN ('completed','failed','cancelled') ORDER BY queue_sequence,id",
            (batch_id,),
        ).fetchall()]
    for job_id in job_ids:
        request_cancel(job_id)
    with connect() as db:
        db.execute("UPDATE upload_batches SET control_state='cancelled',updated_at=? WHERE id=?", (now_iso(), batch_id))
        _audit(db, principal, "cancel_remaining_batch", "batch", batch_id, {"job_count": len(job_ids)})
        db.commit()
    return {"ok": True, "batch_id": batch_id, "cancel_requested": len(job_ids)}


def _set_pipeline(name: str, paused: bool, principal: Principal) -> dict:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Global pipeline controls require Scene Builder administrator access")
    if name not in {"voice", "transcribe", "render", "compress", "upload"}:
        raise HTTPException(status_code=404, detail="Unknown controllable pipeline")
    if name == "voice":
        try:
            set_algrow_paused(paused, principal.username)
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO acs_pipeline_controls(name,paused,actor,updated_at)
               VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                 paused=excluded.paused,actor=excluded.actor,updated_at=excluded.updated_at""",
            (name, int(paused), principal.username, now_iso()),
        )
        _audit(db, principal, "pause_pipeline" if paused else "resume_pipeline", "pipeline", name)
        db.commit()
    return {"ok": True, "name": name, "paused": paused}


@router.post("/pipelines/{name}/pause")
def pause_pipeline(name: str, principal: Principal = Depends(get_current_principal)) -> dict:
    return _set_pipeline(name, True, principal)


@router.post("/pipelines/{name}/resume")
def resume_pipeline(name: str, principal: Principal = Depends(get_current_principal)) -> dict:
    return _set_pipeline(name, False, principal)

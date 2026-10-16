from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import Principal, get_current_principal, require_job_access, require_project_access
from .database import connect, now_iso
from .drive_ingress import DriveIngressError, validate_remote_submission
from .job_states import HELD
from .queue_store import create_job_record, get_job


router = APIRouter(prefix="/api", tags=["drive-ingress"])


class DriveSubmit(BaseModel):
    expected_manifest_sha256: str = ""
    batch_id: str = ""
    client_item_id: str = ""
    item_order: int = 0


@router.post("/projects/{project_id}/drive-submit")
def submit_drive_project(
    project_id: str,
    payload: DriveSubmit,
    project: dict = Depends(require_project_access),
    principal: Principal = Depends(get_current_principal),
) -> dict:
    try:
        inspected = validate_remote_submission(project_id, principal.username)
    except DriveIngressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    expected = payload.expected_manifest_sha256.strip().lower()
    if expected and expected != inspected["manifest_sha256"]:
        raise HTTPException(status_code=409, detail="Desktop/VPS manifest checksum mismatch")

    with connect() as db:
        existing = db.execute(
            "SELECT * FROM drive_ingress WHERE project_id=?", (project_id,)
        ).fetchone()
        if existing is not None:
            if str(existing["manifest_sha256"]) != inspected["manifest_sha256"]:
                raise HTTPException(status_code=409, detail="Project already has a different Drive manifest")
            job = get_job(str(existing["job_id"]))
            return {"idempotent": True, "ingress_status": existing["status"], "job": job}

    manifest = inspected["manifest"]
    settings = dict(manifest["settings"])
    settings["_render_mode"] = "full"
    settings["project_name"] = str(manifest.get("project_name") or project["name"])
    category = str(manifest.get("category") or "Uncategorized")
    ingress_id = uuid.uuid4().hex
    ts = now_iso()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        batch = None
        if payload.batch_id:
            batch = db.execute("SELECT * FROM upload_batches WHERE id=?", (payload.batch_id,)).fetchone()
            if batch is None:
                raise HTTPException(status_code=404, detail="Batch not found")
            if not principal.is_admin and batch["owner_id"] != principal.username:
                raise HTTPException(status_code=403, detail="Batch access denied")
            if batch["status"] not in {"uploading", "accepted", "processing"}:
                raise HTTPException(status_code=409, detail="Batch is not accepting projects")
            frozen = json.loads(batch["settings_json"] or "{}")
            if frozen:
                per_project = {key: value for key, value in settings.items() if key in {
                    "text", "audio_path", "project_name", "foreground_image_path", "metadata"
                }}
                settings = dict(frozen)
                project_metadata = dict(per_project.pop("metadata", {}) or {})
                settings.update(per_project)
                metadata = dict(settings.get("metadata") or {})
                for key in ("foreground_upload_name", "custom_font_upload_name"):
                    if key in project_metadata:
                        metadata[key] = project_metadata[key]
                metadata["category"] = str(batch["category"] or "Uncategorized")
                settings["metadata"] = metadata
            category = str(batch["category"] or "Uncategorized")
        job_id = create_job_record(
            db,
            project=project,
            settings=settings,
            initial_status=HELD,
            batch_id=payload.batch_id,
            client_item_id=payload.client_item_id,
            category=category,
            upload_verified=False,
        )
        db.execute(
            """UPDATE jobs SET input_transport='google_drive',
                   drive_input_status='pending', drive_input_folder=?,
                   drive_manifest_sha256=?, drive_input_verified=0,
                   current_stage='waiting_for_drive_download',
                   message='Drive manifest verified; waiting for VPS download', updated_at=?
               WHERE id=?""",
            (inspected["folder"], inspected["manifest_sha256"], ts, job_id),
        )
        db.execute(
            """INSERT INTO drive_ingress (
                   id,project_id,job_id,owner_id,remote_folder,manifest_sha256,
                   manifest_json,status,file_count,bytes_total,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?)""",
            (
                ingress_id, project_id, job_id, principal.username,
                inspected["folder"], inspected["manifest_sha256"],
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                inspected["file_count"], inspected["bytes_total"], ts, ts,
            ),
        )
        if batch is not None:
            records = list(manifest.get("files") or [])
            script = next((item for item in records if item.get("role") == "script"), {})
            audio = next((item for item in records if item.get("role") == "audio"), {})
            item_id = uuid.uuid4().hex
            item_order = payload.item_order or int(db.execute(
                "SELECT COALESCE(MAX(item_order),0)+1 FROM batch_items WHERE batch_id=?", (payload.batch_id,)
            ).fetchone()[0])
            db.execute(
                """INSERT INTO batch_items(
                   id,batch_id,client_item_id,project_id,job_id,project_name,category,
                   upload_verified,item_order,script_sha256,audio_sha256,script_size,audio_size,
                   created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)""",
                (item_id,payload.batch_id,payload.client_item_id or project_id,project_id,job_id,
                 str(manifest.get("project_name") or project["name"]),category,item_order,
                 str(script.get("sha256") or ""),str(audio.get("sha256") or ""),
                 int(script.get("size") or 0),int(audio.get("size") or 0),ts,ts),
            )
            counts = db.execute(
                "SELECT COUNT(*) staged,SUM(upload_verified) verified FROM batch_items WHERE batch_id=?",
                (payload.batch_id,),
            ).fetchone()
            db.execute(
                "UPDATE upload_batches SET staged_count=?,verified_count=?,updated_at=? WHERE id=?",
                (int(counts["staged"] or 0),int(counts["verified"] or 0),ts,payload.batch_id),
            )
        db.commit()
    return {
        "idempotent": False,
        "ingress_id": ingress_id,
        "ingress_status": "pending",
        "manifest_sha256": inspected["manifest_sha256"],
        "job": get_job(job_id),
    }


@router.get("/jobs/{job_id}/drive-ingress")
def drive_ingress_status(_job: dict = Depends(require_job_access)) -> dict:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM drive_ingress WHERE job_id=?", (_job["id"],)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job does not use Drive ingress")
    item = dict(row)
    item.pop("manifest_json", None)
    return item

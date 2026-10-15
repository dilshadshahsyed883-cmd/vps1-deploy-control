from __future__ import annotations

from collections import OrderedDict

from fastapi import APIRouter, Depends

from .auth import Principal, get_current_principal
from .database import connect, initialize_storage, now_iso


router = APIRouter(tags=["live-audit"])


@router.get("/api/live-audit")
def live_audit(principal: Principal = Depends(get_current_principal)) -> dict:
    """Compatibility payload for the established ACS Live batch-audit layout."""
    initialize_storage()
    owner_sql = "" if principal.is_admin else "WHERE p.owner_id=?"
    params: tuple[object, ...] = () if principal.is_admin else (principal.username,)
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            f"""SELECT j.id,j.project_id,j.batch_id,j.status,j.current_stage,j.pipeline_stage,
                       j.upload_verified,j.drive_verified,j.drive_status,j.drive_web_url,
                       j.drive_remote_path,j.error_text,j.message,j.created_at,j.updated_at,
                       p.name AS project_name,p.owner_id,
                       COALESCE(b.category,j.category,'Uncategorized') AS category,
                       COALESCE(bi.upload_verified,0) AS item_upload_verified,
                       COALESCE(bi.item_order,2147483647) AS item_order
                FROM jobs j
                JOIN projects p ON p.id=j.project_id
                LEFT JOIN upload_batches b ON b.id=j.batch_id
                LEFT JOIN batch_items bi ON bi.job_id=j.id
                {owner_sql}
                ORDER BY j.created_at,j.id""", params
        ).fetchall()]

    groups: OrderedDict[str, dict] = OrderedDict()
    totals = {
        "total": 0, "voice_ready": 0, "completed_uploaded": 0,
        "queued": 0, "running": 0, "failed": 0, "cancelled": 0, "other": 0,
    }
    for row in rows:
        batch_id = str(row.get("batch_id") or "Legacy / unbatched")
        batch = groups.setdefault(batch_id, {
            "batch_id": batch_id, "total": 0, "voice_ready": 0,
            "completed_uploaded": 0, "queued": 0, "running": 0,
            "failed": 0, "cancelled": 0, "other": 0, "projects": [],
        })
        status = str(row.get("status") or "unknown")
        voice_ready = bool(row.get("upload_verified") or row.get("item_upload_verified"))
        # Google Drive verification is the authoritative completed/uploaded signal.
        completed_uploaded = bool(row.get("drive_verified"))
        bucket = (
            status if status in {"queued", "running", "failed", "cancelled"}
            else None if status == "completed" else "other"
        )

        batch["total"] += 1
        totals["total"] += 1
        if voice_ready:
            batch["voice_ready"] += 1
            totals["voice_ready"] += 1
        if completed_uploaded:
            batch["completed_uploaded"] += 1
            totals["completed_uploaded"] += 1
        if bucket:
            batch[bucket] += 1
            totals[bucket] += 1
        batch["projects"].append({
            "project_id": str(row.get("project_id") or ""),
            "job_id": str(row.get("id") or ""),
            "project_name": str(row.get("project_name") or row.get("project_id") or ""),
            "owner_id": str(row.get("owner_id") or ""),
            "category": str(row.get("category") or "Uncategorized"),
            "voice_ready": voice_ready,
            "status": status,
            "stage": str(row.get("current_stage") or row.get("pipeline_stage") or status),
            "drive_status": str(row.get("drive_status") or ""),
            "drive_web_url": str(row.get("drive_web_url") or ""),
            "drive_remote_path": str(row.get("drive_remote_path") or ""),
            "error": str(row.get("error_text") or ""),
            "message": str(row.get("message") or ""),
        })

    batches = list(groups.values())
    batches.reverse()
    for batch in batches:
        batch["projects"].sort(key=lambda item: (item["project_name"].casefold(), item["job_id"]))
    return {"ok": True, "updated_at": now_iso(), "counts": totals, "batches": batches}

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import uuid

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .auth import (
    Principal,
    get_current_principal,
    require_job_access,
    require_project_access,
)
from .algrow_bridge import snapshot as algrow_snapshot
from .config import database_path, projects_root, web_root
from .database import connect, initialize_storage, migration_status
from .job_states import InvalidJobTransition
from .queue_store import (
    JobConflict,
    JobNotFound,
    create_job,
    get_job,
    list_attempts,
    list_events,
    list_jobs,
    reconcile_stale_jobs,
    request_cancel,
    retry_job,
)
from .settings_bridge import (
    load_settings as load_project_settings,
    save_settings as save_project_settings,
    schema as project_settings_schema,
)
from .worker_runtime import runtime_snapshot


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_storage()
    reconcile_stale_jobs(auto_requeue=True)
    yield


app = FastAPI(
    title="Auto Caption Studio API",
    version="1.3.0-separated-workers",
    lifespan=lifespan,
)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    owner_id: str = Field(default="desktop-client", min_length=1, max_length=100)


class ProjectView(BaseModel):
    id: str
    name: str
    owner_id: str
    status: str
    created_at: str
    project_path: str


def _project_view(item: dict) -> ProjectView:
    return ProjectView(
        id=str(item["id"]),
        name=str(item["name"]),
        owner_id=str(item["owner_id"]),
        status=str(item["status"]),
        created_at=str(item["created_at"]),
        project_path=str(item["project_path"]),
    )


@app.get("/health")
def health() -> dict:
    initialize_storage()
    with connect() as db:
        project_count = db.execute("SELECT COUNT(*) AS count FROM projects").fetchone()["count"]
        job_count = db.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()["count"]
    migrations = migration_status()
    return {
        "ok": True,
        "app": "Auto Caption Studio",
        "version": app.version,
        "database_ready": database_path().is_file(),
        "schema_version": migrations[-1]["version"] if migrations else 0,
        "project_count": project_count,
        "job_count": job_count,
    }


@app.post("/api/projects", response_model=ProjectView)
def create_project(
    payload: ProjectCreate,
    principal: Principal = Depends(get_current_principal),
) -> ProjectView:
    initialize_storage()
    project_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    project_path = projects_root() / project_id
    for folder in ("input", "temp", "output", "preview", "logs"):
        (project_path / folder).mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": project_id,
        "name": payload.name,
        "owner_id": principal.username,
        "status": "created",
        "created_at": created_at,
    }
    (project_path / "project.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with connect() as db:
        db.execute(
            """
            INSERT INTO projects(id, name, owner_id, status, created_at, project_path)
            VALUES (?, ?, ?, 'created', ?, ?)
            """,
            (project_id, payload.name, principal.username, created_at, str(project_path)),
        )
        db.commit()
    return ProjectView(
        id=project_id,
        name=payload.name,
        owner_id=principal.username,
        status="created",
        created_at=created_at,
        project_path=str(project_path),
    )


@app.get("/api/projects", response_model=list[ProjectView])
def projects(principal: Principal = Depends(get_current_principal)) -> list[ProjectView]:
    initialize_storage()
    query = "SELECT * FROM projects"
    params: tuple = ()
    if not principal.is_admin:
        query += " WHERE owner_id=?"
        params = (principal.username,)
    query += " ORDER BY created_at DESC"
    with connect() as db:
        rows = db.execute(query, params).fetchall()
    return [_project_view(dict(row)) for row in rows]


@app.get("/api/projects/{project_id}", response_model=ProjectView)
def project(project: dict = Depends(require_project_access)) -> ProjectView:
    return _project_view(project)


@app.post("/api/projects/{project_id}/upload")
def upload_project_file(
    file: UploadFile = File(...),
    project: dict = Depends(require_project_access),
) -> dict:
    """Temporary direct-upload compatibility path retained during Phase 2."""
    safe_name = Path(file.filename or "upload.bin").name
    if safe_name in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid upload filename")
    target = Path(project["project_path"]) / "input" / safe_name
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)
    return {
        "ok": True,
        "project_id": project["id"],
        "filename": safe_name,
        "size": target.stat().st_size,
    }


@app.post("/api/projects/{project_id}/submit")
def submit_project(
    settings: dict,
    project: dict = Depends(require_project_access),
) -> dict:
    return create_job(str(project["id"]), settings)


@app.get("/api/jobs")
def jobs(
    project_id: str | None = None,
    principal: Principal = Depends(get_current_principal),
) -> list[dict]:
    if project_id:
        require_project_access(project_id, principal)
    return list_jobs(
        project_id,
        owner_id=principal.username,
        include_all_owners=principal.is_admin,
    )


@app.get("/api/jobs/{job_id}")
def job_status(_job: dict = Depends(require_job_access)) -> dict:
    return get_job(str(_job["id"]))


@app.get("/api/jobs/{job_id}/attempts")
def job_attempt_history(_job: dict = Depends(require_job_access)) -> list[dict]:
    return list_attempts(str(_job["id"]))


@app.get("/api/jobs/{job_id}/events")
def job_event_history(
    after: int = 0,
    limit: int = 500,
    _job: dict = Depends(require_job_access),
) -> list[dict]:
    return list_events(str(_job["id"]), after_id=after, limit=limit)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(_job: dict = Depends(require_job_access)) -> dict:
    return request_cancel(str(_job["id"]))


@app.post("/api/jobs/{job_id}/retry")
def retry_existing_job(_job: dict = Depends(require_job_access)) -> dict:
    try:
        return retry_job(str(_job["id"]))
    except InvalidJobTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}/logs", response_class=PlainTextResponse)
def job_logs(_job: dict = Depends(require_job_access)) -> str:
    path = Path(str(_job["log_path"] or ""))
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


@app.get("/api/jobs/{job_id}/output")
def job_output(_job: dict = Depends(require_job_access)):
    path = Path(str(_job["output_path"] or ""))
    if _job["status"] != "completed" or not path.is_file():
        raise HTTPException(status_code=404, detail="Output not available")
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


@app.get("/api/jobs/{job_id}/preview")
def job_preview(_job: dict = Depends(require_job_access)):
    path = Path(str(_job["preview_path"] or ""))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Preview not available")
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


@app.get("/api/control/schema")
def control_schema(_principal: Principal = Depends(get_current_principal)) -> dict:
    return project_settings_schema()


@app.get("/api/projects/{project_id}/settings")
def get_project_settings(project: dict = Depends(require_project_access)) -> dict:
    return load_project_settings(Path(project["project_path"]))


@app.put("/api/projects/{project_id}/settings")
def put_project_settings(
    payload: dict,
    project: dict = Depends(require_project_access),
) -> dict:
    return save_project_settings(Path(project["project_path"]), payload)


@app.post("/api/projects/{project_id}/preview")
def submit_preview(
    payload: dict,
    project: dict = Depends(require_project_access),
) -> dict:
    saved = save_project_settings(Path(project["project_path"]), payload)
    saved["_render_mode"] = "preview"
    return create_job(str(project["id"]), saved)


@app.post("/api/projects/{project_id}/render")
def submit_full_render(
    payload: dict,
    project: dict = Depends(require_project_access),
) -> dict:
    saved = save_project_settings(Path(project["project_path"]), payload)
    saved["_render_mode"] = "full"
    return create_job(str(project["id"]), saved)


from .batch_api import router as batch_router
from .acs_control_api import router as acs_control_router
from .drive_ingress_api import router as drive_ingress_router
from .live_audit_api import router as live_audit_router
from .pexels_library_api import router as pexels_library_router
from .web_flow_api import router as web_flow_router

app.include_router(batch_router)
app.include_router(acs_control_router)
app.include_router(drive_ingress_router)
app.include_router(live_audit_router)
app.include_router(pexels_library_router)
app.include_router(web_flow_router)


WEB_ROOT = web_root()
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")


@app.get("/")
def web_index(
    request: Request,
    _principal: Principal = Depends(get_current_principal),
):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_name": "Auto Caption Studio", "job_id": ""},
    )


@app.get("/jobs/{job_id}")
def web_job_status(
    request: Request,
    _job: dict = Depends(require_job_access),
):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_name": "Auto Caption Studio", "job_id": _job["id"]},
    )


@app.get("/api/live")
def live_status(principal: Principal = Depends(get_current_principal)) -> dict:
    """Authoritative owner-filtered counters and worker state for ACS Live."""
    initialize_storage()
    owner_clause = "" if principal.is_admin else "WHERE p.owner_id=?"
    owner_params: tuple[str, ...] = () if principal.is_admin else (principal.username,)
    owner_and = "" if principal.is_admin else "AND p.owner_id=?"
    with connect() as db:
        jobs = [dict(row) for row in db.execute(
            f"""
            SELECT j.id, j.batch_id, j.status, j.progress, j.stage_progress,
                   j.current_stage, j.pipeline_stage, j.message, j.drive_web_url,
                   j.drive_remote_path, j.updated_at, p.name AS project_name,
                   p.owner_id, COALESCE(b.category, j.category) AS category
            FROM jobs j JOIN projects p ON p.id=j.project_id
            LEFT JOIN upload_batches b ON b.id=j.batch_id
            {owner_clause}
            ORDER BY j.updated_at DESC LIMIT 60
            """, owner_params
        ).fetchall()]
        active_jobs = [dict(row) for row in db.execute(
            f"""SELECT j.id,j.status,j.progress,j.stage_progress,j.current_stage,
                       j.pipeline_stage,j.message,j.updated_at,p.name AS project_name,
                       p.owner_id,COALESCE(b.category,j.category) AS category
                FROM jobs j JOIN projects p ON p.id=j.project_id
                LEFT JOIN upload_batches b ON b.id=j.batch_id
                WHERE j.status='running' {owner_and}
                ORDER BY j.updated_at DESC""", owner_params
        ).fetchall()]
        stage_rows = db.execute(
            f"""SELECT j.pipeline_stage,j.status,COUNT(*) AS total
                FROM jobs j JOIN projects p ON p.id=j.project_id
                {owner_clause} GROUP BY j.pipeline_stage,j.status""", owner_params
        ).fetchall()
        stage_counts = {
            (str(row["pipeline_stage"]), str(row["status"])): int(row["total"])
            for row in stage_rows
        }
        acs_total = int(db.execute(
            f"""SELECT COUNT(*) FROM jobs j JOIN projects p ON p.id=j.project_id
                {owner_clause}""", owner_params
        ).fetchone()[0])
        drive_verified = int(db.execute(
            f"""SELECT COUNT(*) FROM jobs j JOIN projects p ON p.id=j.project_id
                WHERE j.drive_verified=1 {owner_and}""", owner_params
        ).fetchone()[0])
        controls = {
            str(row["name"]): dict(row) for row in db.execute(
                "SELECT name,paused,actor,updated_at FROM acs_pipeline_controls"
            ).fetchall()
        }
        batch_clause = "" if principal.is_admin else "WHERE owner_id=?"
        batches = [dict(row) for row in db.execute(
            f"""SELECT id, status, category, expected_count, staged_count,
                       verified_count, created_at, updated_at
                FROM upload_batches {batch_clause}
                ORDER BY created_at DESC LIMIT 30""", owner_params
        ).fetchall()]
        active_row = db.execute(
            "SELECT value FROM scheduler_state WHERE name='active_batch_id'"
        ).fetchone()

    visible_batch_ids = {str(item["id"]) for item in batches}
    active_batch_id = str(active_row["value"] or "") if active_row else ""
    if not principal.is_admin and active_batch_id not in visible_batch_ids:
        active_batch_id = ""
    active_batch = next((item for item in batches if str(item["id"]) == active_batch_id), None)
    lanes: dict[str, dict | None] = {
        "rendering": None, "compressing": None, "uploading": None,
    }
    stage_lane = {
        "ready_transcribe": "rendering",
        "ready_render": "rendering",
        "ready_compress": "compressing",
        "ready_upload": "uploading",
        "ready_publish": "uploading",
    }
    for job in active_jobs:
        lane = stage_lane.get(str(job.get("pipeline_stage") or ""))
        if lane and lanes[lane] is None:
            lanes[lane] = job
    waiting = {
        "rendering": stage_counts.get(("ready_transcribe", "queued"), 0)
                     + stage_counts.get(("ready_render", "queued"), 0),
        "compressing": stage_counts.get(("ready_compress", "queued"), 0),
        "uploading": stage_counts.get(("ready_upload", "queued"), 0)
                     + stage_counts.get(("ready_publish", "queued"), 0),
    }
    algrow = algrow_snapshot(None if principal.is_admin else principal.username)
    runtimes = runtime_snapshot()

    def control_paused(name: str) -> bool:
        if name == "voice" and algrow.get("available"):
            return bool(algrow.get("paused"))
        row = controls.get(name)
        return bool(row and int(row.get("paused") or 0))

    def acs_worker(name: str, lane: str) -> dict:
        runtime = dict(runtimes.get(name) or {})
        current = lanes.get(lane)
        paused = control_paused(name)
        online = bool(current) or bool(runtime.get("online"))
        state = "paused" if paused else "working" if current else str(runtime.get("state") or "offline")
        return {
            "name": name,
            "state": state,
            "online": online,
            "paused": paused,
            "current": current,
            "runtime": runtime,
        }

    voice_runtime = dict(algrow.get("runtime") or {})
    try:
        voice_age = max(0.0, datetime.now(timezone.utc).timestamp() - float(voice_runtime.get("last_heartbeat") or 0))
    except (TypeError, ValueError):
        voice_age = 10**9
    voice_active = algrow.get("active")
    voice_online = bool(voice_active) or voice_age <= 30
    voice_paused = control_paused("voice")
    workers = {
        "voice": {
            "name": "voice",
            "state": "paused" if voice_paused else "working" if voice_active else str(voice_runtime.get("state") or "offline"),
            "online": voice_online,
            "paused": voice_paused,
            "current": voice_active,
            "runtime": {**voice_runtime, "heartbeat_age_seconds": round(voice_age, 1)},
        },
        "render": acs_worker("render", "rendering"),
        "compress": acs_worker("compress", "compressing"),
        "upload": acs_worker("upload", "uploading"),
    }
    terminal = {"completed", "failed", "cancelled", "interrupted"}
    return {
        "ok": True,
        "version": app.version,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "active_batch": active_batch,
        "can_control": principal.is_admin,
        "pipeline_counts": {
            "received_from_drive": int(algrow.get("received") or 0),
            "algrow_queue": int(algrow.get("queue") or 0),
            "renderer_queue": waiting["rendering"],
            "compression_queue": waiting["compressing"],
            "upload_queue": waiting["uploading"],
            "drive_verified": drive_verified,
            "acs_total": acs_total,
        },
        "workers": workers,
        "algrow": algrow,
        "counts": {
            "queued_batches": sum(1 for item in batches if str(item.get("status")) not in terminal),
            "queued_projects": sum(1 for item in jobs if str(item.get("status")) == "queued"),
            "failed_projects": sum(1 for item in jobs if str(item.get("status")) in {"failed", "interrupted"}),
        },
        "lanes": lanes,
        "waiting": waiting,
        "recent_projects": jobs[:30],
    }


@app.get("/live")
def web_live_status(
    request: Request,
    _principal: Principal = Depends(get_current_principal),
):
    return templates.TemplateResponse(
        request=request,
        name="live.html",
        context={"app_name": "Auto Caption Studio Live"},
    )
# ACS_SHARED_FG_ROUTE_BEGIN
from .fg_assets_api import router as _acs_shared_fg_router
_acs_existing_routes = {(getattr(_r, 'path', ''), frozenset(getattr(_r, 'methods', set()) or set())) for _r in app.routes}
for _acs_fg_route in _acs_shared_fg_router.routes:
    _acs_fg_key = (getattr(_acs_fg_route, 'path', ''), frozenset(getattr(_acs_fg_route, 'methods', set()) or set()))
    if _acs_fg_key not in _acs_existing_routes:
        app.router.routes.append(_acs_fg_route)
        _acs_existing_routes.add(_acs_fg_key)
del _acs_existing_routes, _acs_fg_route, _acs_fg_key, _acs_shared_fg_router
# ACS_SHARED_FG_ROUTE_END

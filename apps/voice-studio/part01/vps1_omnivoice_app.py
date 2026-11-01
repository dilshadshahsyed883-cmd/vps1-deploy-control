from __future__ import annotations
from pathlib import Path
import os
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import voice_clone_api as vc
import kokoro_api as ko
import edge_api as ed
import audio_downloader_api as ad
import asyncio
from fastapi.responses import JSONResponse
from voice_tenant import scene_identity, set_identity, reset_identity
BASE=Path(__file__).resolve().parent
templates=Jinja2Templates(directory=str(BASE/"templates"))
app=FastAPI(title="VPS1 OmniVoice Voice Studio",docs_url=None,redoc_url=None)
app.include_router(vc.router)
app.include_router(ko.router)
app.include_router(ed.router)
app.include_router(ad.router)
@app.middleware("http")
async def voice_session_and_permissions(request: Request, call_next):
    """Live SSO for all public Voice API routes, not just sidebar links.

    Health probes remain anonymous. Legacy Audio Downloader retains Song grant.
    """
    path = request.url.path
    if path == "/healthz":
        return await call_next(request)
    try:
        identity = await asyncio.to_thread(scene_identity, request.headers.get("cookie", ""))
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    username = identity["username"]
    admin = identity["is_admin"]
    grants = identity["allowed_studios"]
    required = "song" if path.startswith("/tts/api/audio-downloader") else "voice"
    if not admin and required not in grants:
        return JSONResponse({"detail": required + " Studio access required"}, status_code=403)
    # Endpoint guards are enforced server-side, never merely hidden in the UI.
    protected = (
        path.startswith("/tts/api/clone/worker/") or
        (path.startswith("/tts/api/clone/omnivoice/") and
         any(path.endswith("/" + item) for item in ("config", "connect", "disconnect", "test")))
    )
    if request.method not in ("GET", "HEAD") and protected and not admin:
        return JSONResponse({"detail": "Shared provider configuration requires Admin"}, status_code=403)
    tokens = set_identity(username, admin)
    try:
        return await call_next(request)
    finally:
        reset_identity(tokens)

@app.on_event("startup")
def _startup():
    vc.init_storage()
    ko.init_storage()
    ed.init_storage()
    if os.getenv("FRESH_WEBSITE_SMOKE_TEST", "0") == "1":
        # Exercise genuine storage initialization but prevent external extraction.
        ad.init_storage()
    else:
        ad.start_worker()
@app.get("/",response_class=HTMLResponse)
@app.get("/app",response_class=HTMLResponse)
def studio(request:Request):
    response = templates.TemplateResponse(request, "voice_studio.html", {})
    response.headers["Cache-Control"] = "private, no-store, max-age=0, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Voice-Queue-UI"] = "VSQ-20261020-01"
    return response
@app.get("/tts/api/drive/folders")
def drive_folders():
    from voice_clone_drive_spooler import rclone, REMOTE, DRIVE_ROOT
    try:
        result = rclone(["lsjson", f"{REMOTE}:{DRIVE_ROOT}", "--dirs-only", "--max-depth", "1"], timeout=20)
        rows = json.loads(result.stdout or "[]")
        return {"ok": True, "root": DRIVE_ROOT, "folders": sorted(str(r["Name"]) for r in rows if r.get("IsDir") and r.get("Name"))}
    except Exception as exc:
        raise HTTPException(503, "Voice Drive folder listing unavailable. Check the Voice Studio Drive connection and retry.") from exc


@app.get("/healthz")
def health():
    vc.init_storage()
    with vc.db() as con:
        profiles=con.execute("select count(*) from profiles").fetchone()[0]
        jobs=con.execute("select count(*) from jobs").fetchone()[0]
    cfg=vc.effective_omnivoice_config()
    online,error=vc.omnivoice_worker_health(timeout_seconds=4.0) if cfg.get("endpoint_url") else (False,"not_configured")
    return {"ok":True,"app":"VPS1 OmniVoice Voice Studio","profiles":int(profiles),"jobs":int(jobs),"omnivoice_configured":bool(cfg.get("saved_endpoint_url") or cfg.get("endpoint_url")),"omnivoice_enabled":bool(cfg.get("enabled")),"worker_online":bool(online),"worker_error":str(error or "")}
@app.get("/tts/api/studio/readiness")
def studio_readiness():
    """One safe status endpoint for each isolated Voice Studio engine."""
    kokoro = ko.status()
    edge = ed.status()
    omni = vc.effective_omnivoice_config()
    connected = bool(omni.get("enabled") and omni.get("endpoint_url"))
    online, error = vc.omnivoice_worker_health(timeout_seconds=3.0) if connected else (False, "not_configured")
    downloader = ad.status()
    return {
        "ok": True,
        "engines": {
            "kokoro": {"ready": bool(kokoro.get("ready")), "reason": kokoro.get("reason", "")},
            "edge": {"ready": bool(edge.get("ready")), "reason": edge.get("reason", "")},
            "omnivoice": {"ready": bool(online), "configured": bool(omni.get("saved_endpoint_url")),
                          "enabled": bool(omni.get("enabled")), "reason": str(error or "")},
            "audio_downloader": {"ready": bool(downloader.get("ready")),
                                 "worker_online": bool(downloader.get("worker"))},
        },
    }

@app.get("/tts/api/status")
def compat_status(): return {"busy":False,"queue_paused":False,"mode":"omnivoice-only"}
@app.get("/tts/api/voices")
def compat_voices(): return {"languages":[],"voices":{},"mode":"omnivoice-only"}
@app.get("/tts/api/folders")
def compat_folders(): return {"folders":[]}
@app.get("/tts/api/queue")
def compat_queue(): return {"items":[],"jobs":[]}

"""Song Studio Audio Downloader process boundary.

Staged entrypoint only: production must explicitly enable the guarded cutover.
No Voice synthesis router/queue is imported or started here. Historical Song
data remains at the original AUDIO_DOWNLOADER_ROOT; no migration or deletion.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import song_audio_downloader_api as audio
from song_tenant import scene_identity, set_identity, reset_identity

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title="VPS1 Song Studio Audio Downloader", docs_url=None, redoc_url=None)
app.include_router(audio.router)


@app.middleware("http")
async def song_audio_session(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    try:
        identity = await asyncio.to_thread(scene_identity, request.headers.get("cookie", ""))
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if not identity["is_admin"] and "song" not in identity["allowed_studios"]:
        return JSONResponse({"detail": "Song Studio access required"}, status_code=403)
    request.state.scene_owner = identity["username"]
    request.state.scene_admin = identity["is_admin"]
    tokens = set_identity(identity["username"], identity["is_admin"])
    try:
        return await call_next(request)
    finally:
        reset_identity(tokens)


@app.on_event("startup")
def song_audio_startup():
    # Never start a second downloader while legacy Voice still owns the worker.
    # The release orchestrator must first verify idle queues and move ownership.
    if os.environ.get("SONG_AUDIO_CUTOVER_ACTIVE") != "1":
        raise RuntimeError("Song Audio service blocked pending guarded cutover")
    raw = os.environ.get("AUDIO_DOWNLOADER_ROOT", "")
    root = Path(raw)
    if not raw or not root.is_absolute() or ".." in root.parts or root.is_symlink():
        raise RuntimeError("Song Audio canonical data root missing or unsafe")
    if root != audio.ROOT:
        raise RuntimeError("Song Audio data root changed after module import")
    if root.exists() and root.resolve(strict=True) != root:
        raise RuntimeError("Song Audio data root contains linked directories")
    if os.environ.get("FRESH_WEBSITE_SMOKE_TEST") != "1":
        if not root.is_dir() or not (root / "data/jobs.sqlite3").is_file():
            raise RuntimeError("Existing Song Audio queue is missing; refusing empty replacement")
    if os.environ.get("FRESH_WEBSITE_SMOKE_TEST") == "1":
        audio.init_storage()
    else:
        # start_worker takes an OS-level, fail-closed exclusive lock.
        audio.start_worker()


@app.get("/audio-downloader/app", response_class=HTMLResponse)
def downloader_page(request: Request):
    # Scene's canonical Song page extracts the existing downloader section.
    # This loopback-only route intentionally preserves the reviewed HTML.
    response = templates.TemplateResponse(request, "voice_studio.html", {})
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/healthz")
def health():
    # Anonymous health intentionally exposes no queues, identities or media.
    smoke = os.environ.get("FRESH_WEBSITE_SMOKE_TEST") == "1"
    worker_running = bool(audio._worker is not None and audio._worker.is_alive())
    return {"ok": smoke or worker_running, "app": "song-audio-downloader",
            "worker_active": worker_running, "smoke": smoke}

"""Stable VPS_1 web controls for Imagen and Qwen Voice Clone Salad engines.

A single owner-scoped web control per project; a branch image swap happens in
Salad, never through this API. CPU orchestration only; all inference is remote.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

KINDS = ("imagen", "voice-clone")
IDS = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SUPPORTED_IMAGE = re.compile(r"^ghcr[.]io/dilshadshahsyed883-cmd/(imagen|voice_clone):[A-Za-z0-9_.-]+$")
OUTPUT_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.(?:png|jpe?g|webp)$", re.I)
MAX_VOICE_REFERENCE = 20 * 1024 * 1024
MAX_IMAGE_REFERENCE = 12 * 1024 * 1024
MAX_OUTPUT = 256 * 1024 * 1024


def checked_endpoint(value: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    if not candidate:
        return ""
    try:
        p = urlsplit(candidate)
        port = p.port
    except ValueError as exc:
        raise HTTPException(422, "Invalid GPU gateway URL") from exc
    allowed = {x.strip().lower() for x in os.getenv("EXPERIMENT_GPU_ALLOWED_HOSTS", "").split(",") if x.strip()}
    host = (p.hostname or "").lower()
    if (p.scheme != "https" or not host or (not host.endswith(".salad.cloud") and host not in allowed)
            or p.username or p.password or port not in (None, 443)
            or p.path not in ("", "/") or p.query or p.fragment):
        raise HTTPException(422, "Use an approved HTTPS SaladCloud gateway hostname")
    return "https://" + p.netloc.rstrip("/")


def checked_id(value: str) -> str:
    if not IDS.fullmatch(value or ""):
        raise HTTPException(422, "Invalid GPU job identifier")
    return value


def image_bytes(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise HTTPException(415, "Use a valid JPG, PNG or WebP reference image")


def imagen_outputs(data: dict, job_id: str) -> list[dict]:
    """Normalize engine manifests without exposing worker URLs to the Gallery."""
    outputs = data.get("outputs")
    if isinstance(outputs, list) and outputs:
        return [item for item in outputs if isinstance(item, dict)][:8]
    scenes = data.get("scenes")
    if not isinstance(scenes, list):
        return []
    return [
        {"url": f"/jobs/{job_id}/output/{index}", "filename": scene["output"]}
        for index, scene in enumerate(scenes[:8])
        if isinstance(scene, dict) and scene.get("status") == "completed"
        and isinstance(scene.get("output"), str) and OUTPUT_NAME.fullmatch(scene["output"])
    ]


class ExperimentGpuStudios:
    def __init__(self, root: Path, transport=None):
        self.root = Path(root) / "experiment-gpu-studios"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.db = self.root / "bridge.sqlite3"
        self.transport = transport
        with self.connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS gpu_config (
                studio TEXT NOT NULL, owner TEXT NOT NULL,
                endpoint TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                salad_key TEXT NOT NULL DEFAULT '',
                auth_mode TEXT NOT NULL DEFAULT 'x-api-key',
                image_ref TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(studio, owner)
            );
            CREATE TABLE IF NOT EXISTS gpu_jobs (
                studio TEXT NOT NULL, owner TEXT NOT NULL, id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(studio, owner, id)
            );
            CREATE INDEX IF NOT EXISTS ix_gpu_jobs_owner ON gpu_jobs(studio,owner,created_at);
            """)
        os.chmod(self.db, 0o600)  # Secrets are never group/world readable

    def connection(self):
        conn = sqlite3.connect(self.db, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def config(self, studio: str, owner: str, reveal: bool = False):
        with self.connection() as conn:
            item = conn.execute(
                "SELECT * FROM gpu_config WHERE studio=? AND owner=?", (studio, owner)
            ).fetchone()
        default_mode = "bearer" if studio == "voice-clone" else "x-api-key"
        data = dict(item) if item else {
            "endpoint": "", "api_key": "", "salad_key": "",
            "auth_mode": default_mode, "image_ref": ""
        }
        if reveal:
            return data
        return {
            "endpoint": data["endpoint"],
            "auth_mode": data["auth_mode"],
            "image_ref": data["image_ref"],
            "has_api_key": bool(data["api_key"]),
            "has_salad_key": bool(data["salad_key"]),
            "configured": bool(data["endpoint"] and data["api_key"]),
            "container_policy": "same-container-explicit-image-swap-only"
        }

    def save(self, studio: str, owner: str, payload: dict):
        current = self.config(studio, owner, reveal=True)
        endpoint = checked_endpoint(payload.get("endpoint", current["endpoint"]))
        api_key = str(payload.get("api_key") or "").strip() or current["api_key"]
        salad_key = str(payload.get("salad_key") or "").strip() or current["salad_key"]
        auth_mode = str(payload.get("auth_mode") or current["auth_mode"])
        accepted = ("bearer",) if studio == "voice-clone" else ("x-api-key", "x-renderer-token")
        if auth_mode not in accepted:
            raise HTTPException(422, "Unsupported worker authentication mode")
        image_ref = str(payload.get("image_ref", current["image_ref"]) or "").strip()
        if (image_ref and (not SUPPORTED_IMAGE.fullmatch(image_ref)
                or (studio == "imagen" and "/imagen:" not in image_ref)
                or (studio == "voice-clone" and "/voice_clone:" not in image_ref)
                or image_ref.endswith(":latest"))):
            raise HTTPException(422, "Use an immutable image tag from the matching project")
        if len(api_key) > 512 or len(salad_key) > 512 or len(image_ref) > 230:
            raise HTTPException(422, "GPU configuration value too long")
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO gpu_config(studio,owner,endpoint,api_key,salad_key,auth_mode,image_ref)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(studio,owner) DO UPDATE SET
                     endpoint=excluded.endpoint,api_key=excluded.api_key,
                     salad_key=excluded.salad_key,auth_mode=excluded.auth_mode,
                     image_ref=excluded.image_ref""",
                (studio, owner, endpoint, api_key, salad_key, auth_mode, image_ref)
            )
        return self.config(studio, owner)

    def request(self, studio: str, owner: str, method: str, path: str,
                *, payload=None, files=None, timeout=20):
        cfg = self.config(studio, owner, reveal=True)
        endpoint = checked_endpoint(cfg["endpoint"])
        if not endpoint:
            raise HTTPException(503, "Configure this project's Salad GPU gateway first")
        headers = {"Accept": "application/json"}
        if cfg["salad_key"]:
            headers["Salad-Api-Key"] = cfg["salad_key"]
        if cfg["api_key"]:
            if cfg["auth_mode"] == "bearer":
                headers["Authorization"] = "Bearer " + cfg["api_key"]
            elif cfg["auth_mode"] == "x-renderer-token":
                headers["X-Renderer-Token"] = cfg["api_key"]
            else:
                headers["X-API-Key"] = cfg["api_key"]
        if not path.startswith("/") or "//" in path or ".." in path or "?" in path:
            raise HTTPException(422, "Invalid GPU API path")
        try:
            with httpx.Client(transport=self.transport, follow_redirects=False, trust_env=False,
                              timeout=timeout, headers=headers) as client:
                result = client.request(method, endpoint + path, json=payload, files=files)
        except httpx.RequestError as exc:
            raise HTTPException(503, "Salad GPU worker unreachable") from exc
        if result.is_redirect or result.status_code >= 400:
            raise HTTPException(502, "GPU worker returned HTTP " + str(result.status_code))
        return result

    def health(self, studio: str, owner: str):
        live = self.request(studio, owner, "GET", "/health", timeout=8)
        try:
            details = live.json()
        except ValueError:
            details = {}
        ready = False
        try:
            response = self.request(studio, owner, "GET", "/ready", timeout=10)
            ready = response.status_code == 200
        except HTTPException:
            ready = False
        return {"alive": live.status_code == 200, "ready": ready,
                "engine": str(details.get("engine", ""))[:128] if isinstance(details, dict) else ""}

    def capabilities(self, studio: str, owner: str):
        path = "/capabilities" if studio == "imagen" else "/runtime"
        result = self.request(studio, owner, "GET", path, timeout=10)
        try:
            data = result.json()
        except ValueError:
            raise HTTPException(502, "GPU returned non-JSON model status") from None
        if not isinstance(data, dict):
            raise HTTPException(502, "Unexpected model status")
        # Do not forward secrets or giant remote structures to the browser.
        permitted = ("engine", "model", "model_id", "ready", "modes",
                     "gpu", "gpu_name", "gpu_memory", "vram_gb", "device",
                     "loaded", "supported_modes", "version", "features")
        return {name: data[name] for name in permitted if name in data}

    def create(self, studio: str, owner: str, payload: dict, reference=None):
        if not self.config(studio, owner)["configured"]:
            raise HTTPException(503, "Save the Salad endpoint and worker API key first")
        if studio == "imagen":
            prompt = str(payload.get("prompt") or "").strip()
            if not 1 <= len(prompt) <= 4000:
                raise HTTPException(422, "Prompt must be 1–4000 characters")
            mode = str(payload.get("mode") or "generate")
            if mode not in ("generate", "edit"):
                raise HTTPException(422, "Invalid generation mode")
            try:
                width = int(payload.get("width") or 1024)
                height = int(payload.get("height") or 576)
                count = int(payload.get("count") or 1)
            except (TypeError, ValueError):
                raise HTTPException(422, "Invalid resolution/count") from None
            capabilities = self.capabilities(studio, owner)
            qwen_edit = capabilities.get("engine") == "qwen-image-edit-2511"
            modes = capabilities.get("supported_modes", capabilities.get("modes"))
            if isinstance(modes, list) and mode not in modes:
                raise HTTPException(422, "Deployed Imagen model does not support this mode")
            if qwen_edit and (mode != "edit" or len(prompt) > 2048):
                raise HTTPException(422, "Qwen requires edit mode and a prompt up to 2048 characters")
            if (width < 256 or width > 2048 or height < 256 or height > 2048
                    or width % 8 or height % 8 or not 1 <= count <= 8):
                raise HTTPException(422, "Resolution/count outside supported web limits")
            refs = []
            if mode == "edit":
                if not reference or not reference.get("data"):
                    raise HTTPException(422, "Image editing needs a reference image")
                raw = reference["data"]
                if len(raw) > MAX_IMAGE_REFERENCE:
                    raise HTTPException(413, "Reference image exceeds 12 MB")
                content_type = image_bytes(raw)
                if qwen_edit:
                    # Qwen's queue takes plain base64 references, not /assets IDs.
                    refs = [base64.b64encode(raw).decode("ascii")]
                else:
                    uploaded = self.request(studio, owner, "POST", "/assets",
                                            files={"file": ("reference.png", raw, content_type)}, timeout=60)
                    try:
                        asset = str(uploaded.json().get("asset_id") or "")
                    except ValueError:
                        asset = ""
                    if not IDS.fullmatch(asset):
                        raise HTTPException(502, "GPU did not return a valid reference asset ID")
                    refs = [asset]
            scenes = []
            for _ in range(count):
                scene = {"prompt": prompt, "seed": secrets.randbelow(2147483647)}
                if not qwen_edit:
                    scene.update(width=width, height=height)
                if refs:
                    scene["references"] = refs
                scenes.append(scene)
            response = self.request(studio, owner, "POST", "/jobs",
                                    payload={"mode": mode, "scenes": scenes}, timeout=65)
        else:
            text = str(payload.get("text") or "").strip()
            ref_text = str(payload.get("ref_text") or "").strip()
            language = str(payload.get("language") or "English")
            data = reference["data"] if reference else b""
            if not 1 <= len(text) <= 20000 or not 1 <= len(ref_text) <= 3000:
                raise HTTPException(422, "Provide narration text and exact reference transcript")
            if not data or len(data) > MAX_VOICE_REFERENCE or (
                    not data.startswith(b"RIFF") or data[8:12] != b"WAVE"):
                raise HTTPException(415, "Voice reference must be a valid WAV up to 20 MB")
            if language not in ("English", "Chinese", "Japanese", "Korean", "German", "French",
                                "Russian", "Portuguese", "Spanish", "Italian"):
                raise HTTPException(422, "Unsupported Qwen voice language")
            response = self.request(studio, owner, "POST", "/v1/jobs",
                                    payload={"text": text, "language": language,
                                             "ref_text": ref_text,
                                             "ref_audio_b64": base64.b64encode(data).decode("ascii"),
                                             "chunk_chars": 300}, timeout=80)
        try:
            result = response.json()
        except ValueError:
            raise HTTPException(502, "GPU returned an invalid job response") from None
        job_id = checked_id(str(result.get("id") or result.get("job_id") or ""))
        with self.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO gpu_jobs(studio,owner,id,status) VALUES(?,?,?,?)",
                (studio, owner, job_id, "queued")
            )
        return {"id": job_id, "status": "queued"}

    def listing(self, studio: str, owner: str):
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id,status,created_at FROM gpu_jobs
                   WHERE studio=? AND owner=? ORDER BY created_at DESC LIMIT 100""",
                (studio, owner)
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    def require_job(self, studio: str, owner: str, job_id: str):
        checked_id(job_id)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id,status,created_at FROM gpu_jobs WHERE studio=? AND owner=? AND id=?",
                (studio, owner, job_id)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Job not found")
        return dict(row)

    def status(self, studio: str, owner: str, job_id: str):
        row = self.require_job(studio, owner, job_id)
        path = ("/jobs/" if studio == "imagen" else "/v1/jobs/") + job_id
        try:
            response = self.request(studio, owner, "GET", path)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid JSON")
            status = str(data.get("status") or row["status"])[:40]
            if not re.fullmatch(r"[a-zA-Z_-]{2,40}", status):
                status = "unknown"
            with self.connection() as conn:
                conn.execute(
                    "UPDATE gpu_jobs SET status=? WHERE studio=? AND owner=? AND id=?",
                    (status, studio, owner, job_id)
                )
            outputs = imagen_outputs(data, job_id) if studio == "imagen" else data.get("outputs", [])
            return {"id": job_id, "status": status, "created_at": row["created_at"],
                    "output_count": len(outputs) if isinstance(outputs, list) else 0,
                    "error": str(data.get("error") or "")[:300],
                    "generation_seconds": data.get("generation_seconds"),
                    "audio_seconds": data.get("audio_duration_seconds"),
                    "speed_factor": data.get("speed_factor")}
        except (ValueError, HTTPException) as exc:
            # Local records remain visible if the worker is sleeping or changed.
            return {**row, "worker_error": str(exc.detail if isinstance(exc, HTTPException) else exc)[:180]}

    def cancel(self, studio: str, owner: str, job_id: str):
        self.require_job(studio, owner, job_id)
        if studio != "imagen":
            raise HTTPException(405, "This voice worker does not expose cancellation")
        result = self.request(studio, owner, "POST", "/jobs/" + job_id + "/cancel")
        with self.connection() as conn:
            conn.execute(
                "UPDATE gpu_jobs SET status='cancel_requested' WHERE studio=? AND owner=? AND id=?",
                (studio, owner, job_id)
            )
        return {"ok": result.status_code in (200, 202, 204)}

    def media(self, studio: str, owner: str, job_id: str, index: int = 0, preview: bool = False):
        self.require_job(studio, owner, job_id)
        if studio == "voice-clone":
            path = "/v1/jobs/" + job_id + "/audio"
            mimetype = "audio/wav"
            name = "voice-" + job_id + ".wav"
        else:
            if index < 0 or index > 7:
                raise HTTPException(404, "Image index not found")
            result = self.request(studio, owner, "GET", "/jobs/" + job_id)
            try:
                outputs = imagen_outputs(result.json(), job_id)
                item = outputs[index]
                url = str(item.get("url") or item.get("path") or "")
            except (IndexError, KeyError, AttributeError, ValueError, TypeError):
                raise HTTPException(404, "Image output not available") from None
            try:
                candidate = urlsplit(url)
                output_port = candidate.port
            except ValueError:
                raise HTTPException(502, "Invalid GPU output URL") from None
            configured_host = urlsplit(self.config(studio, owner, reveal=True)["endpoint"]).hostname
            if candidate.scheme or candidate.netloc:
                if (candidate.scheme != "https" or candidate.hostname != configured_host
                        or candidate.username or candidate.password or output_port not in (None, 443)):
                    raise HTTPException(502, "GPU output host mismatch")
            path = candidate.path
            if candidate.query or candidate.fragment:
                raise HTTPException(502, "GPU output path not permitted")
            prefixes = (f"/outputs/{job_id}/", f"/jobs/{job_id}/outputs/")
            name = ""
            for prefix in prefixes:
                if path.startswith(prefix):
                    name = path[len(prefix):]
                    break
            # Qwen uses a numeric scene route; the normalized filename is display-only.
            if re.fullmatch(re.escape(f"/jobs/{job_id}/output/") + r"[0-7]", path):
                name = str(item.get("filename") or "")
            if not OUTPUT_NAME.fullmatch(name) or ".." in name:
                raise HTTPException(502, "GPU output path not permitted")
            mimetype = ("image/jpeg" if name.lower().endswith((".jpg", ".jpeg"))
                        else "image/webp" if name.lower().endswith(".webp") else "image/png")
        result = self.request(studio, owner, "GET", path, timeout=90)
        if len(result.content) > MAX_OUTPUT:
            raise HTTPException(413, "GPU output exceeds download safety limit")
        return Response(content=result.content, media_type=mimetype,
                        headers={"Cache-Control": "private, no-store",
                                 "Content-Disposition": (("inline" if studio == "imagen" and preview else "attachment")
                                                         + '; filename="' + name + '"')})


def register_experiment_gpu_studios(app, templates, root, require_login, transport=None):
    bridge = ExperimentGpuStudios(Path(root), transport=transport)

    def owner(request: Request) -> str:
        require_login(request)
        username = str(request.session.get("username") or "").strip().lower()
        if not username:
            raise HTTPException(401, "Login required")
        return username

    def verify_csrf(request: Request, studio: str):
        key = "experiment_gpu_csrf_" + studio
        expected = str(request.session.get(key) or "")
        supplied = str(request.headers.get("X-Experiment-GPU-CSRF") or "")
        if not expected or not hmac.compare_digest(expected, supplied):
            raise HTTPException(403, "CSRF token required")

    @app.get("/imagen-studio/", response_class=HTMLResponse)
    @app.get("/voice-clone-studio/", response_class=HTMLResponse)
    def page(request: Request):
        studio = "imagen" if request.url.path.startswith("/imagen") else "voice-clone"
        username = owner(request)
        key = "experiment_gpu_csrf_" + studio
        if not request.session.get(key):
            request.session[key] = secrets.token_urlsafe(32)
        return templates.TemplateResponse(request, "experiment_gpu_studio.html", {
            "page": studio.replace("-", "_") + "_studio",
            "username": username,
            "studio": studio,
            "studio_title": "Imagen" if studio == "imagen" else "Voice Clone",
            "csrf_token": request.session[key]
        })

    @app.get("/api/experiment-gpu/{studio}/config")
    def config(request: Request, studio: str):
        if studio not in KINDS:
            raise HTTPException(404, "Unknown studio")
        return bridge.config(studio, owner(request))

    @app.post("/api/experiment-gpu/{studio}/config")
    async def save(request: Request, studio: str):
        if studio not in KINDS:
            raise HTTPException(404, "Unknown studio")
        user = owner(request)
        verify_csrf(request, studio)
        return bridge.save(studio, user, await request.json())

    @app.get("/api/experiment-gpu/{studio}/health")
    def health(request: Request, studio: str):
        if studio not in KINDS:
            raise HTTPException(404, "Unknown studio")
        return bridge.health(studio, owner(request))

    @app.get("/api/experiment-gpu/{studio}/models")
    def models(request: Request, studio: str):
        if studio not in KINDS:
            raise HTTPException(404, "Unknown studio")
        return bridge.capabilities(studio, owner(request))

    @app.post("/api/experiment-gpu/{studio}/jobs", status_code=202)
    async def create(request: Request, studio: str):
        if studio not in KINDS:
            raise HTTPException(404, "Unknown studio")
        user = owner(request)
        verify_csrf(request, studio)
        fields = await request.form()
        data = {key: fields.get(key) for key in (
            "prompt", "mode", "width", "height", "count", "text", "ref_text", "language"
        )}
        upload = fields.get("reference")
        reference = None
        if upload is not None and hasattr(upload, "read"):
            limit = MAX_IMAGE_REFERENCE if studio == "imagen" else MAX_VOICE_REFERENCE
            raw = await upload.read(limit + 1)
            if len(raw) > limit:
                raise HTTPException(413, "Reference file exceeds upload size limit")
            reference = {"data": raw}
        if studio == "voice-clone" and reference is None:
            raise HTTPException(422, "Provide a voice reference WAV")
        return await run_in_threadpool(bridge.create, studio, user, data, reference=reference)

    @app.get("/api/experiment-gpu/{studio}/jobs")
    def listing(request: Request, studio: str):
        if studio not in KINDS:
            raise HTTPException(404, "Unknown studio")
        return bridge.listing(studio, owner(request))

    @app.get("/api/experiment-gpu/{studio}/jobs/{job_id}")
    def status(request: Request, studio: str, job_id: str):
        if studio not in KINDS:
            raise HTTPException(404, "Unknown studio")
        return bridge.status(studio, owner(request), job_id)

    @app.post("/api/experiment-gpu/{studio}/jobs/{job_id}/cancel")
    def cancel(request: Request, studio: str, job_id: str):
        if studio not in KINDS:
            raise HTTPException(404, "Unknown studio")
        user = owner(request)
        verify_csrf(request, studio)
        return bridge.cancel(studio, user, job_id)

    @app.get("/api/experiment-gpu/{studio}/jobs/{job_id}/media")
    def media(request: Request, studio: str, job_id: str, index: int = 0, preview: bool = False):
        if studio not in KINDS:
            raise HTTPException(404, "Unknown studio")
        return bridge.media(studio, owner(request), job_id, index=index, preview=preview)

    return bridge

"""Admin-scoped VPS UI bridge to the independent HeyGen GPU service.

No AI models, GPUs or direct-to-worker browser credentials are installed here.
All job IDs are owner-scoped in local SQLite. Worker credentials stay server-side.
"""
from __future__ import annotations

import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from heygen_delivery import VerifiedMedia
from heygen_outbox import HeygenOutbox
from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac"}
IMAGE_LIMIT = 10 * 1024 * 1024
AUDIO_LIMIT = 80 * 1024 * 1024


def _check_endpoint(endpoint: str) -> str:
    """UI-configured GPU endpoints must be explicit HTTPS allowlisted hosts."""
    value = endpoint.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    hosts = {h.strip().lower() for h in os.getenv("HEYGEN_WORKER_ALLOWED_HOSTS", "").split(",") if h.strip()}
    env_endpoint = os.getenv("HEYGEN_WORKER_URL", "").strip().rstrip("/")
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.path or parsed.query or parsed.fragment or
            parsed.hostname.lower() not in hosts and value != env_endpoint):
        raise HTTPException(422, "HTTPS GPU host must be permitted in HEYGEN_WORKER_ALLOWED_HOSTS")
    return value


def _check_upload(upload: UploadFile, allowed: set[str], limit: int, kind: str) -> tuple[str, str]:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(415, f"Unsupported {kind} format")
    f = upload.file
    f.seek(0, 2)
    size = f.tell()
    if size < 1 or size > limit:
        raise HTTPException(413, f"{kind} file must be 1-{limit // (1024 * 1024)} MB")
    f.seek(0)
    magic = f.read(16)
    f.seek(0)
    if kind == "image":
        valid = (suffix == ".png" and magic.startswith(b"\x89PNG\r\n\x1a\n") or
                 suffix in {".jpg", ".jpeg"} and magic.startswith(b"\xff\xd8") or
                 suffix == ".webp" and magic[:4] == b"RIFF" and magic[8:12] == b"WEBP")
    else:
        valid = (suffix == ".wav" and magic[:4] == b"RIFF" and magic[8:12] == b"WAVE" or
                 suffix == ".flac" and magic.startswith(b"fLaC") or
                 suffix == ".m4a" and magic[4:8] == b"ftyp" or
                 suffix == ".mp3" and (magic.startswith(b"ID3") or
                                       len(magic) >= 2 and magic[0] == 0xff and (magic[1] & 0xe0) == 0xe0))
    if not valid:
        raise HTTPException(415, f"Invalid {kind} file signature")
    return suffix, upload.content_type or "application/octet-stream"


class HeygenBridge:
    def __init__(self, root: Path, transport=None):
        self.root = Path(root) / "heygen-studio"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.db = self.root / "bridge.sqlite3"
        self.transport = transport  # inject MockTransport for isolated tests
        with self.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS config(
                    owner TEXT PRIMARY KEY, endpoint TEXT NOT NULL, token TEXT NOT NULL,
                    salad_api_key TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS jobs(
                    owner TEXT NOT NULL, id TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(owner, id));
            """)
        # Backward-compatible migration for existing per-owner endpoint/token records.
        with self.connection() as con:
            columns = {row["name"] for row in con.execute("PRAGMA table_info(config)")}
            if "salad_api_key" not in columns:
                con.execute("ALTER TABLE config ADD COLUMN salad_api_key TEXT NOT NULL DEFAULT ''")
        os.chmod(self.db, 0o600)
        self.media = VerifiedMedia(self.root, self.connection, self._client)
        self.outbox = HeygenOutbox(
            self.root, self.connection, self._client,
            on_complete=self.media.schedule,
            cached=lambda owner, job: self.media.snapshot(owner, job)["state"] == "ready")

    def connection(self):
        con = sqlite3.connect(str(self.db), timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def config(self, owner: str, *, reveal: bool = False):
        with self.connection() as con:
            row = con.execute("SELECT endpoint, token, salad_api_key FROM config WHERE owner=?", (owner,)).fetchone()
        endpoint = (row["endpoint"] if row and row["endpoint"] else os.getenv("HEYGEN_WORKER_URL", "").strip())
        token = (row["token"] if row and row["token"] else os.getenv("HEYGEN_WORKER_API_KEY", "").strip())
        # The gateway key is separate from HeyGen's application Bearer token.
        # Both secrets are returned only to the server-side HTTP client.
        salad_key = (row["salad_api_key"] if row and row["salad_api_key"]
                     else os.getenv("HEYGEN_SALAD_API_KEY", "").strip())
        if reveal:
            return endpoint, token, salad_key
        return {"endpoint": endpoint, "has_token": bool(token),
                "has_salad_key": bool(salad_key),
                "image": "ghcr.io/dilshadshahsyed883-cmd/heygen:latest",
                "configured": bool(endpoint and token)}

    def save(self, owner: str, payload: dict):
        endpoint = _check_endpoint(str(payload.get("endpoint") or ""))
        _, existing_token, existing_salad_key = self.config(owner, reveal=True)
        token = str(payload.get("token") or "").strip() or existing_token
        salad_key = str(payload.get("salad_api_key") or "").strip() or existing_salad_key
        if len(token) > 512 or len(salad_key) > 512:
            raise HTTPException(422, "GPU API key too long")
        with self.connection() as con:
            con.execute("INSERT INTO config(owner,endpoint,token,salad_api_key) VALUES(?,?,?,?) "
                        "ON CONFLICT(owner) DO UPDATE SET endpoint=?,token=?,salad_api_key=?",
                        (owner, endpoint, token, salad_key, endpoint, token, salad_key))
        return self.config(owner)

    def _client(self, owner: str, timeout: int = 25):
        endpoint, token, salad_key = self.config(owner, reveal=True)
        if not endpoint or not token:
            raise HTTPException(503, "Configure HeyGen GPU endpoint and API token first")
        headers = {"Authorization": "Bearer " + token}
        if salad_key:
            headers["Salad-Api-Key"] = salad_key
        return httpx.Client(base_url=endpoint + "/", headers=headers,
                            timeout=timeout, follow_redirects=False, trust_env=False,
                            transport=self.transport)

    def request(self, owner: str, method: str, path: str, **kwargs):
        try:
            with self._client(owner, timeout=600 if "files" in kwargs else 25) as client:
                response = client.request(method, path.lstrip("/"), **kwargs)
        except (httpx.RequestError, OSError, ValueError) as exc:
            raise HTTPException(503, "HeyGen GPU worker is unreachable") from exc
        if response.status_code >= 400:
            status = 409 if response.status_code == 409 else 502
            detail = (("GPU gateway or worker returned HTTP 403: check SaladCloud API key "
                       "and HeyGen AVATAR_API_KEY") if response.status_code == 403
                      else f"GPU worker returned HTTP {response.status_code}")
            raise HTTPException(status, detail)
        return response

    def owned(self, owner: str, job_id: str):
        with self.connection() as con:
            found = con.execute("SELECT 1 FROM jobs WHERE owner=? AND id=?", (owner, job_id)).fetchone()
        if not found:
            raise HTTPException(404, "Job not found")

    def list_jobs(self, owner: str):
        """Read one worker snapshot, then expose only owner-mapped IDs."""
        with self.connection() as con:
            rows = con.execute("SELECT id,created_at FROM jobs WHERE owner=? "
                               "ORDER BY created_at DESC LIMIT 100", (owner,)).fetchall()
        if not rows:
            return {"items": []}
        try:
            remote = self.request(owner, "GET", "api/v1/jobs").json()
            if not isinstance(remote, list):
                raise ValueError("Invalid worker jobs response")
            status_map = {str(job["id"]): job for job in remote
                          if isinstance(job, dict) and "id" in job}
            online = True
        except (HTTPException, ValueError, TypeError, KeyError):
            status_map, online = {}, False
        pending = self.outbox.snapshot(owner)
        items = []
        for row in rows:
            job_id = row["id"]
            queued = pending.get(job_id)
            item = status_map.get(job_id)
            vault = self.media.snapshot(owner, job_id)
            if queued and queued["state"] != "sent":
                item = {"status": queued["state"], "error": queued.get("error") or None}
            elif item is None:
                item = {"status": ("completed" if vault["state"] == "ready" else
                                   "missing_from_worker" if online else "worker_unavailable")}
            if item.get("status") == "completed" and vault["state"] != "ready":
                vault = self.media.schedule(owner, job_id)
            items.append({**item, "id": job_id, "created_at": row["created_at"],
                          "vps_media": vault})
        return {"items": items}

    def submit(self, owner: str, image: UploadFile, audio: UploadFile):
        image_suffix, image_mime = _check_upload(image, IMAGE_EXT, IMAGE_LIMIT, "image")
        audio_suffix, audio_mime = _check_upload(audio, AUDIO_EXT, AUDIO_LIMIT, "audio")
        # A compatible worker supports retry-safe IDs, allowing a durable VPS
        # queue. Offline uploads remain private until the worker is reachable.
        try:
            capability = self.request(owner, "GET", "api/v1/engine").json()
        except HTTPException as exc:
            if exc.status_code == 503:
                return self.outbox.add(owner, image, audio, image_suffix, audio_suffix)
            raise
        if capability.get("supports_idempotent_submit") is True:
            return self.outbox.add(owner, image, audio, image_suffix, audio_suffix)
        # Backwards-compatible direct submission for older connected workers.
        # If their response is lost, do NOT auto-retry (no idempotent contract).
        response = self.request(owner, "POST", "api/v1/jobs", files={
            "image": ("portrait" + image_suffix, image.file, image_mime),
            "audio": ("speech" + audio_suffix, audio.file, audio_mime),
        })
        data = response.json()
        job_id = str(data.get("id") or "")
        if len(job_id) != 32 or not all(c in "0123456789abcdef" for c in job_id):
            raise HTTPException(502, "GPU worker returned an invalid job identifier")
        with self.connection() as con:
            con.execute("INSERT INTO jobs(owner,id,created_at) VALUES(?,?,?)",
                        (owner, job_id, datetime.now(timezone.utc).isoformat()))
        return {"id": job_id, "status": data.get("status", "queued")}

    def action(self, owner: str, job_id: str, action: str):
        self.owned(owner, job_id)
        local = self.outbox.action(owner, job_id, action)
        if local is not None:
            return local
        if action == "delete":
            self.request(owner, "DELETE", "api/v1/jobs/" + job_id)
            # Remove owner media only after the worker acknowledges deletion.
            self.media.delete(owner, job_id)
            self.outbox.forget(owner, job_id)
            with self.connection() as con:
                con.execute("DELETE FROM jobs WHERE owner=? AND id=?", (owner, job_id))
            return {"deleted": True}
        if action not in {"cancel", "retry"}:
            raise HTTPException(404, "Unknown action")
        response = self.request(owner, "POST", "api/v1/jobs/" + job_id + "/" + action)
        return response.json()

    def save_video(self, owner: str, job_id: str):
        self.owned(owner, job_id)
        state = self.media.snapshot(owner, job_id)
        if state["state"] == "ready":
            return state
        remote = self.request(owner, "GET", "api/v1/jobs/" + job_id).json()
        if remote.get("status") != "completed":
            raise HTTPException(409, "GPU job has not completed")
        return self.media.schedule(owner, job_id, force=True)

    def video(self, owner: str, job_id: str, download: bool = False):
        self.owned(owner, job_id)
        local = self.media.response(owner, job_id, download)
        if local is not None:
            return local
        client = self._client(owner, timeout=300)
        try:
            request = client.build_request("GET", "api/v1/jobs/" + job_id + "/video")
            response = client.send(request, stream=True)
            if response.status_code != 200 or "video/mp4" not in response.headers.get("content-type", ""):
                response.close()
                client.close()
                raise HTTPException(409, "Completed MP4 is not available")
        except (httpx.RequestError, OSError) as exc:
            client.close()
            raise HTTPException(503, "HeyGen MP4 download unavailable") from exc

        def iterator():
            try:
                yield from response.iter_bytes(chunk_size=1024 * 512)
            finally:
                response.close()
                client.close()

        disposition = "attachment" if download else "inline"
        return StreamingResponse(iterator(), media_type="video/mp4", headers={
            "Content-Disposition": f'{disposition}; filename="heygen-{job_id}.mp4"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        })


def register_heygen_studio(app, templates, root, require_admin, require_login, transport=None):
    bridge = HeygenBridge(Path(root), transport=transport)

    def admin(req: Request):
        require_login(req)
        require_admin(req)
        owner = str(req.session.get("username") or "")
        if not owner:
            raise HTTPException(401, "Session owner required")
        return owner

    def csrf(req: Request):
        owner = admin(req)
        expected = str(req.session.get("heygen_csrf") or "")
        received = str(req.headers.get("X-Heygen-CSRF") or "")
        if not expected or not hmac.compare_digest(expected, received):
            raise HTTPException(403, "HeyGen CSRF token required")
        return owner

    @app.get("/heygen-studio/", response_class=HTMLResponse)
    def home(request: Request):
        admin(request)
        if not request.session.get("heygen_csrf"):
            request.session["heygen_csrf"] = secrets.token_urlsafe(32)
        return templates.TemplateResponse(request, "heygen_studio.html", {
            "page": "heygen_studio", "username": request.session["username"],
            "csrf_token": request.session["heygen_csrf"],
        })

    @app.get("/api/heygen-studio/config")
    def config(request: Request):
        return bridge.config(admin(request))

    @app.post("/api/heygen-studio/config")
    async def config_save(request: Request):
        owner = csrf(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(422, "Object expected")
        return bridge.save(owner, payload)

    @app.get("/api/heygen-studio/engine")
    def engine(request: Request):
        owner = admin(request)
        return bridge.request(owner, "GET", "api/v1/engine").json()

    @app.get("/api/heygen-studio/queue")
    def queue_status(request: Request):
        return {"state": bridge.outbox.state(admin(request))}

    @app.post("/api/heygen-studio/queue/{action}")
    def queue_action(request: Request, action: str):
        return bridge.outbox.control(csrf(request), action)

    @app.get("/api/heygen-studio/jobs")
    def jobs(request: Request):
        return bridge.list_jobs(admin(request))

    @app.post("/api/heygen-studio/jobs", status_code=202)
    def create_job(request: Request, image: UploadFile = File(...), audio: UploadFile = File(...)):
        return bridge.submit(csrf(request), image, audio)

    @app.post("/api/heygen-studio/jobs/{job_id}/{action}")
    def job_action(request: Request, job_id: str, action: str):
        return bridge.action(csrf(request), job_id, action)

    @app.post("/api/heygen-studio/jobs/{job_id}/save")
    def job_save(request: Request, job_id: str):
        return bridge.save_video(csrf(request), job_id)

    @app.get("/api/heygen-studio/jobs/{job_id}/video")
    def job_video(request: Request, job_id: str, download: int = 0):
        return bridge.video(admin(request), job_id, bool(download))

    return bridge

"""Admin-only Cloud Render receiver, Media management and isolated SaladCloud config.

Safe contract: authenticated admin manages media; a separate rotating receiver token
permits chunked Salad output ingress. Local MP4 removal only follows successful
rclone transfer AND matching remote length+MD5. The canonical Drive Bridge is
provided by deployment; no OAuth credentials are created or copied here.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi import HTTPException, Request

CHUNK_MAX = 8 * 1024 * 1024
MEDIA_MAX = 20 * 1024 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# The native Linux Concat source lock in main is authoritative.
# Dockerfile exists, but registry availability and runtime acceptance are unverified.
IMAGE = os.getenv("CLOUD_RENDER_GPU_IMAGE_REF", "")
GPU = {"image": IMAGE, "image_ready": False,
       "image_status": "EXPERIMENTAL — image build, GPU render and HTTP API acceptance pending",
       "recipe": "cloud_render/concat/gpu/Dockerfile",
       "source_lock": "cloud_render/concat/source.lock.json",
       "engine": "Concat v0.2.8 native Rust concat-cli + FFmpeg",
       "architecture": "linux/amd64", "gpu": "RTX 3060 Ti", "vram_gb": 8, "cpu": 4,
       "ram_gb": 8, "storage_gb": 30, "replicas": 1, "port": 8080, "codec": "h264_nvenc",
       "concurrency": 1, "render": "1080p H.264"}


def filename(name):
    result = re.sub(r"[^A-Za-z0-9_.() -]", "_", str(name or "").replace("\\", "/").split("/")[-1]).strip(" .")
    if not result.lower().endswith(".mp4") or len(result) > 150 or len(result) < 5:
        raise HTTPException(400, "Only a valid .mp4 filename is allowed")
    return result


def endpoint_url(value):
    value = str(value or "").strip().rstrip("/")
    if not value:
        return ""
    p = urlsplit(value)
    host = (p.hostname or "").lower()
    if (p.scheme != "https" or not host.endswith(".salad.cloud") or p.username or p.password
            or p.port not in (None, 443) or p.query or p.fragment or p.path not in ("", "/")):
        raise HTTPException(400, "Use the HTTPS SaladCloud gateway hostname only")
    return value


def worker_path(raw):
    value = str(raw or "")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]{1,100}", value) or "//" in value or ".." in value:
        raise HTTPException(400, "Invalid worker path")
    return value


class RcloneFailure(RuntimeError):
    """Sanitized rclone error; raw stderr may contain sensitive URLs or tokens."""

    def __init__(self, operation, code, category):
        self.operation, self.code, self.category = operation, code, category
        super().__init__(f"Drive Bridge {operation} failed (exit {code}): {category}")


def classify_rclone_error(operation, returncode, stderr):
    msg = str(stderr or "").lower()[-8000:]
    if any(x in msg for x in ("didn't find section in config file", "config file not found",
            "configuration file not found", "not found in config file", "unknown remote")):
        cause = "remote configuration unavailable to Cloud Render service"
    elif any(x in msg for x in ("invalid_grant", "token has been expired",
            "token has been revoked", "failed to refresh token",
            "invalid credentials", "401 unauthorized")):
        cause = "Drive authentication expired or unauthorized"
    elif any(x in msg for x in ("insufficientpermissions", "insufficient permission",
            "permission denied", "access denied", "403 forbidden")):
        cause = "Google Drive destination permission denied"
    elif any(x in msg for x in ("quota exceeded", "rate limit exceeded", "storage quota")):
        cause = "Google Drive quota or rate limit reached"
    elif any(x in msg for x in ("immutable", "file already exists", "destination already exists")):
        cause = "immutable destination already exists or conflicts"
    elif any(x in msg for x in ("connection timed out", "i/o timeout", "no such host",
            "network is unreachable", "connection refused", "tls handshake timeout")):
        cause = "network or DNS connectivity failure"
    elif operation == "lsjson" and (returncode in (3, 4) or any(x in msg for x in
            ("directory not found", "object not found", "file not found", "doesn't exist"))):
        cause = "remote_file_absent"
    elif returncode == 5:
        cause = "temporary remote failure"
    elif returncode == 7:
        cause = "fatal remote error"
    else:
        cause = "unclassified remote failure; check service-side rclone logs"
    return RcloneFailure(operation, returncode, cause)


class CloudRender:
    def __init__(self, root):
        self.root = Path(root)
        self.media = self.root / "media"
        self.staging = self.root / "incoming"
        for folder in (self.root, self.media, self.staging):
            if folder.is_symlink():
                raise RuntimeError("Cloud Render root cannot be a symlink")
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.chmod(0o700)
        self.path = self.root / "cloud_render.sqlite3"
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cloud-render-drive")
        with self.connection() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS files(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                received INTEGER NOT NULL DEFAULT 0, chunks INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL, md5 TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL, drive_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            con.execute("CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, val TEXT NOT NULL)")
            con.execute("INSERT OR IGNORE INTO config(key,val) VALUES('auto_upload','0')")
            con.execute("INSERT OR IGNORE INTO config(key,val) VALUES('priority','high')")
            con.execute("INSERT OR IGNORE INTO config(key,val) VALUES('health_path','/health/ready')")
            con.execute("INSERT OR IGNORE INTO config(key,val) VALUES('timeout','300')")
            con.execute("INSERT OR IGNORE INTO config(key,val) VALUES('receiver_token',?)",
                        (secrets.token_urlsafe(48),))
            con.execute("UPDATE files SET state='failed',error='Interrupted Drive transfer; retry' WHERE state='uploading'")
            pending = [r["id"] for r in con.execute("SELECT id FROM files WHERE state='queued'")]
        self.path.chmod(0o600)
        for job in pending:
            self.pool.submit(self._upload, job)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def get(self, con, key):
        row = con.execute("SELECT val FROM config WHERE key=?", (key,)).fetchone()
        return row["val"] if row else ""

    def put(self, con, key, val):
        con.execute("INSERT INTO config(key,val) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET val=excluded.val",
                    (key, str(val)))

    def token_required(self, received):
        with self.connection() as con:
            expected = self.get(con, "receiver_token")
        if not received or not hmac.compare_digest(str(received), expected):
            raise HTTPException(401, "Invalid receiver token")

    def rotate_token(self):
        new = secrets.token_urlsafe(48)
        with self.lock, self.connection() as con:
            self.put(con, "receiver_token", new)
        return {"receiver_token": new, "warning": "Shown once; replace RENDER_OUTPUT_TOKEN in the Salad worker securely."}

    def config(self):
        with self.connection() as con:
            cfg = {k: self.get(con, k) for k in ("auto_upload","endpoint","priority","health_path","timeout","salad_key","worker_key","enabled")}
        return {**GPU, "auto_upload": cfg["auto_upload"] == "1",
                "endpoint": cfg["endpoint"], "priority": cfg["priority"] or "high",
                "health_path": cfg["health_path"] or "/health/ready",
                "timeout": int(cfg["timeout"] or 300),
                "enabled": cfg["enabled"] == "1",
                "salad_key_saved": bool(cfg["salad_key"]),
                "worker_key_saved": bool(cfg["worker_key"]),
                "drive_bridge_available": shutil.which("rclone") is not None}

    def save(self, obj):
        accepted = {"auto_upload","endpoint","priority","health_path","timeout","salad_key","worker_key"}
        if not isinstance(obj, dict) or set(obj) - accepted:
            raise HTTPException(400, "Unknown GPU configuration field")
        with self.lock, self.connection() as con:
            if "auto_upload" in obj:
                if type(obj["auto_upload"]) is not bool:
                    raise HTTPException(400, "auto_upload must be boolean")
                self.put(con, "auto_upload", int(obj["auto_upload"]))
            if "endpoint" in obj:
                self.put(con, "endpoint", endpoint_url(obj["endpoint"]))
                self.put(con, "enabled", "0")
            if "priority" in obj:
                if obj["priority"] not in ("low","medium","high"):
                    raise HTTPException(400, "Unsupported priority")
                self.put(con, "priority", obj["priority"])
            if "health_path" in obj:
                self.put(con, "health_path", worker_path(obj["health_path"]))
            if "timeout" in obj:
                try: timeout = int(obj["timeout"])
                except (TypeError, ValueError): raise HTTPException(400, "Invalid timeout") from None
                if not 10 <= timeout <= 1800:
                    raise HTTPException(400, "Timeout outside 10–1800 seconds")
                self.put(con, "timeout", timeout)
            for k in ("salad_key", "worker_key"):
                if obj.get(k):
                    value = str(obj[k])
                    if len(value) > 1024 or "\n" in value or "\r" in value:
                        raise HTTPException(400, "Invalid key")
                    self.put(con, k, value)
        return self.config()

    def probe(self, connect=False):
        with self.connection() as con:
            url = endpoint_url(self.get(con, "endpoint"))
            path = self.get(con, "health_path") or "/health/ready"
            gateway = self.get(con, "salad_key")
            api = self.get(con, "worker_key")
        if not url:
            raise HTTPException(409, "SaladCloud endpoint is missing")
        headers = {"Accept": "application/json"}
        if gateway:
            headers["Salad-Api-Key"] = gateway
        if api:
            headers["Authorization"] = "Bearer " + api
        try:
            result = requests.get(url + path, headers=headers, timeout=15, allow_redirects=False)
            healthy = result.status_code == 200
            status = "online" if healthy else "http_" + str(result.status_code)
        except requests.RequestException:
            healthy, status = False, "unreachable"
        if connect:
            with self.connection() as con:
                self.put(con, "enabled", int(healthy))
        return {"ok": healthy, "status": status, "connected": healthy if connect else self.config()["enabled"]}

    def disconnect(self):
        with self.connection() as con:
            self.put(con, "enabled", "0")
        return {"ok": True}

    def local(self, job):
        if not ID_RE.fullmatch(str(job)):
            raise HTTPException(400, "Invalid job ID")
        return self.media / (job + ".mp4")

    def listing(self):
        with self.connection() as con:
            rows = con.execute("SELECT id,name,size,received,state,drive_path,error,created_at FROM files ORDER BY created_at DESC,id DESC LIMIT 500").fetchall()
        return {"items": [dict(r) for r in rows], "auto_upload": self.config()["auto_upload"]}

    def receive(self, job, index, data, *, name, size, sha):
        final = self.local(job)
        try: size, index = int(size), int(index)
        except (TypeError, ValueError): raise HTTPException(400, "Invalid chunk index or size") from None
        if not 0 < size <= MEDIA_MAX or index < 0 or index > 100000 or not SHA_RE.fullmatch(str(sha)):
            raise HTTPException(400, "Invalid size, index or SHA256")
        if not data or len(data) > CHUNK_MAX:
            raise HTTPException(413, "Chunk must be between 1 byte and 8 MiB")
        name = filename(name)
        part = self.staging / (job + ".part")
        auto = False
        with self.lock, self.connection() as con:
            row = con.execute("SELECT * FROM files WHERE id=?", (job,)).fetchone()
            if row is None:
                if index != 0:
                    raise HTTPException(409, "Start with chunk zero")
                con.execute("INSERT INTO files(id,name,size,sha256,state) VALUES(?,?,?,?,?)",
                            (job,name,size,sha,"receiving"))
                row = con.execute("SELECT * FROM files WHERE id=?", (job,)).fetchone()
            if row["name"] != name or row["size"] != size or row["sha256"] != sha:
                raise HTTPException(409, "Job upload metadata mismatch")
            if row["state"] != "receiving":
                raise HTTPException(409, "Job is not receiving")
            if index < row["chunks"]:
                return {"ok": True, "duplicate": True, "next_chunk": row["chunks"], "received": row["received"]}
            if index != row["chunks"] or part.is_symlink():
                raise HTTPException(409, "Out-of-order or unsafe chunk")
            actual = part.stat().st_size if part.exists() else 0
            if actual != row["received"] or actual + len(data) > size:
                raise HTTPException(409, "Unexpected staged file length")
            with part.open("ab") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            received = actual + len(data)
            con.execute("UPDATE files SET received=?,chunks=? WHERE id=?", (received,index+1,job))
            if received != size:
                return {"ok": True, "state": "receiving", "received": received, "next_chunk": index+1}
            digest, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
            with part.open("rb") as handle:
                header = handle.read(12)
                if len(header) < 12 or header[4:8] != b"ftyp":
                    raise HTTPException(422, "MP4 container signature invalid")
                handle.seek(0)
                for block in iter(lambda: handle.read(4*1024*1024), b""):
                    digest.update(block); md5.update(block)
            if digest.hexdigest() != sha:
                raise HTTPException(422, "MP4 SHA256 mismatch; staged copy retained")
            if final.exists() or final.is_symlink():
                raise HTTPException(409, "Final media path is occupied")
            os.replace(part, final)
            con.execute("UPDATE files SET state='ready',md5=? WHERE id=?", (md5.hexdigest(),job))
            auto = self.get(con, "auto_upload") == "1"
        if auto:
            self.queue(job)
        return {"ok": True, "state": "ready", "received": size, "auto_queued": auto}

    def rename(self, job, new_name):
        self.local(job)
        new_name = filename(new_name)
        with self.lock, self.connection() as con:
            row = con.execute("SELECT state,size FROM files WHERE id=?", (job,)).fetchone()
            if not row: raise HTTPException(404, "File not found")
            if row["state"] not in ("ready","failed"):
                raise HTTPException(409, "File busy or already delivered")
            con.execute("UPDATE files SET name=? WHERE id=?", (new_name,job))
        return {"ok": True}

    def delete(self, job):
        local = self.local(job)
        with self.lock, self.connection() as con:
            row = con.execute("SELECT state FROM files WHERE id=?", (job,)).fetchone()
            if not row: raise HTTPException(404, "File not found")
            if row["state"] in ("uploading","queued"):
                raise HTTPException(409, "Cannot delete during Drive transfer")
            source = self.staging / (job+".part") if row["state"] == "receiving" else local
            if source.is_symlink(): raise HTTPException(409, "Unsafe file path")
            source.unlink(missing_ok=True)
            con.execute("DELETE FROM files WHERE id=?", (job,))
        return {"ok": True}

    def queue(self, job):
        local = self.local(job)
        with self.lock, self.connection() as con:
            row = con.execute("SELECT state,size FROM files WHERE id=?", (job,)).fetchone()
            if not row: raise HTTPException(404, "File not found")
            if row["state"] not in ("ready","failed"):
                raise HTTPException(409, "File is not ready for upload")
            if local.is_symlink() or not local.is_file() or local.stat().st_size != row["size"]:
                raise HTTPException(409, "Local MP4 missing or changed")
            con.execute("UPDATE files SET state='queued',error='' WHERE id=?", (job,))
        self.pool.submit(self._upload, job)
        return {"ok": True, "state": "queued"}

    @staticmethod
    def execute(cmd, timeout=1200):
        operation = cmd[1] if len(cmd) > 1 else "command"
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Drive Bridge {operation} timed out; VPS original retained") from None
        except OSError:
            raise RuntimeError(f"Drive Bridge {operation} unavailable; VPS original retained") from None
        if res.returncode:
            raise classify_rclone_error(operation, res.returncode, res.stderr)
        return res.stdout

    def remote_stat(self, remote):
        try:
            return json.loads(self.execute(["rclone","lsjson",remote,"--stat","--hash"],60))
        except RcloneFailure as exc:
            if exc.operation == "lsjson" and exc.category == "remote_file_absent":
                return None
            raise
        except ValueError:
            raise RuntimeError("Drive Bridge lsjson returned invalid JSON; VPS original retained") from None

    def _upload(self, job):
        local = self.local(job)
        with self.lock, self.connection() as con:
            row = con.execute("SELECT * FROM files WHERE id=?", (job,)).fetchone()
            if not row or row["state"] != "queued":
                return
            con.execute("UPDATE files SET state='uploading' WHERE id=?", (job,))
        try:
            remote_name = os.getenv("CLOUD_RENDER_DRIVE_REMOTE", "gdrive")
            folder = os.getenv("CLOUD_RENDER_DRIVE_FOLDER", "Cloud Render")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}",remote_name) or not re.fullmatch(r"[A-Za-z0-9_ /-]{1,120}",folder):
                raise RuntimeError("Invalid Drive Bridge destination")
            if not shutil.which("rclone"):
                raise RuntimeError("Canonical Drive Bridge rclone executable is not available")
            # Only use the configured canonical remote, under this process identity.
            remotes = {line.strip().rstrip(":") for line in
                       self.execute(["rclone","listremotes"],30).splitlines()}
            if remote_name not in remotes:
                raise RuntimeError(f'Canonical Drive Bridge remote "{remote_name}" is not configured for Cloud Render service')
            if local.is_symlink() or not local.is_file() or local.stat().st_size != row["size"]:
                raise RuntimeError("Source file absent or changed")
            remote = f"{remote_name}:{folder}/{job}_{filename(row['name'])}"
            info = self.remote_stat(remote)
            if info is None:
                self.execute(["rclone","copyto",str(local),remote,"--immutable"],3600)
                info = self.remote_stat(remote)
            if not isinstance(info,dict) or info.get("Size") != row["size"]:
                raise RuntimeError("Remote size verification failed; VPS original retained")
            hashes = info.get("Hashes") or {}
            remote_md5 = hashes.get("MD5") or hashes.get("md5") or ""
            if not remote_md5 or not hmac.compare_digest(remote_md5.lower(),row["md5"].lower()):
                raise RuntimeError("Remote MD5 verification failed; VPS original retained")
            # Record the verified remote before attempting deletion, for crash recovery.
            with self.lock, self.connection() as con:
                con.execute("UPDATE files SET state='verified',drive_path=?,error='' WHERE id=?", (remote,job))
            if local.is_symlink():
                raise RuntimeError("Unsafe media path; cleanup refused")
            local.unlink()
            with self.lock, self.connection() as con:
                con.execute("UPDATE files SET state='delivered' WHERE id=?", (job,))
        except Exception as exc:
            with self.lock, self.connection() as con:
                con.execute("UPDATE files SET state='failed',error=? WHERE id=?",
                            (str(exc)[:250],job))


def register_cloud_render(app, templates, root, require_admin, require_login):
    from fastapi.responses import HTMLResponse, FileResponse
    manager = CloudRender(Path(root)/"cloud-render")

    def admin(req):
        require_login(req)
        require_admin(req)

    def csrf(req):
        admin(req)
        expected = str(req.session.get("cloud_render_csrf") or "")
        actual = str(req.headers.get("X-Cloud-Render-CSRF") or "")
        if not expected or not hmac.compare_digest(expected,actual):
            raise HTTPException(403, "Cloud Render CSRF token required")

    @app.get("/cloud-render/",response_class=HTMLResponse)
    def home(request: Request):
        admin(request)
        if not request.session.get("cloud_render_csrf"):
            request.session["cloud_render_csrf"] = secrets.token_urlsafe(32)
        return templates.TemplateResponse(request, "cloud_render.html",
                 {"page": "cloud_render",
                  "username": request.session.get("username","team"),
                  "csrf_token": request.session["cloud_render_csrf"]})

    @app.get("/api/cloud-render/config")
    def config(request: Request):
        admin(request); return manager.config()

    @app.post("/api/cloud-render/config")
    async def save(request: Request):
        csrf(request)
        return manager.save(await request.json())

    @app.post("/api/cloud-render/token/rotate")
    def token_rotate(request: Request):
        csrf(request); return manager.rotate_token()

    @app.post("/api/cloud-render/gpu/test")
    def gpu_test(request: Request):
        csrf(request); return manager.probe()

    @app.post("/api/cloud-render/gpu/connect")
    def gpu_connect(request: Request):
        csrf(request); return manager.probe(connect=True)

    @app.post("/api/cloud-render/gpu/disconnect")
    def gpu_disconnect(request: Request):
        csrf(request); return manager.disconnect()

    @app.get("/api/cloud-render/media")
    def list_media(request: Request):
        admin(request); return manager.listing()

    @app.post("/api/cloud-render/media/{job}/rename")
    async def rename(request: Request,job: str):
        csrf(request); obj=await request.json()
        return manager.rename(job,obj.get("name",""))

    @app.post("/api/cloud-render/media/{job}/drive")
    def upload(request: Request,job: str):
        csrf(request); return manager.queue(job)

    @app.delete("/api/cloud-render/media/{job}")
    def delete(request: Request,job: str):
        csrf(request); return manager.delete(job)

    @app.get("/api/cloud-render/media/{job}/file")
    def download(request: Request,job: str):
        admin(request)
        local = manager.local(job)
        with manager.connection() as con:
            row = con.execute("SELECT name,state FROM files WHERE id=?",(job,)).fetchone()
        if not row or row["state"] not in ("ready","failed","queued") or not local.is_file() or local.is_symlink():
            raise HTTPException(404,"Local MP4 unavailable")
        return FileResponse(local,media_type="video/mp4",filename=row["name"],content_disposition_type=("attachment" if request.query_params.get("download") == "1" else "inline"),
                            headers={"Cache-Control":"private,no-store","X-Content-Type-Options":"nosniff"})

    @app.post("/api/cloud-render/ingest/{job}/chunk")
    async def ingest(request: Request,job: str,index: int,name: str,size: int,sha256: str):
        manager.token_required(request.headers.get("X-Cloud-Render-Token",""))
        # Stream and cap size before buffering; never permit arbitrary file paths.
        out=bytearray()
        async for chunk in request.stream():
            if len(out)+len(chunk)>CHUNK_MAX:
                raise HTTPException(413,"Chunk too large")
            out.extend(chunk)
        return manager.receive(job,index,bytes(out),name=name,size=size,sha=sha256)

    return manager

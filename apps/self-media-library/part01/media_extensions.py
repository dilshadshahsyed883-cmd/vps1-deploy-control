"""Self Media Library V2 extensions. Additive; V1 clip endpoints/data remain intact."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from media_integrity import sha256_file
from media_catalog import probe_video, safe_origin, classify_orientation

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024
IMPORT_LOCK = threading.Lock()


def normalized_tags(tags):
    out = []
    for raw in tags:
        tag = re.sub(r"\s+", " ", str(raw).strip().lower())[:60]
        if tag and tag not in out:
            out.append(tag)
    return out[:30]


def rank_candidates(items, terms, last_source="", used_ids=()):
    """Stable, explainable editorial relevance + variety. Never selects unapproved items."""
    used = set(used_ids)
    def score(item):
        keywords = set(item.get("keywords") or [])
        body = (item.get("description") or "").lower()
        name = (item.get("source_filename") or "").lower()
        matching = sum(4 if term in keywords else 2 if term in body else 1 if term in name else 0 for term in terms)
        return (
            int(item.get("source_id") != last_source),
            int(item["id"] not in used),
            matching,
            int(item.get("quality") or 0),
            str(item["id"]),
        )
    return sorted(items, key=score, reverse=True)


class ImageEdit(BaseModel):
    status: str | None = None
    keywords: list[str] | None = None
    description: str | None = None
    display_name: str | None = None


class ImportRequest(BaseModel):
    source: str
    value: str
    rights_confirmed: bool = False
    use_proxy: bool = False


class ReservationRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    scene_id: str = Field(min_length=1, max_length=100)
    query: str = Field(default="", max_length=200)
    media_type: str = "video"
    count: int = Field(default=1, ge=1, le=20)
    exclude_ids: list[str] = Field(default_factory=list)


def register_media_extensions(app, *, data_dir, db, now_iso, get_settings_map,
                              setting_true, drive_base, drive_preflight,
                              run_rclone, upload_and_verify, source_dir):
    root = Path(data_dir)
    images = root / "images"
    previews = root / "image_previews"
    cache = root / "media_cache"
    for path in (images, previews, cache):
        path.mkdir(parents=True, exist_ok=True)

    @app.on_event("startup")
    def init_media_v2():
        with db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS image_assets (
                    id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    thumb_name TEXT,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'candidate',
                    drive_state TEXT NOT NULL DEFAULT 'local_only',
                    drive_file_path TEXT,
                    drive_thumb_path TEXT,
                    drive_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_images_sha ON image_assets(sha256);
                CREATE TABLE IF NOT EXISTS media_reservations (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    scene_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    media_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    committed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_media_project ON media_reservations(project_id,state,created_at);
                CREATE TABLE IF NOT EXISTS media_imports (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    source_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            for column, definition in {
                "source_kind": "TEXT NOT NULL DEFAULT 'legacy'",
                "source_locator": "TEXT NOT NULL DEFAULT ''",
                "rights_confirmed": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if column not in {col["name"] for col in conn.execute("PRAGMA table_info(image_assets)")}:
                    conn.execute(f"ALTER TABLE image_assets ADD COLUMN {column} {definition}")
            conn.execute("UPDATE media_imports SET status='failed',message='Interrupted by restart',updated_at=? WHERE status='processing'", (now_iso(),))

    def check_editor_token(value: str):
        expected = os.environ.get("SML_EDITOR_API_TOKEN", "").strip()
        if not expected:
            raise HTTPException(503, "Editor API token not configured; set SML_EDITOR_API_TOKEN server-side")
        if not hmac.compare_digest(value, expected):
            raise HTTPException(401, "Invalid editor API token")

    def image_dict(row):
        asset = dict(row)
        asset["type"] = "image"
        asset["keywords"] = json.loads(row["keywords_json"])
        asset["source_id"] = row["id"]
        asset["source_filename"] = row["original_name"]
        asset["quality"] = 80
        asset["orientation"] = classify_orientation(int(row["width"]), int(row["height"]))
        asset["content_url"] = f"/api/v1/images/{row['id']}/content"
        asset["thumb_url"] = f"/api/v1/images/{row['id']}/preview"
        asset.pop("keywords_json", None)
        return asset

    def ensure_image(row):
        path = images / row["stored_name"]
        if path.is_file():
            return path
        remote = row["drive_file_path"]
        if row["drive_state"] != "verified" or not remote:
            raise HTTPException(404, "Image content is not available")
        try:
            run_rclone(["copyto", remote, str(path)], timeout=180)
        except Exception:
            raise HTTPException(503, "Verified remote image temporarily unavailable")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            path.unlink(missing_ok=True)
            raise HTTPException(503, "Retrieved image failed integrity verification")
        return path

    def persist_image(filename: str, file_path: Path, *, source_kind="local", source_locator="", rights_confirmed=False):
        ext = Path(filename).suffix.lower()
        if ext not in IMAGE_EXT:
            raise ValueError("Unsupported image type")
        size = file_path.stat().st_size
        if not 0 < size <= MAX_IMAGE_BYTES:
            raise ValueError("Image must be between 1 byte and 30 MB")
        magic = file_path.open("rb").read(16)
        if not (magic.startswith(b"\xff\xd8\xff") and ext in {".jpeg",".jpg"}
                or magic.startswith(b"\x89PNG\r\n\x1a\n") and ext == ".png"
                or magic[:4] == b"RIFF" and magic[8:12] == b"WEBP" and ext == ".webp"):
            raise ValueError("Invalid image content")
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        with db() as conn:
            existing = conn.execute("SELECT * FROM image_assets WHERE sha256=? AND deleted_at IS NULL LIMIT 1", (digest,)).fetchone()
        if existing:
            return {"ok": True, "id": existing["id"], "duplicate": True}
        asset_id = "IMG-" + uuid.uuid4().hex[:12].upper()
        name = asset_id + ext
        destination = images / name
        thumb_name = asset_id + ".jpg"
        thumb = previews / thumb_name
        shutil.copyfile(file_path, destination)
        try:
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i",
                            str(destination), "-frames:v", "1",
                            "-vf", "scale='min(480,iw)':-2", str(thumb)],
                           check=True, timeout=50, capture_output=True)
            info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                   "-show_entries", "stream=width,height", "-of", "json",
                                   str(destination)], check=True, timeout=20, capture_output=True, text=True)
            stream = json.loads(info.stdout).get("streams", [{}])[0]
            width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
        except Exception:
            destination.unlink(missing_ok=True)
            thumb.unlink(missing_ok=True)
            raise ValueError("Image could not be decoded")
        ts = now_iso()
        keywords = normalized_tags(Path(filename).stem.replace("_", " ").replace("-", " ").split())
        with db() as conn:
            conn.execute("""INSERT INTO image_assets
                (id,original_name,stored_name,thumb_name,size_bytes,sha256,width,height,
                 keywords_json,created_at,updated_at,source_kind,source_locator,rights_confirmed)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (asset_id,Path(filename).name[:200],name,thumb_name,size,digest,width,height,
                 json.dumps(keywords),ts,ts,source_kind,safe_origin(source_kind,source_locator),
                 int(rights_confirmed)))
        # Failure to sync must preserve the only local copy, never erase it.
        sync_image(asset_id)
        return {"ok": True, "id": asset_id, "duplicate": False}

    def sync_image(asset_id):
        settings = get_settings_map()
        if not setting_true(settings, "drive_enabled"):
            return {"ok": True, "state": "local_only"}
        with db() as conn:
            row = conn.execute("SELECT * FROM image_assets WHERE id=? AND deleted_at IS NULL", (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Image not found")
        try:
            check = drive_preflight(settings)
            if not check.get("ok"):
                raise RuntimeError("Drive preflight did not pass")
            base = drive_base(settings)
            run_rclone(["mkdir", base + "/images"])
            run_rclone(["mkdir", base + "/image_previews"])
            remote = base + "/images/" + row["stored_name"]
            remote_thumb = base + "/image_previews/" + row["thumb_name"]
            upload_and_verify(images / row["stored_name"], remote, verify=True)
            upload_and_verify(previews / row["thumb_name"], remote_thumb, verify=True)
            with db() as conn:
                conn.execute("UPDATE image_assets SET drive_state='verified',drive_file_path=?,drive_thumb_path=?,drive_error=NULL,updated_at=? WHERE id=?",
                             (remote,remote_thumb,now_iso(),asset_id))
            return {"ok": True, "state": "verified"}
        except Exception as exc:
            with db() as conn:
                conn.execute("UPDATE image_assets SET drive_state='sync_failed',drive_error=?,updated_at=? WHERE id=?",
                             (str(exc)[:250],now_iso(),asset_id))
            return {"ok": False, "state": "sync_failed", "detail": str(exc)[:250]}

    @app.post("/api/v1/images/upload")
    async def upload_image(file: UploadFile = File(...)):
        ext = Path(file.filename or "").suffix.lower()
        if ext not in IMAGE_EXT:
            raise HTTPException(400, "Supported: JPG, PNG, WEBP")
        tmp = cache / (uuid.uuid4().hex + ext)
        count = 0
        try:
            with tmp.open("wb") as fp:
                while chunk := await file.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_IMAGE_BYTES:
                        raise HTTPException(413, "30 MB image limit")
                    fp.write(chunk)
            try:
                return persist_image(file.filename or "image" + ext, tmp)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        finally:
            tmp.unlink(missing_ok=True)
            await file.close()

    @app.get("/api/v1/images")
    def list_images(q: str = "", status: str = "all", orientation: str = "all",
                    limit: int = Query(60, ge=1, le=200)):
        with db() as conn:
            rows = conn.execute("SELECT * FROM image_assets WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 500").fetchall()
        items = [image_dict(r) for r in rows]
        if orientation not in {"all", "landscape", "portrait", "square", "unknown"}:
            raise HTTPException(400, "Invalid orientation filter")
        if orientation != "all":
            items = [item for item in items if item["orientation"] == orientation]
        if status != "all":
            items = [x for x in items if x["status"] == status]
        terms = normalized_tags(q.split())
        if terms:
            items = [x for x in items if all(
                t in " ".join(x["keywords"]) or t in (x["description"] or "").lower()
                or t in x["original_name"].lower() for t in terms)]
        return {"items": items[:limit], "total": len(items)}

    @app.patch("/api/v1/images/{asset_id}")
    def edit_image(asset_id: str, body: ImageEdit):
        with db() as conn:
            row = conn.execute("SELECT * FROM image_assets WHERE id=? AND deleted_at IS NULL", (asset_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Image not found")
            status = body.status if body.status is not None else row["status"]
            if status not in {"candidate","approved","rejected"}:
                raise HTTPException(400, "Invalid review status")
            tags = normalized_tags(body.keywords) if body.keywords is not None else json.loads(row["keywords_json"])
            conn.execute("""UPDATE image_assets SET status=?,keywords_json=?,description=?,original_name=?,updated_at=? WHERE id=?""",
                         (status,json.dumps(tags),
                          (body.description if body.description is not None else row["description"])[:1000],
                          (body.display_name if body.display_name is not None else row["original_name"])[:200],
                          now_iso(),asset_id))
        return {"ok": True}

    @app.delete("/api/v1/images/{asset_id}")
    def trash_image(asset_id: str):
        with db() as conn:
            cur = conn.execute("UPDATE image_assets SET deleted_at=?,updated_at=? WHERE id=? AND deleted_at IS NULL",
                               (now_iso(),now_iso(),asset_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "Image not found")
        return {"ok": True, "state": "trash"}

    @app.post("/api/v1/images/{asset_id}/restore")
    def restore_image(asset_id: str):
        with db() as conn:
            cur = conn.execute("UPDATE image_assets SET deleted_at=NULL,updated_at=? WHERE id=?",
                               (now_iso(),asset_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "Image not found")
        return {"ok": True}

    @app.post("/api/v1/images/{asset_id}/drive-sync")
    def retry_image_drive(asset_id: str):
        return sync_image(asset_id)

    @app.get("/api/v1/images/{asset_id}/content")
    def image_content(asset_id: str):
        with db() as conn:
            row = conn.execute("SELECT * FROM image_assets WHERE id=? AND deleted_at IS NULL", (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Image not found")
        return FileResponse(ensure_image(row))

    @app.get("/api/v1/images/{asset_id}/preview")
    def image_preview(asset_id: str):
        with db() as conn:
            row = conn.execute("SELECT * FROM image_assets WHERE id=? AND deleted_at IS NULL", (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Image not found")
        path = previews / row["thumb_name"]
        if path.is_file():
            return FileResponse(path, media_type="image/jpeg")
        return FileResponse(ensure_image(row))

    def lookup_candidates(media_type, terms):
        results = []
        with db() as conn:
            if media_type in ("video","all"):
                clips = conn.execute("""SELECT c.*, s.filename AS original_name FROM clips c
                    LEFT JOIN sources s ON s.id=c.source_id
                    WHERE c.status='approved' AND c.deleted_at IS NULL
                    AND c.duplicate_state != 'duplicate' ORDER BY c.created_at DESC LIMIT 500""").fetchall()
                for row in clips:
                    r = dict(row)
                    r["keywords"] = [x["keyword"] for x in conn.execute("SELECT keyword FROM clip_keywords WHERE clip_id=?", (row["id"],))]
                    path = root / "clips" / row["filename"]
                    if not path.is_file() and not (row["drive_state"] == "verified" and row["drive_clip_path"]):
                        continue
                    results.append({"type":"video","id":row["id"],"source_id":row["source_id"] or row["id"],
                                    "source_filename":row["original_name"] or "", "keywords":r["keywords"],
                                    "description":row["description"],"quality":row["quality"],
                                    "duration":row["duration"], "content_url":f"/api/v1/media/video/{row['id']}/content"})
            if media_type in ("image","all"):
                for row in conn.execute("SELECT * FROM image_assets WHERE status='approved' AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 500"):
                    if not (images / row["stored_name"]).is_file() and not (
                            row["drive_state"] == "verified" and row["drive_file_path"]):
                        continue
                    r = image_dict(row)
                    results.append({"type":"image","id":r["id"],"source_id":r["id"],
                                    "source_filename":r["original_name"],"keywords":r["keywords"],
                                    "description":r["description"],"quality":80,"duration":0,
                                    "width":r["width"],"height":r["height"],
                                    "content_url":r["content_url"]})
        if terms:
            results = [x for x in results if any(
                term in " ".join(x["keywords"]) or term in x["description"].lower()
                or term in x["source_filename"].lower() for term in terms)]
        return results

    @app.get("/api/v1/media/search")
    def media_search(query: str = "", media_type: str = "all",
                     limit: int = Query(30, ge=1, le=100),
                     x_sml_editor_token: str = Header(default="")):
        check_editor_token(x_sml_editor_token)
        if media_type not in ("video","image","all"):
            raise HTTPException(400, "media_type must be video, image or all")
        terms = normalized_tags(query.split())
        ranked = rank_candidates(lookup_candidates(media_type, terms), terms)
        return {"items": ranked[:limit], "total": len(ranked), "query": query}

    @app.post("/api/v1/media/reservations")
    def reserve_media(body: ReservationRequest, x_sml_editor_token: str = Header(default="")):
        check_editor_token(x_sml_editor_token)
        if body.media_type not in ("video","image","all"):
            raise HTTPException(400, "Invalid media type")
        terms = normalized_tags(body.query.split())
        candidates = lookup_candidates(body.media_type, terms)
        now = int(time.time())
        exclusions = set(body.exclude_ids)
        selected = []
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE media_reservations SET state='expired' WHERE state='reserved' AND expires_at<=?", (now,))
            previous = conn.execute("""SELECT source_id FROM media_reservations WHERE project_id=? AND state='committed'
                ORDER BY committed_at DESC, rowid DESC LIMIT 1""", (body.project_id,)).fetchone()
            last_source = previous["source_id"] if previous else ""
            used = {r["media_id"] for r in conn.execute(
                "SELECT media_id FROM media_reservations WHERE project_id=? AND state IN ('committed','reserved')", (body.project_id,))}
            in_use = {r["media_id"] for r in conn.execute(
                "SELECT media_id FROM media_reservations WHERE project_id=? AND state='reserved'", (body.project_id,))}
            ordered = rank_candidates([x for x in candidates if x["id"] not in in_use and x["id"] not in exclusions], terms, last_source, used)
            for item in ordered:
                if len(selected) >= body.count:
                    break
                if item["source_id"] == last_source and any(
                    alt["source_id"] != last_source and alt["id"] not in {s["id"] for s in selected}
                    for alt in ordered):
                    continue
                reservation_id = "RES-" + uuid.uuid4().hex
                conn.execute("""INSERT INTO media_reservations
                    (id,project_id,scene_id,media_type,media_id,source_id,state,created_at,expires_at)
                    VALUES(?,?,?,?,?,?, 'reserved',?,?)""",
                    (reservation_id,body.project_id,body.scene_id,item["type"],item["id"],
                     item["source_id"],now,now+3600))
                selected.append({**item,"reservation_id":reservation_id,"expires_at":now+3600})
                last_source = item["source_id"]
        return {"items": selected, "count": len(selected),
                "shortfall": body.count-len(selected), "project_id": body.project_id}

    def reservation_transition(reservation_id, desired):
        now = int(time.time())
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state,expires_at FROM media_reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Reservation not found")
            if row["state"] == desired:
                return {"ok": True, "state": desired, "idempotent": True}
            if row["state"] != "reserved" or row["expires_at"] <= now:
                raise HTTPException(409, "Reservation is no longer active")
            conn.execute("UPDATE media_reservations SET state=?,committed_at=? WHERE id=?",
                         (desired,now if desired == "committed" else None,reservation_id))
        return {"ok": True, "state": desired}

    @app.post("/api/v1/media/reservations/{reservation_id}/commit")
    def commit_media(reservation_id: str, x_sml_editor_token: str = Header(default="")):
        check_editor_token(x_sml_editor_token)
        return reservation_transition(reservation_id, "committed")

    @app.post("/api/v1/media/reservations/{reservation_id}/release")
    def release_media(reservation_id: str, x_sml_editor_token: str = Header(default="")):
        check_editor_token(x_sml_editor_token)
        return reservation_transition(reservation_id, "released")

    @app.get("/api/v1/media/video/{clip_id}/content")
    def editor_clip_content(clip_id: str, x_sml_editor_token: str = Header(default="")):
        check_editor_token(x_sml_editor_token)
        with db() as conn:
            row = conn.execute("SELECT * FROM clips WHERE id=? AND deleted_at IS NULL AND status='approved'", (clip_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Approved clip not found")
        path = root / "clips" / row["filename"]
        if not path.is_file():
            if row["drive_state"] != "verified" or not row["drive_clip_path"]:
                raise HTTPException(404, "Clip is not available")
            try:
                run_rclone(["copyto", row["drive_clip_path"], str(path)], timeout=180)
            except Exception:
                raise HTTPException(503, "Remote clip temporarily unavailable")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/v1/media/status")
    def media_api_status():
        with db() as conn:
            imgs = conn.execute("SELECT COUNT(*) AS n FROM image_assets WHERE deleted_at IS NULL").fetchone()["n"]
            imports = conn.execute("SELECT COUNT(*) AS n FROM media_imports WHERE status='processing'").fetchone()["n"]
        return {"ok": True, "v2": True,"editor_token_configured": bool(os.getenv("SML_EDITOR_API_TOKEN")),
                "images": imgs, "imports_processing": imports}

    def background_import(job_id, body):
        ext = Path(urlsplit(body.value).path if body.source == "youtube" else body.value).suffix.lower()
        # yt-dlp chooses the actual container; user-supplied suffix is not trusted.
        temp_dir = cache / ("import-" + job_id)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            if not IMPORT_LOCK.acquire(blocking=False):
                raise RuntimeError("Another import is processing; retry shortly")
            try:
                output = temp_dir / "download"
                if body.source == "drive":
                    settings = get_settings_map()
                    if not setting_true(settings,"drive_enabled") or not drive_preflight(settings).get("ok"):
                        raise RuntimeError("Enable and test Self Media Library Drive first")
                    remote = settings.get("drive_remote","gdrive").rstrip(":")
                    value = body.value.strip().lstrip("/")
                    if not value or any(x in ("..","") for x in value.split("/")) or ":" in value or "\\" in value:
                        raise RuntimeError("Invalid Drive relative file path")
                    ext = Path(value).suffix.lower()
                    remote_path = remote + ":" + value
                    remote_info = json.loads(run_rclone(["lsjson", remote_path, "--stat"], timeout=60).stdout or "{}")
                    remote_size = int(remote_info.get("Size", -1))
                    if not 0 < remote_size <= MAX_IMPORT_BYTES:
                        raise RuntimeError("Drive file must be 1B to 2GB and report its size")
                    run_rclone(["copyto",remote_path,str(output)],timeout=900)
                else:
                    parsed = urlsplit(body.value)
                    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
                            "youtube.com","www.youtube.com","m.youtube.com","youtu.be"}:
                        raise RuntimeError("Only HTTPS YouTube video URLs are supported")
                    if not body.rights_confirmed:
                        raise RuntimeError("Media usage rights confirmation is required")
                    tool = shutil.which("yt-dlp")
                    if not tool:
                        raise RuntimeError("yt-dlp is not installed")
                    args = [tool,"--no-playlist","--no-progress","--no-warnings","--max-filesize","1200M",
                            "--max-downloads","1","-f","best[ext=mp4]/best","-o",str(temp_dir/"download.%(ext)s")]
                    proxy = os.getenv("SML_YTDLP_PROXY_URL","") if body.use_proxy else ""
                    if body.use_proxy and not proxy:
                        raise RuntimeError("Proxy profile is not configured")
                    if proxy:
                        args += ["--proxy",proxy]
                    args += [body.value]
                    result = subprocess.run(args,capture_output=True,text=True,timeout=900)
                    if result.returncode:
                        raise RuntimeError("YouTube import failed (access/rights/format or network): " +
                                           result.stderr[-180:].replace("\n"," "))
                    files = [p for p in temp_dir.iterdir() if p.is_file()]
                    if len(files) != 1:
                        raise RuntimeError("Expected one downloaded media file")
                    output = files[0]
                    ext = output.suffix.lower()
                if ext not in IMAGE_EXT | VIDEO_EXT:
                    raise RuntimeError("Unsupported imported media extension")
                if output.stat().st_size > MAX_IMPORT_BYTES or output.stat().st_size == 0:
                    raise RuntimeError("Import size outside permitted 1B–2GB range")
                if ext in IMAGE_EXT:
                    saved = persist_image(
                        output.name if body.source == "youtube" else Path(body.value).name,
                        output, source_kind=body.source, source_locator=body.value,
                        rights_confirmed=body.rights_confirmed,
                    )
                    source_id = saved["id"]
                else:
                    # Deduplicate authorized Drive/YouTube video imports just as browser uploads.
                    fingerprint = sha256_file(output)
                    source_id = "SRC-" + uuid.uuid4().hex[:10].upper()
                    stored = source_id + ext
                    ts = now_iso()
                    technical = probe_video(output)
                    with db() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        prior = conn.execute(
                            "SELECT id,stored_name FROM sources WHERE sha256=? ORDER BY created_at DESC LIMIT 1",
                            (fingerprint,),
                        ).fetchone()
                        if prior and (Path(source_dir) / prior["stored_name"]).is_file():
                            source_id = prior["id"]
                        else:
                            destination = Path(source_dir) / stored
                            shutil.move(str(output), str(destination))
                            try:
                                conn.execute(
                                    """INSERT INTO sources(id,filename,stored_name,size_bytes,status,stage,
                                               progress,created_at,updated_at,sha256,
                                               codec,width,height,fps,orientation,has_audio,
                                               source_kind,source_locator,rights_confirmed)
                                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (source_id, Path(body.value).name[:200] or stored,
                                     stored, destination.stat().st_size,
                                     "queued", "queued", 0, ts, ts, fingerprint,
                                     technical["codec"], technical["width"], technical["height"],
                                     technical["fps"], technical["orientation"], int(technical["has_audio"]),
                                     body.source, safe_origin(body.source, body.value), int(body.rights_confirmed)),
                                )
                            except Exception:
                                destination.unlink(missing_ok=True)
                                raise
                with db() as conn:
                    conn.execute("UPDATE media_imports SET status='completed',message='Imported; video sources require Process',source_id=?,updated_at=? WHERE id=?",
                                 (source_id,now_iso(),job_id))
            finally:
                IMPORT_LOCK.release()
        except Exception as exc:
            with db() as conn:
                conn.execute("UPDATE media_imports SET status='failed',message=?,updated_at=? WHERE id=?",
                             (str(exc)[:350],now_iso(),job_id))
        finally:
            shutil.rmtree(temp_dir,ignore_errors=True)

    @app.post("/api/v1/media/imports")
    def create_import(body: ImportRequest):
        if body.source not in {"drive","youtube"}:
            raise HTTPException(400, "Supported sources: drive or youtube")
        if not body.value.strip() or len(body.value) > 1000:
            raise HTTPException(400, "A source path or URL is required")
        job_id = uuid.uuid4().hex[:14]
        with db() as conn:
            conn.execute("INSERT INTO media_imports(id,source_type,name,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                         (job_id,body.source,body.value[:250],"processing",now_iso(),now_iso()))
        threading.Thread(target=background_import,args=(job_id,body),daemon=True).start()
        return {"ok": True, "job_id": job_id, "status": "processing"}

    @app.get("/api/v1/media/imports")
    def list_imports():
        with db() as conn:
            rows = conn.execute("SELECT id,source_type,name,status,message,source_id,created_at,updated_at FROM media_imports ORDER BY created_at DESC LIMIT 30").fetchall()
        return {"items": [dict(row) for row in rows]}

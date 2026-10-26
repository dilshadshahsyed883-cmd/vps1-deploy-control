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
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from media_integrity import sha256_file
from media_catalog import probe_video, safe_origin, classify_orientation
from manual_people import matching_collections, associated_ids, virtual_tags
from video_merger_downloader import download_youtube_video, proxy_available
import sml_proxy_profiles

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024
IMPORT_LOCK = threading.Lock()


def parse_ytdlp_progress(line: str) -> int | None:
    """Only actual yt-dlp percentages are used for download progress."""
    match = re.search(r"download:\s*(\d{1,3}(?:\.\d+)?)\s*%", line)
    return min(100, max(0, int(float(match.group(1))))) if match else None
# Keep lazy image preview rebuilds bounded when a page opens many old photos.
IMAGE_PREVIEW_SLOTS = threading.BoundedSemaphore(3)
IMAGE_PREVIEW_VERSION = "sharp-v2"



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
            matching + 100 * float(item.get("score") or 0),
            int(item.get("quality") or 0),
            str(item["id"]),
        )
    return sorted(items, key=score, reverse=True)


class ImageEdit(BaseModel):
    status: str | None = None
    keywords: list[str] | None = None
    description: str | None = None
    display_name: str | None = None


class ImageSelection(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class ImageBulkReview(ImageSelection):
    status: str


class ImageBulkKeywords(ImageSelection):
    keyword: str
    action: str


class ImportRequest(BaseModel):
    source: str
    value: str
    rights_confirmed: bool = False
    use_proxy: bool = False


class ImportRetryRequest(BaseModel):
    # Explicit opt-in is required before replacing a saved proxy request
    # with a direct VPS network request.
    direct_without_proxy: bool = False
    with_proxy: bool = False


def sml_proxy_configured() -> bool:
    # Video Merger/Audio selected proxy routes are borrowed READ-ONLY when
    # the owner explicitly opts in; no credentials copied or changed.
    return proxy_available()


class ProxyProfileInput(BaseModel):
    id: str = ""
    name: str = ""
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""


class ProxyProfileId(BaseModel):
    id: str


class ProxyToggle(BaseModel):
    enabled: bool


class ReservationRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    scene_id: str = Field(min_length=1, max_length=100)
    query: str = Field(default="", max_length=200)
    media_type: str = "video"
    semantic: bool = False
    orientation: str = "all"
    count: int = Field(default=1, ge=1, le=20)
    exclude_ids: list[str] = Field(default_factory=list)
    preferred_ids: list[str] = Field(default_factory=list)  # optional exact manual/auto consumer selection


def register_media_extensions(app, *, data_dir, db, now_iso, get_settings_map,
                              setting_true, source_dir):
    root = Path(data_dir)
    images = root / "images"
    previews = root / "image_previews"
    cache = root / "media_cache"
    for path in (images, previews, cache):
        path.mkdir(parents=True, exist_ok=True)

    import_stop = threading.Event()
    import_thread = None

    @app.on_event("startup")
    def init_media_v2():
        nonlocal import_thread
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
            columns = {col['name'] for col in conn.execute('PRAGMA table_info(media_imports)')}
            if 'request_json' not in columns:
                conn.execute("ALTER TABLE media_imports ADD COLUMN request_json TEXT")
            if 'progress' not in columns:
                conn.execute("ALTER TABLE media_imports ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE media_imports SET progress=100 WHERE status='completed' AND progress<100")
            conn.execute("UPDATE media_imports SET status=CASE WHEN request_json IS NULL THEN 'failed' ELSE 'queued' END,message='Recovered after restart',progress=0,updated_at=? WHERE status='processing'", (now_iso(),))
        import_stop.clear()
        import_thread=threading.Thread(target=dispatch_imports,daemon=True)
        import_thread.start()

    @app.on_event("shutdown")
    def stop_imports():
        import_stop.set()
        if import_thread:import_thread.join(timeout=2)

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
        asset["thumb_url"] = f"/api/v1/images/{row['id']}/preview?v={IMAGE_PREVIEW_VERSION}"
        asset.pop("keywords_json", None)
        return asset

    def ensure_image(row):
        path = images / row["stored_name"]
        if path.is_file():
            return path
        raise HTTPException(404, "Local image content is not available; metadata preserved")

    def persist_image(filename: str, file_path: Path, *, source_kind="local", source_locator="", rights_confirmed=False):
        ext = Path(filename).suffix.lower()
        if ext not in IMAGE_EXT:
            raise ValueError("Unsupported image type")
        size = file_path.stat().st_size
        if not 0 < size <= MAX_IMAGE_BYTES:
            raise ValueError("Image must be between 1 byte and 30 MB")
        with file_path.open("rb") as content:
            magic = content.read(16)
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
        thumb_name = f"{asset_id}-{IMAGE_PREVIEW_VERSION}.jpg"
        thumb = previews / thumb_name
        shutil.copyfile(file_path, destination)
        try:
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i",
                            str(destination), "-frames:v", "1",
                            "-vf", "scale='min(1200,iw)':-2:flags=lanczos",
                            "-q:v", "2", "-threads", "1", "-update", "1", str(thumb)],
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
        # Images remain durable on the VPS.
        return {"ok": True, "id": asset_id, "duplicate": False}

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
    def list_images(q: str = "", status: str = "all", orientation: str = "all", date_from: str = "", date_to: str = "",
                    limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0)):
        # Filter, count and paginate inside SQLite instead of materializing
        # every image and thumbnail metadata into the web worker.
        if orientation not in {"all", "landscape", "portrait", "square", "unknown"}:
            raise HTTPException(400, "Invalid orientation filter")
        clauses = ["deleted_at IS NULL"]
        params = []
        if date_from:
            clauses.append("substr(created_at,1,10)>=?")
            params.append(date_from)
        if date_to:
            clauses.append("substr(created_at,1,10)<=?")
            params.append(date_to)
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        if orientation != "all":
            orientation_sql = """CASE
                WHEN width<=0 OR height<=0 THEN 'unknown'
                WHEN width*100>=height*112 THEN 'landscape'
                WHEN width*100<=height*89 THEN 'portrait'
                ELSE 'square' END"""
            clauses.append(f"({orientation_sql})=?")
            params.append(orientation)
        for term in normalized_tags(q.split()):
            # Escape SQL wildcards so user search remains literal substring matching.
            literal = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = "%" + literal + "%"
            clauses.append("""(LOWER(original_name) LIKE ? ESCAPE '\\'
                OR LOWER(description) LIKE ? ESCAPE '\\'
                OR LOWER(keywords_json) LIKE ? ESCAPE '\\'
                OR EXISTS (SELECT 1 FROM sml_person_assets pa
                           JOIN sml_person_collections pc ON pc.id=pa.person_id
                           WHERE pa.media_type='image' AND pa.asset_id=image_assets.id
                           AND (LOWER(pc.name) LIKE ? ESCAPE '\\'
                                OR LOWER(pc.aliases_json) LIKE ? ESCAPE '\\'
                                OR LOWER(pa.action_tags_json) LIKE ? ESCAPE '\\')))""")
            params.extend([pattern] * 6)
        where = " AND ".join(clauses)
        with db() as conn:
            total_images = conn.execute(
                "SELECT COUNT(*) FROM image_assets WHERE deleted_at IS NULL").fetchone()[0]
            total = conn.execute(f"SELECT COUNT(*) FROM image_assets WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM image_assets WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
        return {"items": [image_dict(row) for row in rows], "total": total,
                "total_images": total_images, "limit": limit, "offset": offset}

    @app.get("/api/v1/images/trash")
    def list_trashed_images(limit: int = Query(200, ge=1, le=500)):
        with db() as conn:
            total = conn.execute("SELECT COUNT(*) FROM image_assets WHERE deleted_at IS NOT NULL").fetchone()[0]
            rows = conn.execute("SELECT * FROM image_assets WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT ?", (limit,)).fetchall()
        return {"items": [image_dict(row) for row in rows], "total": total}

    def check_image_batch(conn, raw_ids):
        ids = list(dict.fromkeys(raw_ids))
        if not 1 <= len(ids) <= 500:
            raise HTTPException(400, "Select between 1 and 500 images")
        for ident in ids:
            if not conn.execute("SELECT 1 FROM image_assets WHERE id=? AND deleted_at IS NULL", (ident,)).fetchone():
                raise HTTPException(404, "Image not found or already in Trash: " + ident)
        from local_library import guard_manual, protect
        guard_manual(conn, "image", ids)
        protect(conn, "image", ids)
        return ids

    @app.post("/api/v1/images/bulk/status")
    def bulk_image_status(body: ImageBulkReview):
        if body.status not in {"candidate", "approved", "rejected"}:
            raise HTTPException(400, "Invalid image review status")
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ids = check_image_batch(conn, body.ids)
            stamp = now_iso()
            conn.executemany("UPDATE image_assets SET status=?,updated_at=? WHERE id=?",
                             [(body.status, stamp, ident) for ident in ids])
        return {"ok": True, "updated": len(ids)}

    @app.post("/api/v1/images/bulk/keywords")
    def bulk_image_keywords(body: ImageBulkKeywords):
        if body.action not in {"add", "remove"}:
            raise HTTPException(400, "Invalid keyword operation")
        keyword = normalized_tags([body.keyword])
        if not keyword:
            raise HTTPException(400, "Keyword is required")
        changed = []
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ids = check_image_batch(conn, body.ids)
            stamp = now_iso()
            for ident in ids:
                row = conn.execute("SELECT keywords_json FROM image_assets WHERE id=?", (ident,)).fetchone()
                tags = normalized_tags(json.loads(row["keywords_json"]))
                if body.action == "add":
                    tags = normalized_tags([*tags, keyword[0]])
                else:
                    tags = [tag for tag in tags if tag != keyword[0]]
                conn.execute("UPDATE image_assets SET keywords_json=?,updated_at=? WHERE id=?",
                             (json.dumps(tags), stamp, ident))
                changed.append((ident, tags))
        from ai_workspace import manual_patch
        for ident, tags in changed:
            manual_patch(root, "image", ident, {"keywords": tags})
        return {"ok": True, "updated": len(changed)}

    @app.post("/api/v1/images/bulk/trash")
    def bulk_image_trash(body: ImageSelection):
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ids = check_image_batch(conn, body.ids)
            stamp = now_iso()
            conn.executemany("UPDATE image_assets SET deleted_at=?,updated_at=? WHERE id=?",
                             [(stamp, stamp, ident) for ident in ids])
        return {"ok": True, "trashed": len(ids)}

    @app.patch("/api/v1/images/{asset_id}")
    def edit_image(asset_id: str, body: ImageEdit):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if body.status is not None:
                from local_library import protect
                protect(conn,'image',[asset_id])
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
        from ai_workspace import manual_patch
        fields={}
        if body.keywords is not None:fields["keywords"]=tags
        if body.description is not None:fields["description"]=body.description
        if fields:manual_patch(root,"image",asset_id,fields)
        return {"ok": True}

    @app.delete("/api/v1/images/{asset_id}")
    def trash_image(asset_id: str):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            from local_library import busy, protect
            busy(conn); protect(conn,'image',[asset_id])
            cur = conn.execute("UPDATE image_assets SET deleted_at=?,updated_at=? WHERE id=? AND deleted_at IS NULL",
                               (now_iso(),now_iso(),asset_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "Image not found")
        return {"ok": True, "state": "trash"}

    @app.post("/api/v1/images/{asset_id}/restore")
    def restore_image(asset_id: str):
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            from local_library import guard_manual, protect
            guard_manual(conn, "image", [asset_id])
            protect(conn, "image", [asset_id])
            cur = conn.execute("UPDATE image_assets SET deleted_at=NULL,updated_at=? WHERE id=? AND deleted_at IS NOT NULL",
                               (now_iso(),asset_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "Trashed image not found")
        return {"ok": True}

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
            # Preview is available in Trash for the owner's restore decision;
            # original content delivery remains restricted to non-trashed assets.
            row = conn.execute("SELECT * FROM image_assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Image not found")
        # Existing Drive-era photos retain their original bytes. Rebuild only a
        # versioned, high-quality local cache; never upscale the source image.
        high_quality = previews / f"{row['id']}-{IMAGE_PREVIEW_VERSION}.jpg"
        if high_quality.is_file() and high_quality.stat().st_size:
            return FileResponse(high_quality, media_type="image/jpeg")
        with IMAGE_PREVIEW_SLOTS:
            if high_quality.is_file() and high_quality.stat().st_size:
                return FileResponse(high_quality, media_type="image/jpeg")
            try:
                source = ensure_image(row)
            except HTTPException:
                legacy = previews / (row["thumb_name"] or "")
                if legacy.is_file() and legacy.stat().st_size:
                    return FileResponse(legacy, media_type="image/jpeg")
                raise
            temporary = previews / f".{row['id']}-{uuid.uuid4().hex}.jpg"
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-i", str(source), "-frames:v", "1",
                     "-vf", "scale='min(1200,iw)':-2:flags=lanczos",
                     "-q:v", "2", "-threads", "1", "-update", "1", str(temporary)],
                    check=True, timeout=50, capture_output=True,
                )
                if not temporary.is_file() or not temporary.stat().st_size:
                    raise ValueError("Empty preview")
                os.replace(temporary, high_quality)
            except (OSError, subprocess.SubprocessError, ValueError):
                # A preview failure must never block or change the durable photo.
                return FileResponse(source)
            finally:
                temporary.unlink(missing_ok=True)
            return FileResponse(high_quality, media_type="image/jpeg")

    def lookup_candidates(media_type, terms):
        """Legacy approved catalog + virtual manual person tags (original tags untouched)."""
        results = []
        with db() as conn:
            named_ids = matching_collections(conn, " ".join(terms))
            recognized_phrases = []
            if named_ids:
                for person_id in named_ids:
                    person = conn.execute(
                        "SELECT name,aliases_json FROM sml_person_collections WHERE id=?",
                        (person_id,),
                    ).fetchone()
                    if person:
                        recognized_phrases.extend([person["name"], *json.loads(person["aliases_json"])])

            if media_type in ("video", "all"):
                clips = list(conn.execute("""SELECT c.*, s.filename AS original_name FROM clips c
                    LEFT JOIN sources s ON s.id=c.source_id
                    WHERE c.status='approved' AND c.deleted_at IS NULL
                    AND c.duplicate_state != 'duplicate' ORDER BY c.created_at DESC LIMIT 500"""))
                known = {r["id"] for r in clips}
                for ident in associated_ids(conn, "video", named_ids):
                    if ident not in known:
                        row = conn.execute("""SELECT c.*, s.filename AS original_name FROM clips c
                            LEFT JOIN sources s ON s.id=c.source_id
                            WHERE c.id=? AND c.status='approved' AND c.deleted_at IS NULL
                            AND c.duplicate_state != 'duplicate'""", (ident,)).fetchone()
                        if row:
                            clips.append(row)
                            known.add(ident)
                for row in clips:
                    r = dict(row)
                    r["keywords"] = [x["keyword"] for x in conn.execute(
                        "SELECT keyword FROM clip_keywords WHERE clip_id=?", (row["id"],))]
                    path = root / "clips" / row["filename"]
                    if not path.is_file():
                        continue
                    results.append({"type": "video", "id": row["id"],
                                    "source_id": row["source_id"] or row["id"],
                                    "source_filename": row["original_name"] or "",
                                    "keywords": r["keywords"], "description": row["description"],
                                    "quality": row["quality"], "duration": row["duration"],
                                    "content_url": f"/api/v1/media/video/{row['id']}/content",
                                    "thumb_url": f"/media/thumbs/{row['thumb_filename']}" if row["thumb_filename"] else "",
                                    "sha256": row["sha256"]})
            if media_type in ("image", "all"):
                image_rows = list(conn.execute(
                    "SELECT * FROM image_assets WHERE status='approved' AND deleted_at IS NULL "
                    "ORDER BY created_at DESC LIMIT 500"))
                known = {r["id"] for r in image_rows}
                for ident in associated_ids(conn, "image", named_ids):
                    if ident not in known:
                        row = conn.execute(
                            "SELECT * FROM image_assets WHERE id=? AND status='approved' AND deleted_at IS NULL",
                            (ident,)).fetchone()
                        if row:
                            image_rows.append(row)
                            known.add(ident)
                for row in image_rows:
                    if not (images / row["stored_name"]).is_file():
                        continue
                    r = image_dict(row)
                    results.append({"type": "image", "id": r["id"], "source_id": r["id"],
                                    "source_filename": r["original_name"], "keywords": r["keywords"],
                                    "description": r["description"], "quality": 80, "duration": 0,
                                    "width": r["width"], "height": r["height"],
                                    "content_url": f"/api/v1/media/image/{r['id']}/content",
                                    "sha256": r["sha256"]})

            # Virtual person/action keywords: visible to existing Scene worker with
            # no changes to legacy image keywords or clip_keywords tables.
            membership_index = {}
            for kind in ("video", "image"):
                kind_items = [x for x in results if x["type"] == kind]
                if not kind_items:
                    continue
                tags, memberships = virtual_tags(conn, kind, [x["id"] for x in kind_items])
                for item in kind_items:
                    item["keywords"] = list(dict.fromkeys([*item["keywords"], *tags.get(item["id"], [])]))
                    membership_index[item["id"]] = memberships.get(item["id"], set())

        if named_ids:
            # When a named person is explicitly requested, a shot for another
            # named person must not match merely because both share "playing".
            def belongs_to_requested_person(item):
                links = membership_index.get(item["id"], set())
                if links:
                    return bool(links & named_ids)
                legacy = " ".join([*item["keywords"], item["source_filename"], item["description"]]).casefold()
                return any(re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", legacy)
                           for name in recognized_phrases)
            results = [item for item in results if belongs_to_requested_person(item)]
        if terms:
            results = [x for x in results if any(
                term in " ".join(str(k).casefold() for k in x["keywords"])
                or term in (x["description"] or "").casefold()
                or term in (x["source_filename"] or "").casefold()
                for term in terms)]
        return results

    @app.get("/api/v1/media/inventory")
    def editor_media_inventory(x_sml_editor_token: str = Header(default="")):
        check_editor_token(x_sml_editor_token)
        counts = {}
        with db() as conn:
            for kind, table in (("video", "clips"), ("image", "image_assets")):
                grouped = conn.execute(
                    f"SELECT status, COUNT(*) AS n FROM {table} WHERE deleted_at IS NULL GROUP BY status"
                ).fetchall()
                statuses = {str(row["status"]): int(row["n"]) for row in grouped}
                counts[kind] = {
                    "total": sum(statuses.values()),
                    "approved": statuses.get("approved", 0),
                    "candidate": statuses.get("candidate", 0),
                    "rejected": statuses.get("rejected", 0),
                }
        return {"counts": counts, "eligibility": "Approved media must also be locally available"}

    @app.get("/api/v1/media/search")
    def media_search(query: str = "", media_type: str = "all", semantic: bool = False, orientation: str = "all",
                     limit: int = Query(30, ge=1, le=100),
                     x_sml_editor_token: str = Header(default="")):
        check_editor_token(x_sml_editor_token)
        if media_type not in ("video","image","all"):
            raise HTTPException(400, "media_type must be video, image or all")
        if semantic:
            return app.state.ai_workspace.search(query=query,media_type=media_type,status="approved",orientation=orientation,rights_only=True,limit=limit)
        terms = normalized_tags(query.split())
        ranked = rank_candidates(lookup_candidates(media_type, terms), terms)
        return {"items": ranked[:limit], "total": len(ranked), "query": query}

    @app.post("/api/v1/media/reservations")
    def reserve_media(body: ReservationRequest, x_sml_editor_token: str = Header(default="")):
        check_editor_token(x_sml_editor_token)
        if body.media_type not in ("video","image","all"):
            raise HTTPException(400, "Invalid media type")
        terms = normalized_tags(body.query.split())
        if body.semantic:
            candidates=app.state.ai_workspace.search(query=body.query,media_type=body.media_type,status="approved",orientation=body.orientation,rights_only=True,limit=100)["items"]
        else:
            candidates = lookup_candidates(body.media_type, terms)
        if body.preferred_ids:
            # Exact ID selection is permitted only for approved and available
            # candidates returned by the same server-side lookup policy.
            preferred = set(body.preferred_ids[:20])
            candidates = [x for x in lookup_candidates(body.media_type, []) if x["id"] in preferred]
        now = int(time.time())
        exclusions = set(body.exclude_ids)
        selected = []
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE media_reservations SET state='expired' WHERE state='reserved' AND expires_at<=?", (now,))
            existing=conn.execute("SELECT * FROM media_reservations WHERE project_id=? AND scene_id=? AND state IN ('reserved','committed') ORDER BY created_at LIMIT ?",(body.project_id,body.scene_id,body.count)).fetchall()
            if existing:
                return {"items":[{"id":r["media_id"],"type":r["media_type"],"reservation_id":r["id"],"expires_at":r["expires_at"]} for r in existing],"count":len(existing),"shortfall":max(0,body.count-len(existing)),"project_id":body.project_id,"idempotent":True}
            previous = conn.execute("""SELECT source_id FROM media_reservations WHERE project_id=? AND state='committed'
                ORDER BY committed_at DESC, rowid DESC LIMIT 1""", (body.project_id,)).fetchone()
            last_source = previous["source_id"] if previous else ""
            used = {r["media_id"] for r in conn.execute(
                "SELECT media_id FROM media_reservations WHERE project_id=? AND state IN ('committed','reserved')", (body.project_id,))}
            in_use = {r["media_id"] for r in conn.execute(
                "SELECT media_id FROM media_reservations WHERE project_id=? AND state='reserved'", (body.project_id,))}
            ordered = rank_candidates([x for x in candidates if x["id"] not in used and x["id"] not in exclusions], terms, last_source, used)
            for item in ordered:
                table='clips' if item['type']=='video' else 'image_assets'
                current=conn.execute(f"SELECT * FROM {table} WHERE id=? AND status='approved' AND deleted_at IS NULL",(item['id'],)).fetchone()
                if not current:continue
                if item['type']=='video' and current['local_selection']=='pending':continue
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
            raise HTTPException(404, "Local clip is not available; metadata preserved")
        return FileResponse(path, media_type={
            ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
            ".mkv": "video/x-matroska", ".webm": "video/webm",
            ".avi": "video/x-msvideo",
        }.get(path.suffix.lower(), "application/octet-stream"))

    @app.get("/api/v1/media/image/{asset_id}/content")
    def editor_image_content(asset_id: str, x_sml_editor_token: str = Header(default="")):
        # The worker can fetch only approved images with its server-to-server token.
        check_editor_token(x_sml_editor_token)
        if not re.fullmatch(r"IMG-[A-Za-z0-9_-]+", asset_id):
            raise HTTPException(400, "Invalid approved image ID")
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM image_assets WHERE id=? AND status='approved' AND deleted_at IS NULL",
                (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Approved image not found")
        return FileResponse(ensure_image(row))

    @app.get("/api/v1/media/status")
    def media_api_status():
        with db() as conn:
            imgs = conn.execute("SELECT COUNT(*) AS n FROM image_assets WHERE deleted_at IS NULL").fetchone()["n"]
            imports = conn.execute("SELECT COUNT(*) AS n FROM media_imports WHERE status='processing'").fetchone()["n"]
        return {"ok": True, "v2": True,"editor_token_configured": bool(os.getenv("SML_EDITOR_API_TOKEN")),
                "images": imgs, "imports_processing": imports,
                "proxy_configured": sml_proxy_configured()}

    # SML-only proxy profiles: same controls as Video Merger, independent data.
    # Never serve credential values, and never alter Video Merger's profile file.
    @app.get("/api/v1/media/proxy")
    def proxy_settings():
        return sml_proxy_profiles.state()

    @app.post("/api/v1/media/proxy/save")
    def proxy_save(body: ProxyProfileInput):
        try:
            pid = sml_proxy_profiles.save(body.model_dump())
            return {"ok": True, "id": pid}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/v1/media/proxy/active")
    def proxy_activate(body: ProxyProfileId):
        try:
            sml_proxy_profiles.set_active(body.id)
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/v1/media/proxy/toggle")
    def proxy_toggle(body: ProxyToggle):
        try:
            sml_proxy_profiles.toggle(body.enabled)
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/v1/media/proxy/delete")
    def proxy_delete(body: ProxyProfileId):
        try:
            sml_proxy_profiles.delete(body.id)
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/v1/media/proxy/test")
    def proxy_test(body: ProxyProfileId):
        try:
            return sml_proxy_profiles.test(body.id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def report_import(job_id, percent, message):
        with db() as conn:
            conn.execute("UPDATE media_imports SET progress=?,message=?,updated_at=? WHERE id=?",
                         (max(0, min(99, int(percent))), message[:350], now_iso(), job_id))

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
                if body.source != 'youtube':
                    raise RuntimeError('Drive ingestion retired; use local upload')
                else:
                    parsed = urlsplit(body.value)
                    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
                            "youtube.com","www.youtube.com","m.youtube.com","youtu.be"}:
                        raise RuntimeError("Only HTTPS YouTube video URLs are supported")
                    if not body.rights_confirmed:
                        raise RuntimeError("Media usage rights confirmation is required")
                    # Reuse Video Merger's proven video-only downloader strategy
                    # in an independent SML worker; do not touch VM's queue/DB.
                    output = download_youtube_video(
                        body.value, temp_dir,
                        lambda pct, message: report_import(job_id, pct, message),
                        use_proxy=body.use_proxy, timeout=900,
                    )
                    report_import(job_id, 93, "Validating downloaded video")
                    if not output.is_file():
                        raise RuntimeError("Downloader returned no verified video")
                    ext = output.suffix.lower()
                if ext not in IMAGE_EXT | VIDEO_EXT:
                    raise RuntimeError("Unsupported imported media extension")
                if output.stat().st_size > MAX_IMPORT_BYTES or output.stat().st_size == 0:
                    raise RuntimeError("Import size outside permitted 1B–2GB range")
                report_import(job_id, 97, "Saving media to library")
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
                    conn.execute("UPDATE media_imports SET status='completed',progress=100,message='Imported; video sources require Process',source_id=?,updated_at=? WHERE id=?",
                                 (source_id,now_iso(),job_id))
            finally:
                IMPORT_LOCK.release()
        except Exception as exc:
            with db() as conn:
                conn.execute("UPDATE media_imports SET status='failed',message=?,updated_at=? WHERE id=?",
                             (str(exc)[:350],now_iso(),job_id))
        finally:
            shutil.rmtree(temp_dir,ignore_errors=True)

    def dispatch_imports():
        while not import_stop.wait(1):
            try:
                with db() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    job=conn.execute("SELECT * FROM media_imports WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                    if job:conn.execute("UPDATE media_imports SET status='processing',message='Starting import',progress=1,updated_at=? WHERE id=?",(now_iso(),job['id']))
                if job:background_import(job['id'],ImportRequest(**json.loads(job['request_json'])))
            except Exception:
                # Keep the dispatcher alive; original media remains protected.
                if import_stop.wait(2):return

    @app.post('/api/v1/media/imports/{job_id}/retry')
    def retry_import(job_id: str, body: ImportRetryRequest | None = None):
        direct = bool(body and body.direct_without_proxy)
        with_proxy = bool(body and body.with_proxy)
        if direct and with_proxy:
            raise HTTPException(400, 'Choose direct OR proxy for retry')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM media_imports WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, 'Import not found')
            if row['status'] != 'failed' or not row['request_json']:
                raise HTTPException(409, 'Only a failed saved request can be retried')
            original = ImportRequest(**json.loads(row['request_json']))
            if direct:
                if not original.use_proxy:
                    raise HTTPException(409, 'This job already uses a direct connection')
                # Do not silently bypass user-selected proxy on normal Retry.
                original.use_proxy = False
            elif with_proxy:
                if original.use_proxy:
                    raise HTTPException(409, 'This job already uses the selected proxy')
                if not sml_proxy_configured():
                    raise HTTPException(409, 'Activate a proxy profile first')
                original.use_proxy = True
            elif original.use_proxy and not sml_proxy_configured():
                raise HTTPException(409, 'Proxy profile is not configured. Choose Retry direct to use the VPS connection.')
            if direct or with_proxy:
                message = ('Proxy retry queued (owner selected active proxy)' if with_proxy
                           else 'Direct retry queued (proxy disabled by user)')
                conn.execute("""UPDATE media_imports SET status='queued',progress=0,
                             message=?,request_json=?,updated_at=? WHERE id=?""",
                             (message,original.model_dump_json(),now_iso(),job_id))
            else:
                conn.execute("UPDATE media_imports SET status='queued',progress=0,message='Retry queued',updated_at=? WHERE id=?",
                             (now_iso(), job_id))
        return {'ok': True, 'status': 'queued',
                'direct_without_proxy': direct, 'with_proxy': with_proxy}

    @app.post('/api/v1/media/imports/batch')
    def batch_imports(items:list[ImportRequest]):
        if not 1<=len(items)<=32:raise HTTPException(400,'Use 1 to 32 import requests per batch')
        return {'items':[create_import(body) for body in items]}

    @app.post("/api/v1/media/imports")
    def create_import(body: ImportRequest):
        if body.source != "youtube":
            raise HTTPException(400, "Use local uploads or authorized YouTube sources")
        value = body.value.strip()
        if not value or len(value) > 1000:
            raise HTTPException(400, "A YouTube URL is required (maximum 1000 characters)")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
                "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
            raise HTTPException(400, "Use an HTTPS youtube.com or youtu.be video URL")
        if not body.rights_confirmed:
            raise HTTPException(400, "Media usage rights confirmation is required")
        if body.use_proxy and not sml_proxy_configured():
            raise HTTPException(400, "Proxy profile is not configured. Uncheck Use server proxy to import directly.")
        job_id = uuid.uuid4().hex[:14]
        with db() as conn:
            conn.execute("INSERT INTO media_imports(id,source_type,name,status,message,progress,created_at,updated_at,request_json) VALUES(?,?,?,?,?,?,?,?,?)",
                         (job_id,body.source,value[:250],"queued","Waiting for worker",0,now_iso(),now_iso(),body.model_dump_json()))
        return {"ok": True, "job_id": job_id, "status": "queued"}

    @app.get("/api/v1/media/imports")
    def list_imports():
        with db() as conn:
            rows = conn.execute("SELECT id,source_type,name,status,message,progress,source_id,created_at,updated_at,request_json FROM media_imports ORDER BY created_at DESC LIMIT 30").fetchall()
        items = []
        for row in rows:
            item = dict(row)
            saved = item.pop("request_json", None)  # Do not send saved import bodies to the browser.
            try:
                item["use_proxy"] = bool(json.loads(saved).get("use_proxy")) if saved else False
            except (ValueError, TypeError, AttributeError):
                item["use_proxy"] = False
            items.append(item)
        return {"items": items, "proxy_configured": sml_proxy_configured()}

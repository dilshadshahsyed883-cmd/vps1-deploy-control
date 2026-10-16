from __future__ import annotations

from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .auth import Principal, get_current_principal
from .database import connect, initialize_storage, now_iso
from .shared_fg import SHA256_RE, file_sha256, normalize_fg_name, resolve_shared_fg, shared_fg_root


router = APIRouter(prefix="/api/fg-assets", tags=["shared-fg"])


@router.get("")
def list_fg_assets(_principal: Principal = Depends(get_current_principal)) -> dict:
    initialize_storage()
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            """SELECT name,category,relative_path,sha256,size_bytes,width,height,status,modified_at
               FROM shared_fg_assets ORDER BY category,name"""
        ).fetchall()]
    return {"ok": True, "assets": rows}


@router.post("")
def upload_fg_asset(
    category: str = Form("Global"), name: str = Form(...), sha256: str = Form(...),
    file: UploadFile = File(...),
    _principal: Principal = Depends(get_current_principal),
) -> dict:
    initialize_storage()
    # Shared foregrounds are global and stored in one flat folder. Keep the
    # category form field for backward-compatible Desktop clients.
    safe_category = "Global"
    filename = Path(name).name
    try:
        fg_name = normalize_fg_name(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    expected = sha256.strip().lower()
    if not SHA256_RE.fullmatch(expected):
        raise HTTPException(status_code=400, detail="Invalid FG SHA-256")
    root = shared_fg_root()
    target = (root / fg_name).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        existing = db.execute(
            "SELECT * FROM shared_fg_assets WHERE lower(name)=lower(?) ORDER BY created_at LIMIT 1",
            (fg_name,),
        ).fetchone()
        legacy = db.execute(
            """SELECT * FROM shared_fg_assets
               WHERE lower(name) LIKE lower(?) AND status='available'
               ORDER BY created_at LIMIT 1""",
            (f"%/{filename}",),
        ).fetchone()
    if existing is not None and str(existing["status"]) == "available":
        if str(existing["sha256"]).lower() != expected:
            raise HTTPException(status_code=409, detail="FG name already exists with a different SHA-256")
        try:
            resolve_shared_fg(str(existing["name"]), expected)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "idempotent": True, "asset": dict(existing)}
    if legacy is not None and str(legacy["sha256"]).lower() != expected:
        raise HTTPException(status_code=409, detail="FG filename already exists with different content")

    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.upload-part")
    try:
        received = 0
        with temporary.open("wb") as handle:
            while block := file.file.read(1024 * 1024):
                received += len(block)
                if received > 50 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="FG image exceeds 50 MB")
                handle.write(block)
        if temporary.stat().st_size <= 0:
            raise HTTPException(status_code=400, detail="FG upload is empty")
        actual = file_sha256(temporary)
        if actual != expected:
            raise HTTPException(status_code=409, detail="FG upload checksum mismatch")
        width = height = 0
        try:
            from PIL import Image
            with Image.open(temporary) as image:
                image.verify()
            with Image.open(temporary) as image:
                width, height = image.size
        except ImportError:
            pass
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid FG image: {exc}") from exc
        temporary.replace(target)
        ts = now_iso()
        with connect() as db:
            db.execute(
                """INSERT INTO shared_fg_assets(
                       name,category,relative_path,sha256,size_bytes,width,height,status,modified_at,created_at)
                   VALUES(?,?,?,?,?,?,?,'available',?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     category=excluded.category,relative_path=excluded.relative_path,
                     sha256=excluded.sha256,size_bytes=excluded.size_bytes,
                     width=excluded.width,height=excluded.height,status='available',
                     modified_at=excluded.modified_at""",
                (fg_name,safe_category,fg_name,actual,target.stat().st_size,width,height,ts,ts),
            )
            db.commit()
    finally:
        if temporary.is_file():
            temporary.unlink()
    return {"ok": True, "idempotent": False, "asset": {"name": fg_name, "category": safe_category,
            "sha256": actual, "size_bytes": target.stat().st_size, "width": width, "height": height,
            "status": "available"}}


@router.delete("/{asset_name:path}")
def delete_unused_fg(asset_name: str, principal: Principal = Depends(get_current_principal)) -> dict:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="FG deletion requires Scene Builder administrator access")
    try:
        normalized = normalize_fg_name(asset_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with connect() as db:
        active = db.execute(
            """SELECT COUNT(*) FROM upload_batches WHERE fg_name=?
               AND status NOT IN ('completed','failed','cancelled')""", (normalized,)
        ).fetchone()[0]
        if active:
            raise HTTPException(status_code=409, detail="FG is referenced by an active batch")
        row = db.execute("SELECT * FROM shared_fg_assets WHERE name=?", (normalized,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="FG asset not found")
        target = resolve_shared_fg(normalized, str(row["sha256"]))
        target.unlink()
        db.execute("UPDATE shared_fg_assets SET status='deleted',modified_at=? WHERE name=?", (now_iso(), normalized))
        db.commit()
    return {"ok": True, "name": normalized, "status": "deleted"}

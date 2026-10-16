from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from typing import Any
import uuid

from .database import connect, now_iso
from .drive_uploader import _load_runtime
from .job_states import FAILED, HELD, QUEUED
from .process_manager import ManagedProcessCancelled, run_cancellable_command
from .queue_store import _event, transition_job_in_db


ALLOWED_ROLES = {"audio", "script", "font", "foreground", "source_video"}
ALLOWED_EXTENSIONS = {
    "audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"},
    "script": {".txt"},
    "font": {".ttf", ".otf"},
    "foreground": {".png", ".jpg", ".jpeg", ".webp"},
    "source_video": {".mp4", ".mov", ".mkv", ".webm"},
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DriveIngressError(RuntimeError):
    pass


def incoming_root() -> str:
    return os.environ.get(
        "AUTO_CAPTION_DRIVE_INCOMING_ROOT",
        "Auto Caption Studio/Incoming",
    ).strip().strip("/")


def project_remote_folder(project_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", project_id):
        raise DriveIngressError("Invalid project identifier for Drive ingress")
    return f"{incoming_root()}/{project_id}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rclone_copyto(remote_path: str, destination: Path) -> None:
    config, remote, _ = _load_runtime()
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "rclone", "copyto", f"{remote}:{remote_path}", str(destination),
            "--config", config, "--retries", "3", "--low-level-retries", "10",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=180,
    )
    if result.returncode:
        raise DriveIngressError("Drive metadata download failed: " + result.stdout[-1200:])


def _load_small_remote_json(remote_path: str) -> tuple[dict[str, Any], str]:
    with tempfile.TemporaryDirectory(prefix="auto_caption_drive_manifest_") as folder:
        target = Path(folder) / "document.json"
        _rclone_copyto(remote_path, target)
        if target.stat().st_size > 2 * 1024 * 1024:
            raise DriveIngressError("Drive metadata document is too large")
        raw = target.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise DriveIngressError(f"Invalid Drive JSON document: {exc}") from exc
    if not isinstance(payload, dict):
        raise DriveIngressError("Drive JSON document must be an object")
    return payload, hashlib.sha256(raw).hexdigest()


def validate_remote_submission(project_id: str, owner_id: str) -> dict[str, Any]:
    folder = project_remote_folder(project_id)
    manifest, manifest_sha = _load_small_remote_json(f"{folder}/manifest.json")
    marker, _ = _load_small_remote_json(f"{folder}/upload_complete.json")
    if int(manifest.get("manifest_version") or 0) != 1:
        raise DriveIngressError("Unsupported manifest version")
    if str(manifest.get("project_id") or "") != project_id:
        raise DriveIngressError("Manifest project ID mismatch")
    if str(manifest.get("owner_id") or "") != owner_id:
        raise DriveIngressError("Manifest owner mismatch")
    if str(marker.get("project_id") or "") != project_id:
        raise DriveIngressError("Completion marker project ID mismatch")
    if str(marker.get("manifest_sha256") or "").lower() != manifest_sha:
        raise DriveIngressError("Completion marker does not match manifest")

    files = manifest.get("files")
    if not isinstance(files, list) or not 2 <= len(files) <= 24:
        raise DriveIngressError("Manifest must contain 2 to 24 files")
    normalized: list[dict[str, Any]] = []
    roles: list[str] = []
    seen: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise DriveIngressError("Invalid manifest file record")
        role = str(item.get("role") or "").strip().lower()
        relative = str(item.get("relative_path") or "").strip().replace("\\", "/")
        size = int(item.get("size") or 0)
        checksum = str(item.get("sha256") or "").lower()
        path = PurePosixPath(relative)
        if role not in ALLOWED_ROLES:
            raise DriveIngressError(f"Unsupported Drive file role: {role}")
        if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
            raise DriveIngressError("Unsafe Drive relative path")
        if path.parts[0] not in {"source", "fonts"}:
            raise DriveIngressError("Drive file must be below source/ or fonts/")
        if relative in seen:
            raise DriveIngressError("Duplicate Drive file path")
        if path.suffix.lower() not in ALLOWED_EXTENSIONS[role]:
            raise DriveIngressError(f"Unsupported extension for {role}")
        if size <= 0 or size > 20 * 1024**3:
            raise DriveIngressError("Invalid Drive file size")
        if not SHA256_RE.fullmatch(checksum):
            raise DriveIngressError("Invalid Drive file checksum")
        normalized.append({"role": role, "relative_path": relative, "size": size, "sha256": checksum})
        roles.append(role); seen.add(relative); total += size
    if "script" not in roles or not ({"audio", "source_video"} & set(roles)):
        raise DriveIngressError("Manifest requires a script and audio or source video")
    if int(marker.get("file_count") or -1) != len(normalized):
        raise DriveIngressError("Completion marker file count mismatch")
    if int(marker.get("total_bytes") or -1) != total:
        raise DriveIngressError("Completion marker byte count mismatch")
    settings = manifest.get("settings")
    if not isinstance(settings, dict):
        raise DriveIngressError("Manifest settings must be an object")
    source_mode = str(manifest.get("source_mode") or "automatic_transcription")
    if source_mode not in {"automatic_transcription", "script_captions", "scrolling_text"}:
        raise DriveIngressError("Source mode is not supported in this phase")
    manifest["files"] = normalized
    manifest["settings"] = settings
    manifest["source_mode"] = source_mode
    return {
        "folder": folder,
        "manifest": manifest,
        "manifest_sha256": manifest_sha,
        "file_count": len(normalized),
        "bytes_total": total,
    }


def _lease(seconds: int = 90) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(30, seconds))).isoformat()


def claim_next_ingress(worker_id: str) -> dict[str, Any] | None:
    ts = now_iso()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        stale = db.execute(
            """SELECT * FROM drive_ingress
               WHERE status='downloading' AND (lease_expires_at IS NULL OR lease_expires_at<?)""",
            (ts,),
        ).fetchall()
        for expired in stale:
            db.execute(
                """UPDATE drive_ingress SET status='pending', worker_id='', lease_expires_at=NULL,
                       error_text='Recovered expired Drive download lease', updated_at=? WHERE id=?""",
                (ts, expired["id"]),
            )
            db.execute(
                """UPDATE jobs SET message='Recovered expired Drive download lease',
                       recovery_note='Drive ingress lease expired and was safely requeued', updated_at=?
                   WHERE id=? AND status='held'""",
                (ts, expired["job_id"]),
            )
            _event(
                db, job_id=str(expired["job_id"]), attempt_id=None,
                event_type="drive_ingress_recovered", from_status=HELD, to_status=HELD,
                stage="waiting_for_drive_download", progress=0,
                message="Recovered expired Drive download lease",
                detail={"previous_worker_id": str(expired["worker_id"] or "")},
            )
        row = db.execute(
            """SELECT di.* FROM drive_ingress di JOIN jobs j ON j.id=di.job_id
               WHERE di.status='pending' AND j.status='held'
               ORDER BY di.created_at LIMIT 1"""
        ).fetchone()
        if row is None:
            db.commit(); return None
        db.execute(
            """UPDATE drive_ingress SET status='downloading', worker_id=?,
                   lease_expires_at=?, heartbeat_at=?, error_text='', updated_at=? WHERE id=?""",
            (worker_id, _lease(), ts, ts, row["id"]),
        )
        db.execute(
            """UPDATE jobs SET current_stage='downloading_from_drive', message=?,
                   drive_input_status='downloading', updated_at=? WHERE id=?""",
            ("Downloading verified project files from Drive", ts, row["job_id"]),
        )
        _event(db, job_id=str(row["job_id"]), attempt_id=None,
               event_type="drive_download_claimed", from_status=HELD, to_status=HELD,
               stage="downloading_from_drive", progress=1,
               message="Drive ingress claimed", detail={"worker_id": worker_id})
        db.commit()
    return dict(row)


def _heartbeat(ingress_id: str, job_id: str, worker_id: str, downloaded: int, total: int, message: str) -> None:
    ts = now_iso(); pct = min(18, max(1, int(downloaded * 18 / max(1, total))))
    with connect() as db:
        changed = db.execute(
            """UPDATE drive_ingress SET heartbeat_at=?, lease_expires_at=?,
                   bytes_downloaded=?, updated_at=?
               WHERE id=? AND status='downloading' AND worker_id=?""",
            (ts, _lease(), downloaded, ts, ingress_id, worker_id),
        ).rowcount
        if not changed:
            raise DriveIngressError("Drive ingress lease was lost")
        db.execute(
            """UPDATE jobs SET progress=?, stage_progress=?, current_stage='downloading_from_drive',
                   message=?, updated_at=? WHERE id=? AND status='held'""",
            (pct, min(100, int(downloaded * 100 / max(1, total))), message, ts, job_id),
        )
        db.commit()


def process_ingress(item: dict[str, Any], worker_id: str) -> dict[str, Any]:
    ingress_id = str(item["id"]); job_id = str(item["job_id"])
    manifest = json.loads(str(item["manifest_json"]))
    files = list(manifest["files"]); total = int(item["bytes_total"] or 0)
    with connect() as db:
        project = db.execute("SELECT * FROM projects WHERE id=?", (item["project_id"],)).fetchone()
    if project is None:
        raise DriveIngressError("Drive ingress project no longer exists")
    input_dir = Path(project["project_path"]) / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    config, remote, _ = _load_runtime()
    downloaded = 0

    def cancelled() -> bool:
        with connect() as db:
            row = db.execute("SELECT status,cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row is None or row["status"] == "cancelled" or bool(row["cancel_requested"])

    try:
        for index, file_item in enumerate(files, start=1):
            relative = str(file_item["relative_path"])
            name = Path(PurePosixPath(relative).name).name
            target_name = name
            if file_item["role"] == "font": target_name = f"custom_font_{name}"
            elif file_item["role"] == "foreground": target_name = f"foreground_{name}"
            target = input_dir / target_name
            partial = input_dir / f".{target_name}.drive-part"
            if target.is_file() and target.stat().st_size == int(file_item["size"]) and _sha256(target) == file_item["sha256"]:
                downloaded += target.stat().st_size
                _heartbeat(ingress_id, job_id, worker_id, downloaded, total, f"Verified cached file {index}/{len(files)}")
                continue
            result = run_cancellable_command(
                ["rclone", "copyto", f"{remote}:{item['remote_folder']}/{relative}", str(partial),
                 "--config", config, "--retries", "3", "--low-level-retries", "10"],
                cancel_requested=cancelled,
                heartbeat=lambda: _heartbeat(ingress_id, job_id, worker_id, downloaded, total, f"Downloading file {index}/{len(files)}"),
                timeout=24 * 3600,
                poll_seconds=1.0,
            )
            if result.returncode:
                raise DriveIngressError("rclone input download failed: " + (result.stderr or result.stdout)[-1200:])
            if partial.stat().st_size != int(file_item["size"]) or _sha256(partial) != file_item["sha256"]:
                raise DriveIngressError(f"Downloaded file verification failed: {name}")
            partial.replace(target); downloaded += target.stat().st_size
            _heartbeat(ingress_id, job_id, worker_id, downloaded, total, f"Verified file {index}/{len(files)}")

        with connect() as db:
            job_row = db.execute("SELECT settings_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        # The API may have replaced manifest settings with the batch's frozen
        # settings. Preserve that authoritative copy across Drive download.
        settings = json.loads(str(job_row["settings_json"] or "{}")) if job_row else {}
        metadata = dict(settings.get("metadata") or {})
        for file_item in files:
            name = Path(PurePosixPath(file_item["relative_path"]).name).name
            if file_item["role"] == "font": metadata["custom_font_upload_name"] = f"custom_font_{name}"
            if file_item["role"] == "foreground": metadata["foreground_upload_name"] = f"foreground_{name}"
        settings["metadata"] = metadata
        skip_transcription = not bool(settings.get("caption_mode", True))
        next_pipeline_stage = "ready_render" if skip_transcription else "ready_transcribe"
        ts = now_iso()
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET settings_json=? WHERE id=?", (json.dumps(settings, ensure_ascii=False), job_id))
            transition_job_in_db(db, job_id, QUEUED, stage="queued", progress=0,
                                 message="Drive input verified; queued for rendering",
                                 event_type="drive_input_verified",
                                 detail={"manifest_sha256": item["manifest_sha256"], "bytes": downloaded})
            db.execute(
                """UPDATE jobs SET upload_verified=1, drive_input_status='verified',
                       drive_input_verified=1, pipeline_stage=?, updated_at=? WHERE id=?""",
                (next_pipeline_stage, ts, job_id))
            db.execute(
                """UPDATE drive_ingress SET status='ready', worker_id='', lease_expires_at=NULL,
                       heartbeat_at=?, bytes_downloaded=?, completed_at=?, updated_at=? WHERE id=?""",
                (ts, downloaded, ts, ts, ingress_id),
            )
            db.commit()
        return {"status": "ready", "job_id": job_id, "bytes_downloaded": downloaded}
    except ManagedProcessCancelled:
        with connect() as db:
            db.execute("UPDATE drive_ingress SET status='cancelled',worker_id='',lease_expires_at=NULL,updated_at=? WHERE id=?", (now_iso(), ingress_id)); db.commit()
        return {"status": "cancelled", "job_id": job_id}
    except BaseException as exc:
        ts = now_iso(); error = f"{type(exc).__name__}: {exc}"[-4000:]
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row and row["status"] == HELD:
                transition_job_in_db(db, job_id, FAILED, stage="drive_download_failed",
                                     progress=0, message="Drive input download failed", error_text=error,
                                     event_type="drive_input_failed")
            db.execute("UPDATE jobs SET drive_input_status='failed',drive_input_verified=0,updated_at=? WHERE id=?", (ts, job_id))
            db.execute(
                """UPDATE drive_ingress SET status='failed',worker_id='',lease_expires_at=NULL,
                       error_text=?,updated_at=? WHERE id=?""", (error, ts, ingress_id))
            db.commit()
        return {"status": "failed", "job_id": job_id, "error": error}

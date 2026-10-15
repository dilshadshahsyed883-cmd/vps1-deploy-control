from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any

from .database import connect, initialize_storage
from .process_manager import run_cancellable_command


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_component(value: str, fallback: str) -> str:
    text = INVALID_FILENAME_CHARS.sub("_", str(value or "")).strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    return text[:180] or fallback


def ensure_drive_schema() -> None:
    initialize_storage()


def _load_runtime() -> tuple[str, str, str]:
    config = os.environ.get(
        "AUTO_CAPTION_RCLONE_CONFIG",
        "/home/ubuntu/apps/auto_caption_studio/data/config/rclone.conf",
    )
    remote = os.environ.get("AUTO_CAPTION_RCLONE_REMOTE", "").strip().rstrip(":")
    root = os.environ.get("AUTO_CAPTION_DRIVE_ROOT", "Auto Caption Studio").strip()

    if not remote:
        env_path = Path(
            "/home/ubuntu/apps/auto_caption_studio/data/config/drive_upload.env"
        )
        if env_path.is_file():
            values: dict[str, str] = {}
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip(chr(39)).strip(chr(34))
            config = values.get("AUTO_CAPTION_RCLONE_CONFIG", config)
            remote = values.get("AUTO_CAPTION_RCLONE_REMOTE", remote).rstrip(":")
            root = values.get("AUTO_CAPTION_DRIVE_ROOT", root)

    if not Path(config).is_file():
        raise RuntimeError(f"rclone config missing: {config}")
    if not remote:
        raise RuntimeError("Drive remote name is missing.")
    return config, remote, root


def _run(
    command: list[str],
    *,
    cancel_requested=None,
    heartbeat=None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    if cancel_requested is not None and heartbeat is not None:
        result = run_cancellable_command(
            command,
            cancel_requested=cancel_requested,
            heartbeat=heartbeat,
            timeout=timeout,
            poll_seconds=0.5,
        )
        return subprocess.CompletedProcess(
            args=result.args,
            returncode=result.returncode,
            stdout=(result.stdout or "") + (result.stderr or ""),
            stderr="",
        )
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _remote_exists(
    config: str,
    remote: str,
    path: str,
    *,
    cancel_requested=None,
    heartbeat=None,
) -> bool:
    result = _run([
        "rclone", "lsjson", f"{remote}:{path}",
        "--config", config,
        "--files-only",
        "--no-mimetype",
        "--no-modtime",
    ], cancel_requested=cancel_requested, heartbeat=heartbeat, timeout=120)
    return result.returncode == 0 and bool(result.stdout.strip())


def _choose_remote_path(
    config: str,
    remote: str,
    root: str,
    category: str,
    project_name: str,
    *,
    cancel_requested=None,
    heartbeat=None,
) -> str:
    safe_category = _safe_component(category, "Uncategorized")
    safe_name = _safe_component(project_name, "Untitled Project")

    base_folder = f"{root}/{safe_category}"
    candidate = f"{base_folder}/{safe_name}.mp4"
    if not _remote_exists(
        config, remote, candidate,
        cancel_requested=cancel_requested, heartbeat=heartbeat,
    ):
        return candidate

    number = 2
    while number < 10000:
        candidate = f"{base_folder}/{safe_name} ({number}).mp4"
        if not _remote_exists(
            config, remote, candidate,
            cancel_requested=cancel_requested, heartbeat=heartbeat,
        ):
            return candidate
        number += 1
    raise RuntimeError("Could not allocate a unique Drive filename.")


def upload_completed_job(
    job_id: str,
    local_path: str,
    now_iso_value: str,
    *,
    cancel_requested=None,
    heartbeat=None,
) -> dict[str, Any]:
    ensure_drive_schema()
    source = Path(local_path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError(f"Validated output is missing or empty: {source}")

    with connect() as db:
        row = db.execute(
            """
            SELECT j.*, p.name AS project_name
            FROM jobs j
            JOIN projects p ON p.id=j.project_id
            WHERE j.id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Job not found for Drive upload.")

        if row["drive_verified"] and row["drive_remote_path"]:
            return {
                "remote_path": row["drive_remote_path"],
                "web_url": row["drive_web_url"],
                "verified": True,
                "already_uploaded": True,
            }

        category = row["category"] or "Uncategorized"
        project_name = row["project_name"] or job_id

    config, remote, root = _load_runtime()
    remote_path = _choose_remote_path(
        config, remote, root, category, project_name,
        cancel_requested=cancel_requested,
        heartbeat=heartbeat,
    )

    with connect() as db:
        db.execute(
            """
            UPDATE jobs
            SET drive_status='uploading', drive_remote_path=?,
                drive_error='', updated_at=?
            WHERE id=?
            """,
            (remote_path, now_iso_value, job_id),
        )
        db.commit()

    copy_result = _run([
        "rclone", "copyto", str(source), f"{remote}:{remote_path}",
        "--config", config,
        "--checksum",
        "--retries", "3",
        "--low-level-retries", "10",
        "--stats", "10s",
    ], cancel_requested=cancel_requested, heartbeat=heartbeat)
    if copy_result.returncode != 0:
        raise RuntimeError(
            "rclone upload failed: " + copy_result.stdout[-1500:]
        )

    size_result = _run([
        "rclone", "size", f"{remote}:{remote_path}",
        "--config", config,
        "--json",
    ], cancel_requested=cancel_requested, heartbeat=heartbeat, timeout=180)
    if size_result.returncode != 0:
        raise RuntimeError(
            "Remote verification failed: " + size_result.stdout[-1500:]
        )

    try:
        payload = json.loads(size_result.stdout or "{}")
        remote_size = int(payload.get("bytes", -1))
    except Exception as exc:
        raise RuntimeError(f"Invalid remote verification response: {exc}") from exc

    local_size = source.stat().st_size
    if remote_size != local_size:
        raise RuntimeError(
            f"Remote size mismatch: local={local_size}, remote={remote_size}"
        )

    link_result = _run([
        "rclone", "link", f"{remote}:{remote_path}", "--config", config,
    ], cancel_requested=cancel_requested, heartbeat=heartbeat, timeout=180)
    link_lines = [line.strip() for line in link_result.stdout.splitlines()]
    web_url = next(
        (line for line in reversed(link_lines) if line.startswith(("https://", "http://"))),
        "",
    ) if link_result.returncode == 0 else ""

    with connect() as db:
        db.execute(
            """
            UPDATE jobs
            SET drive_status='completed', drive_remote_path=?, drive_web_url=?,
                drive_error='', drive_uploaded_at=?, drive_verified=1,
                local_cleanup_status='retained', updated_at=?
            WHERE id=?
            """,
            (remote_path, web_url, now_iso_value, now_iso_value, job_id),
        )
        db.commit()

    return {
        "remote_path": remote_path,
        "web_url": web_url,
        "verified": True,
        "local_size": local_size,
        "remote_size": remote_size,
    }


def mark_drive_failure(job_id: str, error: str, now_iso_value: str) -> None:
    ensure_drive_schema()
    with connect() as db:
        db.execute(
            """
            UPDATE jobs
            SET drive_status='failed', drive_error=?,
                drive_verified=0, local_cleanup_status='retained',
                updated_at=?
            WHERE id=?
            """,
            (str(error)[-4000:], now_iso_value, job_id),
        )
        db.commit()

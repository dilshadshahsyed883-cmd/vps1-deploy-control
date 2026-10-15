#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable


VERSION = "2026.08.18-storage-manager-v17.4-intro-music-sfx-autoclean"
LISTEN_HOST = os.getenv("STORAGE_MANAGER_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("STORAGE_MANAGER_PORT", "8799"))
AUTH_TOKEN = os.getenv("STORAGE_MANAGER_TOKEN", "").strip()

STATE_ROOT = Path("/var/lib/vps-storage-manager")
STATE_DB = STATE_ROOT / "storage-manager.sqlite3"
SNAPSHOT_FILE = STATE_ROOT / "latest-snapshot.json"
BACKUP_ROOT = STATE_ROOT / "db-backups"
FULL_CLEAN_STATUS = STATE_ROOT / "full-production-clean-status.json"
FULL_CLEAN_LOG = STATE_ROOT / "full-production-clean.log"
FULL_CLEAN_LOCK = STATE_ROOT / "full-production-clean.lock"
FULL_CLEAN_SERVICE = "vps-production-cleaner.service"

SCENE_ROOT = Path("/var/lib/scene-builder")
SCENE_DB = SCENE_ROOT / "queue.sqlite3"
ACS_ROOT = Path("/home/ubuntu/apps/auto_caption_studio")
ACS_PROJECTS = ACS_ROOT / "data/projects"
ACS_DB = ACS_ROOT / "data/database/auto_caption.db"
ALGROW_ROOT = Path("/home/ubuntu/apps/algrow_voice_bridge/shared")
ALGROW_PROJECTS = ALGROW_ROOT / "projects"
ALGROW_DB = ALGROW_ROOT / "data/algrow_voice.sqlite3"

PIP_CACHE = Path("/home/ubuntu/.cache/pip")
APT_CACHE = Path("/var/cache/apt")
SNAP_CACHE = Path("/var/lib/snapd/cache")
ALGROW_PROFILE = ALGROW_ROOT / "vps-chrome-profile"

PROJECT_CATEGORIES = {"acs_projects", "algrow_projects", "scene_projects"}
CACHE_CATEGORIES = {
    "docker_build_cache", "apt_cache", "pip_cache", "journals",
    "snap_cache", "algrow_browser_cache", "temporary_files",
}
ALL_CATEGORIES = PROJECT_CATEGORIES | CACHE_CATEGORIES

# STORAGE_MANAGER_SERVICE_ACTIVATION_V6
# Fixed allowlist only. The web/API cannot start arbitrary systemd units.
STARTABLE_SERVICES: dict[str, str] = {
    "scene-builder-web": "Scene Builder Web",
    "auto-caption-web": "Auto Caption Web",
    "auto-caption-worker": "Auto Caption Worker",
    "auto-caption-renderer": "Auto Caption Renderer",
    "auto-caption-compressor": "Auto Caption Compressor",
    "auto-caption-drive-uploader": "Auto Caption Drive Uploader",
    "algrow-vps-chrome": "Algrow Chrome",
    "algrow-voice-sidecar": "Algrow Voice Sidecar",
    "algrow-vps-viewer": "Algrow Viewer",
    "algrow-vps-x11vnc": "Algrow VNC Backend",
    "algrow-vps-xvfb": "Algrow Virtual Display",
}

MONITORED_SERVICES: tuple[str, ...] = (
    "scene-builder-web", "auto-caption-web", "auto-caption-worker",
    "auto-caption-renderer", "auto-caption-compressor", "auto-caption-drive-uploader",
    "algrow-vps-chrome", "algrow-voice-sidecar", "algrow-vps-viewer",
    "algrow-vps-x11vnc", "algrow-vps-xvfb", "vps-storage-manager",
)

DEFAULT_POLICY: dict[str, Any] = {
    "auto_enabled": False,
    "auto_mode": "low_space",
    "minimum_free_gb": 20.0,
    "retention_hours": 48,
    "scan_interval_minutes": 10,
    "journal_max_mb": 300,
    "temporary_retention_days": 2,
    "auto_categories": [
        "acs_projects", "algrow_projects", "scene_projects",
        "docker_build_cache", "apt_cache", "pip_cache", "journals",
        "snap_cache", "temporary_files",
    ],
}

TERMINAL_SCENE = {"completed"}
TERMINAL_ACS = {"completed"}
TERMINAL_ALGROW_PROJECT = {"acs_handoff_complete"}
TERMINAL_ALGROW_VOICE = {"voice_ready"}
_operation_lock = threading.Lock()
_scan_lock = threading.Lock()
_snapshot_lock = threading.Lock()
_latest_snapshot: dict[str, Any] = {}
_stop_event = threading.Event()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def clamp_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default


def run(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=timeout, check=False,
    )


def init_state() -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o750)
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o750)
    with sqlite3.connect(STATE_DB) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS operations(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              operation_type TEXT NOT NULL,
              mode TEXT NOT NULL,
              categories_json TEXT NOT NULL,
              status TEXT NOT NULL,
              candidate_bytes INTEGER NOT NULL DEFAULT 0,
              reclaimed_bytes INTEGER NOT NULL DEFAULT 0,
              item_count INTEGER NOT NULL DEFAULT 0,
              details_json TEXT NOT NULL DEFAULT '{}',
              error_text TEXT NOT NULL DEFAULT '',
              started_at TEXT NOT NULL,
              completed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS policy(
              id INTEGER PRIMARY KEY CHECK(id=1),
              value_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT OR IGNORE INTO policy(id,value_json,updated_at) VALUES(1,?,?)",
            (json.dumps(DEFAULT_POLICY), utc_now()),
        )


def load_policy() -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    try:
        with sqlite3.connect(STATE_DB) as db:
            row = db.execute("SELECT value_json FROM policy WHERE id=1").fetchone()
        if row:
            payload = json.loads(row[0])
            if isinstance(payload, dict):
                policy.update(payload)
    except Exception:
        pass
    policy["auto_enabled"] = bool(policy.get("auto_enabled"))
    policy["auto_mode"] = "scheduled" if policy.get("auto_mode") == "scheduled" else "low_space"
    policy["minimum_free_gb"] = clamp_float(policy.get("minimum_free_gb"), 1, 2000, 20)
    policy["retention_hours"] = clamp_int(policy.get("retention_hours"), 1, 8760, 48)
    policy["scan_interval_minutes"] = clamp_int(policy.get("scan_interval_minutes"), 5, 1440, 10)
    policy["journal_max_mb"] = clamp_int(policy.get("journal_max_mb"), 100, 4096, 300)
    policy["temporary_retention_days"] = clamp_int(policy.get("temporary_retention_days"), 1, 90, 2)
    categories = policy.get("auto_categories") or []
    policy["auto_categories"] = sorted({str(x) for x in categories} & ALL_CATEGORIES)
    return policy


def save_policy(payload: dict[str, Any]) -> dict[str, Any]:
    policy = load_policy()
    for key in (
        "auto_enabled", "auto_mode", "minimum_free_gb", "retention_hours",
        "scan_interval_minutes", "journal_max_mb", "temporary_retention_days",
        "auto_categories",
    ):
        if key in payload:
            policy[key] = payload[key]
    policy["auto_enabled"] = bool(policy.get("auto_enabled"))
    policy["auto_mode"] = "scheduled" if policy.get("auto_mode") == "scheduled" else "low_space"
    policy["minimum_free_gb"] = clamp_float(policy.get("minimum_free_gb"), 1, 2000, 20)
    policy["retention_hours"] = clamp_int(policy.get("retention_hours"), 1, 8760, 48)
    policy["scan_interval_minutes"] = clamp_int(policy.get("scan_interval_minutes"), 5, 1440, 10)
    policy["journal_max_mb"] = clamp_int(policy.get("journal_max_mb"), 100, 4096, 300)
    policy["temporary_retention_days"] = clamp_int(policy.get("temporary_retention_days"), 1, 90, 2)
    policy["auto_categories"] = sorted(
        {str(x) for x in (policy.get("auto_categories") or [])} & ALL_CATEGORIES
    )
    with sqlite3.connect(STATE_DB) as db:
        db.execute(
            "UPDATE policy SET value_json=?,updated_at=? WHERE id=1",
            (json.dumps(policy), utc_now()),
        )
    return policy


def parse_time(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def old_enough(value: Any, retention_hours: int) -> bool:
    stamp = parse_time(value)
    return stamp is not None and stamp <= time.time() - retention_hours * 3600


def path_within(path: Path, root: Path) -> Path | None:
    try:
        root_real = root.resolve(strict=True)
        path_real = path.resolve(strict=True)
        if path.is_symlink() or path_real == root_real or root_real not in path_real.parents:
            return None
        return path_real
    except (OSError, RuntimeError):
        return None


def directory_size(path: Path) -> int:
    if not path.exists() or path.is_symlink():
        return 0
    total = 0
    try:
        for base, dirs, files in os.walk(path, topdown=True, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
            for name in files:
                item = Path(base) / name
                try:
                    if not item.is_symlink():
                        total += item.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def paths_size(paths: Iterable[Path]) -> int:
    total = 0
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            total += directory_size(path) if path.is_dir() else (path.stat().st_size if path.is_file() else 0)
        except OSError:
            pass
    return total


def readonly_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def acs_verified_projects(retention_hours: int) -> dict[str, dict[str, Any]]:
    if not ACS_DB.is_file():
        return {}
    with readonly_db(ACS_DB) as db:
        jobs = [dict(row) for row in db.execute(
            """SELECT project_id,status,drive_status,upload_verified,drive_verified,
                      drive_remote_path,drive_uploaded_at,completed_at,local_cleanup_status
               FROM jobs WHERE project_id IS NOT NULL AND project_id!=''"""
        )]
        project_rows = {
            str(row["id"]): dict(row) for row in db.execute(
                "SELECT id,name,project_path,status FROM projects"
            )
        }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in jobs:
        grouped.setdefault(str(row["project_id"]), []).append(row)
    verified: dict[str, dict[str, Any]] = {}
    for project_id, rows in grouped.items():
        if not project_id or Path(project_id).name != project_id:
            continue
        def safe(row: dict[str, Any]) -> bool:
            return (
                str(row.get("status") or "").lower() in TERMINAL_ACS
                and str(row.get("drive_status") or "").lower() == "completed"
                and int(row.get("upload_verified") or 0) == 1
                and int(row.get("drive_verified") or 0) == 1
                and bool(str(row.get("drive_remote_path") or "").strip())
                and bool(str(row.get("drive_uploaded_at") or "").strip())
            )
        if not rows or not all(safe(row) for row in rows):
            continue
        uploaded = max(
            (parse_time(row.get("drive_uploaded_at")) or 0 for row in rows),
            default=0,
        )
        if not old_enough(uploaded, retention_hours):
            continue
        project = project_rows.get(project_id, {})
        configured = Path(str(project.get("project_path") or ""))
        candidate = path_within(configured, ACS_PROJECTS) if configured else None
        expected = ACS_PROJECTS / project_id
        if candidate is None:
            candidate = path_within(expected, ACS_PROJECTS)
        verified[project_id] = {
            "project_id": project_id,
            "name": str(project.get("name") or project_id),
            "path": str(candidate) if candidate else "",
            "uploaded_at": datetime.fromtimestamp(uploaded, timezone.utc).isoformat(),
            "remote_path": str(rows[-1].get("drive_remote_path") or ""),
            "jobs": len(rows),
        }
    return verified


def scan_acs(retention_hours: int) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    verified = acs_verified_projects(retention_hours)
    items = []
    for project_id, record in verified.items():
        path = Path(record["path"]) if record.get("path") else None
        if not path or not path.is_dir():
            continue
        size = directory_size(path)
        if size:
            item = dict(record)
            item["bytes"] = size
            items.append(item)
    items.sort(key=lambda x: int(x["bytes"]), reverse=True)
    return {
        "id": "acs_projects",
        "label": "Auto Caption completed projects",
        "bytes": sum(int(x["bytes"]) for x in items),
        "items": items,
        "item_count": len(items),
        "safety": "Completed + upload verified + Drive verified",
    }, verified


def scan_algrow(retention_hours: int, acs_verified: dict[str, dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if ALGROW_DB.is_file():
        with readonly_db(ALGROW_DB) as db:
            projects = [dict(row) for row in db.execute(
                "SELECT project_id,project_name,local_dir,status,updated_at FROM projects"
            )]
            voices: dict[str, list[str]] = {}
            for row in db.execute("SELECT project_id,status FROM voice_jobs"):
                voices.setdefault(str(row["project_id"]), []).append(str(row["status"] or "").lower())
        for row in projects:
            project_id = str(row.get("project_id") or "")
            voice_states = voices.get(project_id, [])
            if project_id not in acs_verified:
                continue
            if str(row.get("status") or "").lower() not in TERMINAL_ALGROW_PROJECT:
                continue
            if not voice_states or any(state not in TERMINAL_ALGROW_VOICE for state in voice_states):
                continue
            if not old_enough(row.get("updated_at"), retention_hours):
                continue
            configured = Path(str(row.get("local_dir") or ""))
            path = path_within(configured, ALGROW_PROJECTS) if configured else None
            if path is None:
                path = path_within(ALGROW_PROJECTS / project_id, ALGROW_PROJECTS)
            if not path or not path.is_dir():
                continue
            size = directory_size(path)
            if size:
                items.append({
                    "project_id": project_id,
                    "name": str(row.get("project_name") or project_id),
                    "path": str(path),
                    "bytes": size,
                    "handoff_status": str(row.get("status") or ""),
                    "acs_drive_verified": True,
                })
    items.sort(key=lambda x: int(x["bytes"]), reverse=True)
    return {
        "id": "algrow_projects",
        "label": "Algrow projects delivered through Auto Caption",
        "bytes": sum(int(x["bytes"]) for x in items),
        "items": items,
        "item_count": len(items),
        "safety": "Voice ready + ACS handoff complete + ACS Drive verified",
    }


def scene_paths(row: dict[str, Any]) -> list[Path]:
    result: list[Path] = []
    for key in ("input_zip", "output_file"):
        raw = str(row.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = SCENE_ROOT / path
        safe = path_within(path, SCENE_ROOT)
        if safe:
            result.append(safe)
    job_id = str(row.get("id") or "")
    if job_id and Path(job_id).name == job_id:
        job_dir = path_within(SCENE_ROOT / "jobs" / job_id, SCENE_ROOT)
        if job_dir:
            result.append(job_dir)
    return result


def scan_scene(retention_hours: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if SCENE_DB.is_file():
        with readonly_db(SCENE_DB) as db:
            rows = [dict(row) for row in db.execute(
                """SELECT id,project_name,status,drive_status,drive_destination,
                          completed_at,input_zip,output_file,files_cleaned_at
                   FROM jobs"""
            )]
        for row in rows:
            if str(row.get("status") or "").lower() not in TERMINAL_SCENE:
                continue
            if str(row.get("drive_status") or "").lower() != "completed":
                continue
            if not old_enough(row.get("completed_at"), retention_hours):
                continue
            paths = scene_paths(row)
            size = paths_size(paths)
            if size:
                items.append({
                    "project_id": str(row.get("id") or ""),
                    "name": str(row.get("project_name") or row.get("id") or ""),
                    "paths": [str(path) for path in paths],
                    "bytes": size,
                    "uploaded_at": str(row.get("completed_at") or ""),
                    "remote_path": str(row.get("drive_destination") or ""),
                })
    items.sort(key=lambda x: int(x["bytes"]), reverse=True)
    return {
        "id": "scene_projects",
        "label": "Scene Builder completed projects",
        "bytes": sum(int(x["bytes"]) for x in items),
        "items": items,
        "item_count": len(items),
        "safety": "Completed + Drive completed",
    }


def parse_human_size(text: str) -> int:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B|[KMGT])\b", text, re.I)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).upper()
    powers = {
        "B": 0, "K": 1, "KB": 1, "KIB": 1,
        "M": 2, "MB": 2, "MIB": 2,
        "G": 3, "GB": 3, "GIB": 3,
        "T": 4, "TB": 4, "TIB": 4,
    }
    return int(value * (1024 ** powers.get(unit, 0)))


def docker_build_cache_bytes() -> int:
    result = run(["docker", "system", "df"], timeout=30)
    for line in result.stdout.splitlines():
        if line.lower().startswith("build cache"):
            return parse_human_size(line)
    return 0


def journal_bytes() -> int:
    result = run(["journalctl", "--disk-usage"], timeout=30)
    return parse_human_size(result.stdout)


def old_temp_bytes(days: int) -> int:
    threshold = time.time() - days * 86400
    total = 0
    for root in (Path("/tmp"), ACS_ROOT / "logs", ALGROW_ROOT.parent / "logs"):
        if not root.is_dir():
            continue
        for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
            for name in files:
                item = Path(base) / name
                try:
                    stat = item.stat()
                    if not item.is_symlink() and stat.st_mtime < threshold:
                        total += stat.st_size
                except OSError:
                    pass
    return total


def scan_caches(policy: dict[str, Any]) -> list[dict[str, Any]]:
    browser_paths = [
        ALGROW_PROFILE / "Default/Cache",
        ALGROW_PROFILE / "Default/Code Cache",
        ALGROW_PROFILE / "Default/GPUCache",
        ALGROW_PROFILE / "GPUPersistentCache",
    ]
    values = [
        ("docker_build_cache", "Docker build cache", docker_build_cache_bytes(), "Safe; future builds may be slower"),
        ("apt_cache", "APT package cache", directory_size(APT_CACHE), "Downloaded package files only"),
        ("pip_cache", "Python pip cache", directory_size(PIP_CACHE), "Downloaded Python package cache only"),
        ("journals", "System journal above retained limit", max(0, journal_bytes() - int(policy["journal_max_mb"]) * 1024**2), "Keeps the configured newest journal size"),
        ("snap_cache", "Snap download cache", directory_size(SNAP_CACHE), "Downloaded Snap package cache only"),
        ("algrow_browser_cache", "Algrow browser cache", paths_size(browser_paths), "Requires a brief Chrome-only restart"),
        ("temporary_files", "Expired temporary files and logs", old_temp_bytes(int(policy["temporary_retention_days"])), "Age-based allowlisted cleanup"),
    ]
    return [
        {"id": key, "label": label, "bytes": size, "item_count": 1 if size else 0, "items": [], "safety": safety}
        for key, label, size, safety in values
    ]


def _perform_scan_unlocked() -> dict[str, Any]:
    policy = load_policy()
    retention = int(policy["retention_hours"])
    acs, acs_verified = scan_acs(retention)
    categories = [
        acs,
        scan_algrow(retention, acs_verified),
        scan_scene(retention),
        *scan_caches(policy),
    ]
    disk = shutil.disk_usage("/")
    snapshot = {
        "ok": True,
        "version": VERSION,
        "generated_at": utc_now(),
        "policy": policy,
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "percent": round(disk.used / disk.total * 100, 1)},
        "categories": categories,
        "reclaimable_bytes": sum(int(x.get("bytes") or 0) for x in categories),
        "reclaimable_project_bytes": sum(int(x.get("bytes") or 0) for x in categories if x["id"] in PROJECT_CATEGORIES),
        "reclaimable_cache_bytes": sum(int(x.get("bytes") or 0) for x in categories if x["id"] in CACHE_CATEGORIES),
    }
    with _snapshot_lock:
        global _latest_snapshot
        _latest_snapshot = snapshot
        temp = SNAPSHOT_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(SNAPSHOT_FILE)
    return snapshot


def perform_scan() -> dict[str, Any]:
    with _scan_lock:
        return _perform_scan_unlocked()


def latest_snapshot() -> dict[str, Any]:
    with _snapshot_lock:
        if _latest_snapshot:
            return dict(_latest_snapshot)
    try:
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return perform_scan()


def read_cpu() -> tuple[int, int]:
    fields = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def service_status(name: str) -> str:
    result = run(["systemctl", "is-active", name], timeout=10)
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"


def service_failure_details(name: str) -> str:
    chunks: list[str] = []
    status = run(["systemctl", "status", name, "--no-pager", "--full"], timeout=20)
    if status.stdout.strip():
        chunks.append(status.stdout.strip())
    journal = run(["journalctl", "-u", name, "-n", "30", "--no-pager", "--output=short"], timeout=20)
    if journal.stdout.strip():
        chunks.append(journal.stdout.strip())
    value = "\n\n".join(chunks).strip()
    return value[-10000:] if value else "No systemd diagnostic output was returned."


def start_allowlisted_service(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("service") or "").strip()
    if name not in STARTABLE_SERVICES:
        raise ValueError("Service is not allowed for web activation")

    reset_lock = globals().get("FULL_CLEAN_LOCK")
    if isinstance(reset_lock, Path) and reset_lock.exists():
        raise RuntimeError("Full production clean is currently running. Try again after it finishes.")

    before = service_status(name)
    if before == "active":
        return {"ok": True, "service": name, "label": STARTABLE_SERVICES[name], "state": "active", "changed": False,
                "message": f"{STARTABLE_SERVICES[name]} is already active."}

    started = run(["systemctl", "start", name], timeout=90)
    if started.returncode != 0:
        raise RuntimeError(f"{STARTABLE_SERVICES[name]} could not be started.\n\n{service_failure_details(name)}")

    consecutive_active = 0
    last_state = "unknown"
    for _ in range(24):
        last_state = service_status(name)
        if last_state == "active":
            consecutive_active += 1
            if consecutive_active >= 4:
                return {"ok": True, "service": name, "label": STARTABLE_SERVICES[name], "state": "active", "changed": True,
                        "message": f"{STARTABLE_SERVICES[name]} started successfully."}
        else:
            consecutive_active = 0
            if last_state in {"failed", "masked"}:
                break
        time.sleep(0.5)

    raise RuntimeError(
        f"{STARTABLE_SERVICES[name]} did not remain active (final state: {last_state}).\n\n{service_failure_details(name)}"
    )


def metrics() -> dict[str, Any]:
    total1, idle1 = read_cpu()
    time.sleep(0.15)
    total2, idle2 = read_cpu()
    delta = max(1, total2 - total1)
    cpu = round(max(0, min(100, (1 - (idle2 - idle1) / delta) * 100)), 1)
    mem: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        mem[key] = int(value.strip().split()[0]) * 1024
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    disk = shutil.disk_usage("/")
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    top = run(["ps", "-eo", "pid=,comm=,%cpu=,%mem=,rss=", "--sort=-%cpu"], timeout=15)
    processes = []
    for line in top.stdout.splitlines()[:8]:
        parts = line.split(None, 4)
        if len(parts) == 5:
            processes.append({"pid": parts[0], "name": parts[1], "cpu": parts[2], "memory_percent": parts[3], "rss_kb": parts[4]})
    return {
        "ok": True,
        "version": VERSION,
        "generated_at": utc_now(),
        "cpu": {"percent": cpu, "cores": os.cpu_count() or 1, "load": list(os.getloadavg())},
        "memory": {"total": total, "used": max(0, total - available), "available": available, "percent": round((total - available) / total * 100, 1) if total else 0},
        "swap": {"total": swap_total, "used": max(0, swap_total - swap_free)},
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "percent": round(disk.used / disk.total * 100, 1)},
        "uptime_seconds": int(uptime),
        "services": {name: service_status(name) for name in MONITORED_SERVICES},
        "startable_services": sorted(STARTABLE_SERVICES),
        "processes": processes,
    }


def backup_database(source: Path, label: str) -> None:
    if not source.is_file():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = BACKUP_ROOT / f"{label}-{stamp}.sqlite3"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    backups = sorted(BACKUP_ROOT.glob(f"{label}-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[5:]:
        old.unlink(missing_ok=True)


def delete_tree(path: Path, root: Path) -> int:
    safe = path_within(path, root)
    if safe is None:
        raise RuntimeError(f"Refused unsafe path: {path}")
    size = directory_size(safe) if safe.is_dir() else safe.stat().st_size
    if safe.is_dir():
        shutil.rmtree(safe)
    elif safe.is_file():
        safe.unlink()
    return size


def current_acs_safe(project_id: str) -> bool:
    with readonly_db(ACS_DB) as db:
        rows = [dict(row) for row in db.execute(
            """SELECT status,drive_status,upload_verified,drive_verified,
                      drive_remote_path,drive_uploaded_at
               FROM jobs WHERE project_id=?""", (project_id,)
        )]
    return bool(rows) and all(
        str(row.get("status") or "").lower() == "completed"
        and str(row.get("drive_status") or "").lower() == "completed"
        and int(row.get("upload_verified") or 0) == 1
        and int(row.get("drive_verified") or 0) == 1
        and bool(str(row.get("drive_remote_path") or "").strip())
        and bool(str(row.get("drive_uploaded_at") or "").strip())
        for row in rows
    )


def clean_acs(items: list[dict[str, Any]]) -> tuple[int, int, list[str]]:
    if not items:
        return 0, 0, []
    backup_database(ACS_DB, "auto-caption-before-cleanup")
    freed = count = 0
    errors: list[str] = []
    for item in items:
        project_id = str(item.get("project_id") or "")
        path = Path(str(item.get("path") or ""))
        try:
            if not current_acs_safe(project_id):
                raise RuntimeError("database eligibility changed")
            with sqlite3.connect(ACS_DB, timeout=30) as db:
                changed = db.execute(
                    """UPDATE jobs SET local_cleanup_status='cleaning',cleanup_error=''
                       WHERE project_id=? AND status='completed' AND drive_status='completed'
                         AND upload_verified=1 AND drive_verified=1""",
                    (project_id,),
                ).rowcount
                if not changed:
                    raise RuntimeError("no eligible database rows")
            removed = delete_tree(path, ACS_PROJECTS)
            with sqlite3.connect(ACS_DB, timeout=30) as db:
                db.execute(
                    "UPDATE jobs SET local_cleanup_status='cleared',cleanup_error='' WHERE project_id=?",
                    (project_id,),
                )
            freed += removed
            count += 1
        except Exception as exc:
            errors.append(f"ACS {project_id}: {exc}")
            with contextlib.suppress(Exception), sqlite3.connect(ACS_DB, timeout=30) as db:
                db.execute(
                    "UPDATE jobs SET local_cleanup_status='retained',cleanup_error=? WHERE project_id=?",
                    (str(exc)[:1000], project_id),
                )
    return freed, count, errors


def current_algrow_safe(project_id: str) -> bool:
    if not current_acs_safe(project_id):
        return False
    with readonly_db(ALGROW_DB) as db:
        project = db.execute("SELECT status FROM projects WHERE project_id=?", (project_id,)).fetchone()
        voices = db.execute("SELECT status FROM voice_jobs WHERE project_id=?", (project_id,)).fetchall()
    return (
        project is not None
        and str(project[0] or "").lower() == "acs_handoff_complete"
        and bool(voices)
        and all(str(row[0] or "").lower() == "voice_ready" for row in voices)
    )


def clean_algrow(items: list[dict[str, Any]]) -> tuple[int, int, list[str]]:
    freed = count = 0
    errors: list[str] = []
    for item in items:
        project_id = str(item.get("project_id") or "")
        try:
            if not current_algrow_safe(project_id):
                raise RuntimeError("cross-application eligibility changed")
            freed += delete_tree(Path(str(item.get("path") or "")), ALGROW_PROJECTS)
            count += 1
        except Exception as exc:
            errors.append(f"Algrow {project_id}: {exc}")
    return freed, count, errors


def clean_scene(items: list[dict[str, Any]]) -> tuple[int, int, list[str]]:
    if items:
        backup_database(SCENE_DB, "scene-builder-before-cleanup")
    freed = count = 0
    errors: list[str] = []
    for item in items:
        job_id = str(item.get("project_id") or "")
        try:
            with readonly_db(SCENE_DB) as db:
                row = db.execute("SELECT status,drive_status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or str(row[0] or "").lower() != "completed" or str(row[1] or "").lower() != "completed":
                raise RuntimeError("database eligibility changed")
            removed = 0
            for raw in item.get("paths") or []:
                path = Path(str(raw))
                if path.exists():
                    removed += delete_tree(path, SCENE_ROOT)
            with sqlite3.connect(SCENE_DB, timeout=30) as db:
                db.execute("UPDATE jobs SET files_cleaned_at=? WHERE id=?", (utc_now(), job_id))
            freed += removed
            count += 1
        except Exception as exc:
            errors.append(f"Scene Builder {job_id}: {exc}")
    return freed, count, errors


def delete_contents(root: Path) -> int:
    if not root.is_dir() or root.is_symlink():
        return 0
    before = directory_size(root)
    for item in list(root.iterdir()):
        if item.is_symlink():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)
        elif item.is_file():
            item.unlink()
    return before


def cleanup_old_files(root: Path, days: int) -> int:
    if not root.is_dir() or root.is_symlink():
        return 0
    threshold = time.time() - days * 86400
    freed = 0
    for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for name in files:
            item = Path(base) / name
            try:
                stat = item.stat()
                if not item.is_symlink() and stat.st_mtime < threshold:
                    freed += stat.st_size
                    item.unlink()
            except OSError:
                pass
    return freed


def clean_cache(category: str, policy: dict[str, Any]) -> tuple[int, list[str]]:
    errors: list[str] = []
    before = shutil.disk_usage("/").free
    try:
        if category == "docker_build_cache":
            result = run(["docker", "builder", "prune", "-af"], timeout=1800)
            if result.returncode:
                raise RuntimeError(result.stdout[-500:])
        elif category == "apt_cache":
            result = run(["apt-get", "clean"], timeout=300)
            if result.returncode:
                raise RuntimeError(result.stdout[-500:])
        elif category == "pip_cache":
            delete_contents(PIP_CACHE)
        elif category == "journals":
            result = run(["journalctl", f"--vacuum-size={int(policy['journal_max_mb'])}M"], timeout=300)
            if result.returncode:
                raise RuntimeError(result.stdout[-500:])
        elif category == "snap_cache":
            if SNAP_CACHE.is_dir():
                for item in SNAP_CACHE.iterdir():
                    if item.is_file() and not item.is_symlink():
                        item.unlink()
        elif category == "temporary_files":
            days = int(policy["temporary_retention_days"])
            for root in (Path("/tmp"), ACS_ROOT / "logs", ALGROW_ROOT.parent / "logs"):
                cleanup_old_files(root, days)
        elif category == "algrow_browser_cache":
            was_active = service_status("algrow-vps-chrome") == "active"
            if was_active:
                stopped = run(["systemctl", "stop", "algrow-vps-chrome"], timeout=60)
                if stopped.returncode:
                    raise RuntimeError("Could not stop Algrow Chrome safely")
            try:
                for path in (
                    ALGROW_PROFILE / "Default/Cache",
                    ALGROW_PROFILE / "Default/Code Cache",
                    ALGROW_PROFILE / "Default/GPUCache",
                    ALGROW_PROFILE / "GPUPersistentCache",
                ):
                    delete_contents(path)
            finally:
                if was_active:
                    started = run(["systemctl", "start", "algrow-vps-chrome"], timeout=60)
                    if started.returncode:
                        errors.append("Algrow Chrome did not restart successfully")
        else:
            raise RuntimeError("Unknown cache category")
    except Exception as exc:
        errors.append(f"{category}: {exc}")
    after = shutil.disk_usage("/").free
    return max(0, after - before), errors


def record_operation_start(mode: str, categories: list[str], candidate_bytes: int) -> int:
    with sqlite3.connect(STATE_DB) as db:
        cursor = db.execute(
            """INSERT INTO operations(operation_type,mode,categories_json,status,candidate_bytes,started_at)
               VALUES('cleanup',?,?, 'running',?,?)""",
            (mode, json.dumps(categories), candidate_bytes, utc_now()),
        )
        return int(cursor.lastrowid)


def record_operation_end(operation_id: int, status: str, reclaimed: int, count: int, details: dict[str, Any], error: str = "") -> None:
    with sqlite3.connect(STATE_DB) as db:
        db.execute(
            """UPDATE operations SET status=?,reclaimed_bytes=?,item_count=?,details_json=?,error_text=?,completed_at=?
               WHERE id=?""",
            (status, reclaimed, count, json.dumps(details), error[:4000], utc_now(), operation_id),
        )


def cleanup(payload: dict[str, Any], *, automatic: bool = False) -> dict[str, Any]:
    if FULL_CLEAN_LOCK.exists():
        raise RuntimeError("Full production clean is currently running")
    categories = sorted({str(x) for x in payload.get("categories", [])} & ALL_CATEGORIES)
    if not categories:
        raise ValueError("Select at least one cleanup category")
    if not automatic and str(payload.get("confirmation") or "") != "RECLAIM":
        raise ValueError("Confirmation phrase RECLAIM is required")
    if not _operation_lock.acquire(blocking=False):
        raise RuntimeError("Another storage operation is already running")
    try:
        snapshot = perform_scan()
        by_id = {item["id"]: item for item in snapshot["categories"]}
        candidate = sum(int(by_id.get(key, {}).get("bytes") or 0) for key in categories)
        operation_id = record_operation_start("automatic" if automatic else "manual", categories, candidate)
        before_free = shutil.disk_usage("/").free
        count = 0
        errors: list[str] = []
        details: dict[str, Any] = {}
        try:
            for category in categories:
                data = by_id.get(category, {})
                if category == "acs_projects":
                    freed, done, failed = clean_acs(list(data.get("items") or []))
                elif category == "algrow_projects":
                    freed, done, failed = clean_algrow(list(data.get("items") or []))
                elif category == "scene_projects":
                    freed, done, failed = clean_scene(list(data.get("items") or []))
                else:
                    freed, failed = clean_cache(category, load_policy())
                    done = 1 if not failed else 0
                details[category] = {"estimated_freed_bytes": freed, "items": done, "errors": failed}
                count += done
                errors.extend(failed)
            after_free = shutil.disk_usage("/").free
            reclaimed = max(0, after_free - before_free)
            status = "completed_with_warnings" if errors else "completed"
            record_operation_end(operation_id, status, reclaimed, count, details, "\n".join(errors))
            perform_scan()
            return {
                "ok": not errors,
                "operation_id": operation_id,
                "status": status,
                "candidate_bytes": candidate,
                "reclaimed_bytes": reclaimed,
                "items_cleaned": count,
                "categories": categories,
                "warnings": errors,
            }
        except Exception as exc:
            record_operation_end(operation_id, "failed", 0, count, details, str(exc))
            raise
    finally:
        _operation_lock.release()


# STORAGE_MANAGER_FULL_CLEAN_V173
# STORAGE_MANAGER_FULL_CLEAN_V174
def _atomic_write_full_clean_status(value: dict[str, Any]) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = STATE_ROOT / f".full-clean-status.{os.getpid()}.{time.time_ns()}.tmp"
    tmp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, FULL_CLEAN_STATUS)


def full_production_clean_status() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "service": FULL_CLEAN_SERVICE,
        "running": False,
        "status": "never_run",
        "request_id": "",
        "started_at": "",
        "completed_at": "",
        "exit_code": None,
        "verified": False,
        "reclaimed_bytes": 0,
        "message": "No full production clean has run yet.",
        "log_tail": "",
    }
    with contextlib.suppress(Exception):
        raw = json.loads(FULL_CLEAN_STATUS.read_text(encoding="utf-8"))
        if isinstance(raw, dict): payload.update(raw)

    state = run(["systemctl", "is-active", FULL_CLEAN_SERVICE], timeout=10)
    active = state.returncode == 0 and state.stdout.strip() == "active"
    queued = str(payload.get("status") or "") == "queued"
    payload["running"] = active or queued
    if active:
        payload["status"] = "running"
        payload["message"] = "Full production clean is running. Production services may restart briefly."
    with contextlib.suppress(Exception):
        lines = FULL_CLEAN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        payload["log_tail"] = "\n".join(lines[-140:])[-30000:]
    return payload


def start_full_production_clean(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("confirmation") or "") != "FULL_PRODUCTION_CLEAN":
        raise ValueError("Full production clean confirmation is required")
    if _operation_lock.locked():
        raise RuntimeError("Another storage operation is already running")

    # Clear a stale lock only when the oneshot service is not running.
    state = run(["systemctl", "is-active", FULL_CLEAN_SERVICE], timeout=10)
    active = state.returncode == 0 and state.stdout.strip() == "active"
    if FULL_CLEAN_LOCK.exists() and not active:
        with contextlib.suppress(Exception):
            if time.time() - FULL_CLEAN_LOCK.stat().st_mtime > 60:
                FULL_CLEAN_LOCK.unlink()

    current = full_production_clean_status()
    if current.get("running") or FULL_CLEAN_LOCK.exists():
        raise RuntimeError("Full production clean is already running")

    request_id = f"{int(time.time() * 1000)}-{os.getpid()}"
    started = utc_now()
    queued = {
        "ok": True,
        "service": FULL_CLEAN_SERVICE,
        "running": True,
        "status": "queued",
        "request_id": request_id,
        "started_at": started,
        "completed_at": "",
        "exit_code": None,
        "verified": False,
        "reclaimed_bytes": 0,
        "message": "Full production clean queued. Waiting for the privileged cleaner to start.",
    }
    _atomic_write_full_clean_status(queued)
    result = run(["systemctl", "start", "--no-block", FULL_CLEAN_SERVICE], timeout=30)
    if result.returncode:
        queued.update({
            "ok": False, "running": False, "status": "failed", "exit_code": result.returncode,
            "completed_at": utc_now(), "message": (result.stdout or "Could not start full production clean service").strip(),
        })
        _atomic_write_full_clean_status(queued)
        raise RuntimeError(queued["message"])
    time.sleep(0.2)
    status = full_production_clean_status()
    status["started"] = True
    status["request_id"] = request_id
    return status

def history(limit: int = 50) -> list[dict[str, Any]]:
    with sqlite3.connect(STATE_DB) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT id,operation_type,mode,categories_json,status,candidate_bytes,
                      reclaimed_bytes,item_count,error_text,started_at,completed_at
               FROM operations ORDER BY id DESC LIMIT ?""",
            (max(1, min(200, limit)),),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        with contextlib.suppress(Exception):
            item["categories"] = json.loads(item.pop("categories_json"))
        result.append(item)
    return result


def scheduler_loop() -> None:
    while not _stop_event.is_set():
        policy = load_policy()
        wait_seconds = int(policy["scan_interval_minutes"]) * 60
        try:
            snapshot = perform_scan()
            if policy["auto_enabled"] and policy["auto_categories"]:
                free = int(snapshot["disk"]["free"])
                threshold = int(float(policy["minimum_free_gb"]) * 1024**3)
                should_run = policy["auto_mode"] == "scheduled" or free < threshold
                if should_run and int(snapshot.get("reclaimable_bytes") or 0) > 0:
                    cleanup({"categories": policy["auto_categories"]}, automatic=True)
        except Exception:
            pass
        _stop_event.wait(max(300, wait_seconds))


class Handler(BaseHTTPRequestHandler):
    server_version = "VPSStorageManager/1"

    def log_message(self, *_args: Any) -> None:
        return

    def respond(self, status: int, payload: Any) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self) -> bool:
        supplied = self.headers.get("X-Storage-Token", "")
        return bool(AUTH_TOKEN) and hmac.compare_digest(supplied, AUTH_TOKEN)

    def body(self) -> dict[str, Any]:
        length = min(1024 * 1024, int(self.headers.get("Content-Length", "0") or 0))
        value = json.loads(self.rfile.read(length) or b"{}")
        return value if isinstance(value, dict) else {}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self.respond(200, {"ok": True, "version": VERSION})
        if not self.authenticated():
            return self.respond(403, {"ok": False, "error": "forbidden"})
        try:
            if path == "/v1/metrics":
                return self.respond(200, metrics())
            if path == "/v1/snapshot":
                return self.respond(200, latest_snapshot())
            if path == "/v1/policy":
                return self.respond(200, {"ok": True, "policy": load_policy()})
            if path == "/v1/history":
                return self.respond(200, {"ok": True, "operations": history()})
            if path == "/v1/full-production-clean/status":
                return self.respond(200, full_production_clean_status())
            return self.respond(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            return self.respond(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        if not self.authenticated():
            return self.respond(403, {"ok": False, "error": "forbidden"})
        path = self.path.split("?", 1)[0]
        try:
            payload = self.body()
            if path == "/v1/services/start":
                return self.respond(200, start_allowlisted_service(payload))
            if path == "/v1/scan":
                return self.respond(200, perform_scan())
            if path == "/v1/cleanup":
                return self.respond(200, cleanup(payload))
            if path == "/v1/policy":
                return self.respond(200, {"ok": True, "policy": save_policy(payload)})
            if path == "/v1/full-production-clean":
                return self.respond(202, start_full_production_clean(payload))
            return self.respond(404, {"ok": False, "error": "not found"})
        except ValueError as exc:
            return self.respond(400, {"ok": False, "error": str(exc)})
        except RuntimeError as exc:
            return self.respond(409, {"ok": False, "error": str(exc)})
        except Exception as exc:
            return self.respond(500, {"ok": False, "error": str(exc)})


def serve() -> None:
    if not AUTH_TOKEN:
        raise SystemExit("STORAGE_MANAGER_TOKEN is required")
    init_state()
    threading.Thread(target=scheduler_loop, name="storage-scheduler", daemon=True).start()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve", "scan", "metrics", "history"), default="serve", nargs="?")
    args = parser.parse_args()
    init_state()
    if args.command == "serve":
        serve()
    elif args.command == "scan":
        print(json.dumps(perform_scan(), indent=2))
    elif args.command == "metrics":
        print(json.dumps(metrics(), indent=2))
    else:
        print(json.dumps(history(), indent=2))


if __name__ == "__main__":
    main()

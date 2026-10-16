from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .auth import Principal
from .database import connect
from .drive_batch_store import ensure_drive_batch_schema
from .web_flow_api import (
    DriveFolderScanPayload,
    DeferredDriveBatchPayload,
    DeferredDriveItemPayload,
    _load_presets,
    _load_user_state,
    scan_drive_folder,
    create_deferred_drive_batch,
)

CONFIG = Path("/home/ubuntu/apps/auto_caption_studio/data/config/drive_profiles.json")
RUNTIME = Path("/home/ubuntu/apps/auto_caption_studio/data/config/drive_profiles_runtime.json")
TRIGGER = Path("/home/ubuntu/apps/auto_caption_studio/data/config/drive_profiles_sync.trigger")
STARTUP_GRACE = max(0, int(os.environ.get("ACS_AUTOREFILL_STARTUP_GRACE_SECONDS", "30")))
POLL_SECONDS = max(5, int(os.environ.get("ACS_AUTOREFILL_POLL_SECONDS", "10")))
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
STOP = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(msg: str) -> None:
    print(f"{now_iso()} {msg}", flush=True)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8")) if path.is_file() else default
    except Exception:
        return default


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def error_text(exc: BaseException) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return f"{type(exc).__name__}: {exc}"


def runtime_state() -> dict[str, Any]:
    state = load_json(RUNTIME, {})
    return state if isinstance(state, dict) else {}


def save_runtime(**patch: Any) -> dict[str, Any]:
    state = runtime_state()
    state.update(patch)
    state["updated_at"] = now_iso()
    state.setdefault("profiles", {})
    atomic_json(RUNTIME, state)
    return state


def update_profile_runtime(profile_id: str, **patch: Any) -> None:
    state = runtime_state()
    profiles = state.setdefault("profiles", {})
    item = profiles.setdefault(str(profile_id), {})
    item.update(patch)
    item["updated_at"] = now_iso()
    state["updated_at"] = now_iso()
    atomic_json(RUNTIME, state)


def load_config() -> dict[str, Any]:
    data = load_json(CONFIG, {"version": 1, "settings": {}, "profiles": []})
    if not isinstance(data, dict):
        data = {"version": 1, "settings": {}, "profiles": []}
    data.setdefault("settings", {})
    data.setdefault("profiles", [])
    return data


def _is_web_flow(settings_text: str) -> tuple[bool, dict[str, Any]]:
    try:
        settings = json.loads(settings_text or "{}")
    except Exception:
        settings = {}
    metadata = settings.get("metadata") if isinstance(settings, dict) and isinstance(settings.get("metadata"), dict) else {}
    return bool(metadata.get("web_flow_v1")), metadata


def queue_snapshot() -> dict[str, Any]:
    busy_jobs: list[dict[str, str]] = []
    busy_sessions = 0
    busy_drive_rows = 0
    with connect() as db:
        rows = db.execute(
            """SELECT id,status,pipeline_stage,current_stage,drive_status,drive_verified,local_cleanup_status,settings_json
               FROM jobs ORDER BY created_at"""
        ).fetchall()
        for row in rows:
            is_web, meta = _is_web_flow(str(row["settings_json"] or "{}"))
            if not is_web:
                continue
            status = str(row["status"] or "").lower()
            pipeline = str(row["pipeline_stage"] or "").lower()
            drive_status = str(row["drive_status"] or "").lower()
            busy = status not in TERMINAL
            # A completed render is still production-busy while output delivery is pending/uploading.
            if status == "completed":
                drive_enabled = bool(meta.get("web_drive_upload_enabled", True))
                if drive_enabled and not int(row["drive_verified"] or 0) and drive_status in {"", "pending", "queued", "uploading"}:
                    busy = True
                if pipeline == "ready_upload" and drive_status != "failed":
                    busy = True
            if busy:
                busy_jobs.append({
                    "id": str(row["id"]), "status": status,
                    "pipeline_stage": pipeline, "current_stage": str(row["current_stage"] or ""),
                    "drive_status": drive_status,
                })

        # A manual upload session can exist briefly before its first job is staged.
        sessions = db.execute("SELECT status,settings_json FROM upload_batches WHERE status='uploading'").fetchall()
        for row in sessions:
            try:
                settings = json.loads(str(row["settings_json"] or "{}"))
            except Exception:
                settings = {}
            if bool(settings.get("web_flow_v1")):
                busy_sessions += 1

        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='drive_batch_queue'").fetchone()
        if exists:
            busy_drive_rows = int(db.execute(
                """SELECT COUNT(*) FROM drive_batch_queue q
                   LEFT JOIN jobs j ON j.id=q.job_id
                   WHERE q.status IN ('copying','move_pending')
                      OR (q.status IN ('remote_waiting','retry_wait','staged') AND COALESCE(j.status,'held') NOT IN ('failed','cancelled','interrupted','completed'))"""
            ).fetchone()[0] or 0)

    empty = not busy_jobs and busy_sessions == 0 and busy_drive_rows == 0
    return {
        "empty": empty,
        "busy_jobs": len(busy_jobs),
        "busy_sessions": busy_sessions,
        "busy_drive_rows": busy_drive_rows,
        "sample": busy_jobs[:5],
    }


def principal_for(profile: dict[str, Any]) -> Principal:
    owner = str(profile.get("created_by") or "team").strip() or "team"
    return Principal(username=owner, is_admin=False)


def resolve_preset(profile: dict[str, Any], principal: Principal) -> str:
    presets = _load_presets(principal)
    requested = str(profile.get("preset") or "").strip()
    if not requested:
        requested = str(_load_user_state(principal).get("selected_preset_id") or "")
    key = requested.casefold()
    item = next((p for p in presets if str(p.get("id") or "").casefold() == key), None)
    if item is None:
        item = next((p for p in presets if str(p.get("name") or "").casefold() == key), None)
    if item is None:
        raise RuntimeError(f"Preset not found for Drive Profile: {requested or '(blank)'}")
    return str(item.get("id") or "")


def existing_sources(owner: str) -> set[str]:
    ensure_drive_batch_schema()
    with connect() as db:
        rows = db.execute("SELECT txt_remote,audio_remote FROM drive_batch_queue WHERE owner_id=?", (owner,)).fetchall()
    out: set[str] = set()
    for row in rows:
        out.add(str(row["txt_remote"] or ""))
        out.add(str(row["audio_remote"] or ""))
    out.discard("")
    return out


def scan_one(profile: dict[str, Any]) -> dict[str, Any]:
    pid = str(profile.get("id") or "")
    principal = principal_for(profile)
    voice = str(profile.get("voice_mode") or "upload_audio").strip().casefold().replace(" ", "_")
    if voice and voice not in {"upload_audio", "use_uploaded_audio", "uploaded_audio", "audio"}:
        raise RuntimeError("Drive auto-refill currently supports Upload Audio profiles only")
    folder = str(profile.get("folder_ref") or "").strip()
    if not folder:
        raise RuntimeError("Drive folder is blank")
    preset_id = resolve_preset(profile, principal)
    scanned = scan_drive_folder(DriveFolderScanPayload(folder=folder), principal=principal)
    valid = [x for x in list(scanned.get("items") or []) if bool(x.get("valid"))]
    seen = existing_sources(principal.username)
    fresh = [x for x in valid if str(x.get("txt_remote") or "") not in seen and str(x.get("audio_remote") or "") not in seen]
    fresh = fresh[:500]
    return {
        "profile": profile,
        "profile_id": pid,
        "principal": principal,
        "folder": folder,
        "preset_id": preset_id,
        "category": str(profile.get("category") or "ACS Final Videos").strip() or "ACS Final Videos",
        "found": int(scanned.get("valid_pairs") or len(valid)),
        "fresh": fresh,
    }


def create_batch(scanned: dict[str, Any]) -> dict[str, Any] | None:
    fresh = scanned["fresh"]
    if not fresh:
        return None
    payload = DeferredDriveBatchPayload(
        folder=scanned["folder"],
        preset_id=scanned["preset_id"],
        category=scanned["category"],
        items=[DeferredDriveItemPayload(
            base=str(x.get("base") or ""),
            txt_remote=str(x.get("txt_remote") or ""),
            audio_remote=str(x.get("audio_remote") or ""),
            fg_library_id="",
        ) for x in fresh],
    )
    return create_deferred_drive_batch(payload, principal=scanned["principal"])


def run_refill_cycle(reason: str) -> dict[str, Any]:
    started = now_iso()
    cfg = load_config()
    profiles = [p for p in cfg.get("profiles", []) if isinstance(p, dict) and bool(p.get("enabled", False))]
    save_runtime(status="scanning", reason=reason, cycle_started_at=started, enabled_profiles=len(profiles))
    if not profiles:
        save_runtime(status="idle_empty", reason=reason, last_global_sync=started, last_result="No enabled Drive Profiles", last_added=0)
        return {"added": 0, "profiles": 0}

    # Scan every enabled profile while the queue is still empty. No queue rows are created in this stage.
    scanned_profiles: list[dict[str, Any]] = []
    total_found = 0
    for profile in profiles:
        pid = str(profile.get("id") or "")
        try:
            scanned = scan_one(profile)
            scanned_profiles.append(scanned)
            total_found += int(scanned["found"])
            update_profile_runtime(pid, last_sync=started, last_result=f"Scanned: {scanned['found']} pair(s), {len(scanned['fresh'])} new", last_found=int(scanned["found"]), last_added=0)
        except Exception as exc:
            msg = error_text(exc)
            update_profile_runtime(pid, last_sync=started, last_result=f"Error: {msg}"[:1000], last_found=0, last_added=0)
            log(f"PROFILE_SCAN_FAIL id={pid} error={msg}")

    # Guard again after all Drive reads. If manual work appeared during scanning, enqueue nothing.
    guard = queue_snapshot()
    if not guard["empty"]:
        save_runtime(status="waiting_queue_empty", reason=reason, last_global_sync=started, last_result="Scanned but queue became busy; no auto-refill batch created", last_found=total_found, last_added=0, queue=guard)
        return {"added": 0, "profiles": len(profiles), "blocked": True}

    total_added = 0
    created_batches: list[str] = []
    for scanned in scanned_profiles:
        pid = scanned["profile_id"]
        if not scanned["fresh"]:
            continue
        try:
            result = create_batch(scanned)
            added = len(scanned["fresh"]) if result else 0
            total_added += added
            if result:
                created_batches.append(str(result.get("batch_id") or ""))
            update_profile_runtime(pid, last_sync=started, last_result=f"Added {added} project(s) to ACS queue" if added else "No new pairs", last_found=int(scanned["found"]), last_added=added, last_batch_id=str((result or {}).get("batch_id") or ""))
        except Exception as exc:
            msg = error_text(exc)
            update_profile_runtime(pid, last_sync=started, last_result=f"Enqueue error: {msg}"[:1000], last_found=int(scanned["found"]), last_added=0)
            log(f"PROFILE_ENQUEUE_FAIL id={pid} error={msg}")

    result_text = f"Added {total_added} project(s) from {len(profiles)} enabled profile(s)" if total_added else f"No new pairs across {len(profiles)} enabled profile(s)"
    save_runtime(status="queue_refilled" if total_added else "idle_empty", reason=reason, last_global_sync=started, last_result=result_text, last_found=total_found, last_added=total_added, batches=created_batches, queue=queue_snapshot())
    return {"added": total_added, "profiles": len(profiles), "batches": created_batches}


def requested_manual_sync() -> bool:
    return TRIGGER.is_file()


def consume_trigger() -> None:
    try:
        TRIGGER.unlink(missing_ok=True)
    except Exception:
        pass


def on_signal(_signum, _frame) -> None:
    global STOP
    STOP = True


def main() -> None:
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    ensure_drive_batch_schema()
    save_runtime(status="starting", service_started_at=now_iso(), startup_grace_seconds=STARTUP_GRACE)
    log(f"ACS_DRIVE_PROFILE_AUTOREFILL_START grace={STARTUP_GRACE}s poll={POLL_SECONDS}s")
    if STARTUP_GRACE:
        deadline = time.monotonic() + STARTUP_GRACE
        while not STOP and time.monotonic() < deadline:
            time.sleep(1)

    was_empty: bool | None = None
    last_cycle_mono = 0.0
    while not STOP:
        try:
            cfg = load_config()
            settings = cfg.get("settings") if isinstance(cfg.get("settings"), dict) else {}
            auto_enabled = bool(settings.get("auto_refill_enabled", False))
            interval_min = max(1, min(1440, int(settings.get("idle_recheck_minutes", 15) or 15)))
            snap = queue_snapshot()
            empty = bool(snap["empty"])
            manual = requested_manual_sync()
            if not empty:
                was_empty = False
                if manual:
                    save_runtime(status="waiting_queue_empty", auto_refill_enabled=auto_enabled, queue=snap, next_scan="when queue is empty", last_result="Manual sync requested; waiting for queue to become empty")
                else:
                    save_runtime(status="sleeping_queue_busy", auto_refill_enabled=auto_enabled, queue=snap, next_scan="when queue is empty")
                time.sleep(POLL_SECONDS)
                continue

            due_transition = was_empty is False or was_empty is None
            due_interval = last_cycle_mono <= 0 or (time.monotonic() - last_cycle_mono) >= interval_min * 60
            should_run = manual or (auto_enabled and (due_transition or due_interval))
            if should_run:
                reason = "manual" if manual else ("queue_became_empty" if due_transition else "idle_recheck")
                if manual:
                    consume_trigger()
                result = run_refill_cycle(reason)
                last_cycle_mono = time.monotonic()
                log(f"REFILL_CYCLE reason={reason} added={result.get('added',0)} profiles={result.get('profiles',0)}")
                snap = queue_snapshot()
                empty = bool(snap["empty"])
            else:
                remaining = max(0, int(interval_min * 60 - (time.monotonic() - last_cycle_mono))) if last_cycle_mono else 0
                save_runtime(status="idle_empty", auto_refill_enabled=auto_enabled, queue=snap, next_scan_seconds=remaining, next_scan="idle recheck" if auto_enabled else "auto refill disabled")
            was_empty = empty
        except Exception as exc:
            log(f"LOOP_ERROR {error_text(exc)}")
            save_runtime(status="error", last_result=error_text(exc)[:1000])
        time.sleep(POLL_SECONDS)
    save_runtime(status="stopped")
    log("ACS_DRIVE_PROFILE_AUTOREFILL_STOP")


if __name__ == "__main__":
    main()

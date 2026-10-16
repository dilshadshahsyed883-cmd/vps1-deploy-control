from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request

from autoflow_control_backend import DB_PATH, _now, _project_row, _require_api

FLOW_STATUS = {"REGISTERED", "READY", "COOLDOWN", "ERROR", "DISABLED", "NOT_LOGGED_IN"}
LOCK_MODES = {"AUTO", "PREFER", "STRICT"}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._").lower()
    return value or "flow-profile"


def _iso_future(value: str) -> bool:
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > datetime.now(timezone.utc)
    except Exception:
        return False


def init_flow_schema() -> None:
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS autoflow_flow_profiles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              enabled INTEGER NOT NULL DEFAULT 1,
              is_default INTEGER NOT NULL DEFAULT 0,
              profile_dir TEXT NOT NULL DEFAULT '',
              debug_port INTEGER NOT NULL DEFAULT 0,
              display_num INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'REGISTERED',
              login_state TEXT NOT NULL DEFAULT 'UNKNOWN',
              health INTEGER NOT NULL DEFAULT 100,
              manual_daily_limit INTEGER NOT NULL DEFAULT 0,
              detected_daily_limit INTEGER NOT NULL DEFAULT 0,
              used_today INTEGER NOT NULL DEFAULT 0,
              usage_date TEXT NOT NULL DEFAULT '',
              cooldown_until TEXT NOT NULL DEFAULT '',
              last_used_at TEXT NOT NULL DEFAULT '',
              last_health_at TEXT NOT NULL DEFAULT '',
              last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_autoflow_flow_profiles_ready
              ON autoflow_flow_profiles(enabled,status,health,used_today);

            CREATE TABLE IF NOT EXISTS autoflow_flow_project_bindings (
              project_id INTEGER PRIMARY KEY,
              flow_profile_id INTEGER,
              lock_mode TEXT NOT NULL DEFAULT 'AUTO',
              updated_at TEXT NOT NULL,
              FOREIGN KEY(flow_profile_id) REFERENCES autoflow_flow_profiles(id) ON DELETE SET NULL
            );
            """
        )


def _roll_day(conn: sqlite3.Connection) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    conn.execute(
        "UPDATE autoflow_flow_profiles SET used_today=0,usage_date=?,updated_at=? WHERE usage_date!=?",
        (today, _now(), today),
    )


def _effective_limit(row: dict[str, Any]) -> int:
    vals = [int(row.get(k) or 0) for k in ("manual_daily_limit", "detected_daily_limit")]
    vals = [v for v in vals if v > 0]
    return min(vals) if vals else 0


def _profile_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["enabled"] = bool(out.get("enabled"))
    out["is_default"] = bool(out.get("is_default"))
    out["effective_daily_limit"] = _effective_limit(out)
    limit = out["effective_daily_limit"]
    used = int(out.get("used_today") or 0)
    out["remaining_today"] = max(0, limit - used) if limit > 0 else None
    out["in_cooldown"] = _iso_future(str(out.get("cooldown_until") or ""))
    out["eligible"] = bool(
        out["enabled"]
        and str(out.get("status") or "").upper() not in {"ERROR", "DISABLED"}
        and int(out.get("health") or 0) >= 20
        and not out["in_cooldown"]
        and (limit <= 0 or used < limit)
    )
    return out


def _binding(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    _project_row(conn, project_id)
    row = conn.execute("SELECT * FROM autoflow_flow_project_bindings WHERE project_id=?", (project_id,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO autoflow_flow_project_bindings(project_id,flow_profile_id,lock_mode,updated_at) VALUES(?,NULL,'AUTO',?)",
            (project_id, _now()),
        )
        row = conn.execute("SELECT * FROM autoflow_flow_project_bindings WHERE project_id=?", (project_id,)).fetchone()
    return dict(row)


def _eligible_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    _roll_day(conn)
    rows = [_profile_dict(r) for r in conn.execute("SELECT * FROM autoflow_flow_profiles ORDER BY id").fetchall()]
    return [r for r in rows if r["eligible"]]


def select_flow_profile(conn: sqlite3.Connection, project_id: int | None = None, reserve: bool = False) -> dict[str, Any] | None:
    binding = None
    preferred_id = None
    lock_mode = "AUTO"
    if project_id is not None:
        binding = _binding(conn, int(project_id))
        preferred_id = binding.get("flow_profile_id")
        lock_mode = str(binding.get("lock_mode") or "AUTO").upper()

    all_rows = {int(r["id"]): r for r in [_profile_dict(r) for r in conn.execute("SELECT * FROM autoflow_flow_profiles ORDER BY id").fetchall()]}
    eligible = _eligible_profiles(conn)
    selected = None
    reason = "SMART_POOL"

    if preferred_id:
        preferred = all_rows.get(int(preferred_id))
        if preferred and preferred.get("eligible"):
            selected = preferred
            reason = "PROJECT_STRICT" if lock_mode == "STRICT" else "PROJECT_PREFERRED"
        elif lock_mode == "STRICT":
            return None

    if selected is None and eligible:
        def score(p: dict[str, Any]):
            limit = int(p.get("effective_daily_limit") or 0)
            used = int(p.get("used_today") or 0)
            utilization = (used / limit) if limit > 0 else (used / max(1, used + 10))
            return (utilization, used, -int(p.get("health") or 0), 0 if p.get("is_default") else 1, str(p.get("last_used_at") or ""))
        selected = sorted(eligible, key=score)[0]
        reason = "SMART_POOL"

    if selected and reserve:
        today = datetime.now(timezone.utc).date().isoformat()
        now = _now()
        conn.execute(
            "UPDATE autoflow_flow_profiles SET used_today=used_today+1,usage_date=?,last_used_at=?,updated_at=? WHERE id=?",
            (today, now, now, selected["id"]),
        )
        selected = _profile_dict(conn.execute("SELECT * FROM autoflow_flow_profiles WHERE id=?", (selected["id"],)).fetchone())
    if selected:
        selected["selection_reason"] = reason
        selected["lock_mode"] = lock_mode
    return selected


def register_autoflow_flow_profiles(app) -> None:
    init_flow_schema()

    @app.get("/api/master-controller/flow/profiles")
    def flow_profiles(request: Request, project_id: int | None = None):
        _require_api(request)
        with _db() as conn:
            _roll_day(conn)
            profiles = [_profile_dict(r) for r in conn.execute("SELECT * FROM autoflow_flow_profiles ORDER BY is_default DESC,name COLLATE NOCASE").fetchall()]
            binding = _binding(conn, project_id) if project_id is not None else None
            selected = select_flow_profile(conn, project_id, False) if profiles else None
            return {"ok": True, "profiles": profiles, "binding": binding, "selected": selected, "rotation": "SMART"}

    @app.post("/api/master-controller/flow/profiles")
    async def flow_profile_create(request: Request):
        _require_api(request)
        body = await request.json()
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Profile name is required")
        manual = max(0, int(body.get("manual_daily_limit") or 0))
        detected = max(0, int(body.get("detected_daily_limit") or 0))
        now = _now()
        with _db() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO autoflow_flow_profiles(name,manual_daily_limit,detected_daily_limit,usage_date,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (name, manual, detected, datetime.now(timezone.utc).date().isoformat(), now, now),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, "A Flow profile with this name already exists")
            pid = int(cur.lastrowid)
            profile_dir = f"/home/ubuntu/.local/share/autoflow-flow/profiles/{_slug(name)}-{pid}"
            debug_port = 9330 + pid
            display_num = 101 + pid
            if conn.execute("SELECT COUNT(*) FROM autoflow_flow_profiles WHERE is_default=1").fetchone()[0] == 0:
                conn.execute("UPDATE autoflow_flow_profiles SET is_default=1 WHERE id=?", (pid,))
            conn.execute(
                "UPDATE autoflow_flow_profiles SET profile_dir=?,debug_port=?,display_num=?,updated_at=? WHERE id=?",
                (profile_dir, debug_port, display_num, now, pid),
            )
            row = conn.execute("SELECT * FROM autoflow_flow_profiles WHERE id=?", (pid,)).fetchone()
            return {"ok": True, "profile": _profile_dict(row)}

    @app.post("/api/master-controller/flow/profiles/{profile_id}")
    async def flow_profile_update(profile_id: int, request: Request):
        _require_api(request)
        body = await request.json()
        allowed = {"name", "enabled", "status", "login_state", "health", "manual_daily_limit", "detected_daily_limit", "cooldown_until", "last_error"}
        sets: list[str] = []
        vals: list[Any] = []
        if body.get("is_default"):
            with _db() as conn:
                if not conn.execute("SELECT 1 FROM autoflow_flow_profiles WHERE id=?", (profile_id,)).fetchone():
                    raise HTTPException(404, "Flow profile not found")
                conn.execute("UPDATE autoflow_flow_profiles SET is_default=0")
                conn.execute("UPDATE autoflow_flow_profiles SET is_default=1,updated_at=? WHERE id=?", (_now(), profile_id))
                return {"ok": True, "profile": _profile_dict(conn.execute("SELECT * FROM autoflow_flow_profiles WHERE id=?", (profile_id,)).fetchone())}
        for key in allowed:
            if key not in body:
                continue
            val = body[key]
            if key == "enabled": val = int(bool(val))
            elif key in {"health"}: val = max(0, min(100, int(val)))
            elif key in {"manual_daily_limit", "detected_daily_limit"}: val = max(0, int(val or 0))
            elif key == "status":
                val = str(val or "REGISTERED").upper()
                if val not in FLOW_STATUS: raise HTTPException(400, "Invalid Flow status")
            else: val = str(val or "").strip()
            sets.append(f"{key}=?"); vals.append(val)
        if not sets:
            raise HTTPException(400, "No supported fields supplied")
        sets.append("updated_at=?"); vals.append(_now()); vals.append(profile_id)
        with _db() as conn:
            if not conn.execute("SELECT 1 FROM autoflow_flow_profiles WHERE id=?", (profile_id,)).fetchone():
                raise HTTPException(404, "Flow profile not found")
            try:
                conn.execute(f"UPDATE autoflow_flow_profiles SET {','.join(sets)} WHERE id=?", vals)
            except sqlite3.IntegrityError:
                raise HTTPException(409, "A Flow profile with this name already exists")
            return {"ok": True, "profile": _profile_dict(conn.execute("SELECT * FROM autoflow_flow_profiles WHERE id=?", (profile_id,)).fetchone())}

    @app.post("/api/master-controller/flow/profiles/{profile_id}/action")
    async def flow_profile_action(profile_id: int, request: Request):
        _require_api(request)
        body = await request.json()
        action = str(body.get("action") or "").upper()
        now = _now()
        with _db() as conn:
            row = conn.execute("SELECT * FROM autoflow_flow_profiles WHERE id=?", (profile_id,)).fetchone()
            if not row: raise HTTPException(404, "Flow profile not found")
            if action == "ENABLE": conn.execute("UPDATE autoflow_flow_profiles SET enabled=1,status=CASE WHEN status='DISABLED' THEN 'REGISTERED' ELSE status END,updated_at=? WHERE id=?", (now, profile_id))
            elif action == "DISABLE": conn.execute("UPDATE autoflow_flow_profiles SET enabled=0,status='DISABLED',updated_at=? WHERE id=?", (now, profile_id))
            elif action == "CLEAR_COOLDOWN": conn.execute("UPDATE autoflow_flow_profiles SET cooldown_until='',status=CASE WHEN status='COOLDOWN' THEN 'REGISTERED' ELSE status END,updated_at=? WHERE id=?", (now, profile_id))
            elif action == "RESET_USAGE": conn.execute("UPDATE autoflow_flow_profiles SET used_today=0,usage_date=?,updated_at=? WHERE id=?", (datetime.now(timezone.utc).date().isoformat(), now, profile_id))
            elif action == "MARK_READY": conn.execute("UPDATE autoflow_flow_profiles SET status='READY',health=MAX(health,80),last_health_at=?,last_error='',updated_at=? WHERE id=?", (now, now, profile_id))
            else: raise HTTPException(400, "Unsupported Flow profile action")
            return {"ok": True, "profile": _profile_dict(conn.execute("SELECT * FROM autoflow_flow_profiles WHERE id=?", (profile_id,)).fetchone())}

    @app.get("/api/master-controller/projects/{project_id}/flow/binding")
    def flow_binding(project_id: int, request: Request):
        _require_api(request)
        with _db() as conn: return {"ok": True, "binding": _binding(conn, project_id), "selected": select_flow_profile(conn, project_id, False)}

    @app.post("/api/master-controller/projects/{project_id}/flow/binding")
    async def flow_binding_update(project_id: int, request: Request):
        _require_api(request)
        body = await request.json()
        mode = str(body.get("lock_mode") or "AUTO").upper()
        if mode not in LOCK_MODES: raise HTTPException(400, "Invalid lock mode")
        profile_id = body.get("flow_profile_id")
        profile_id = int(profile_id) if profile_id not in (None, "", 0, "0") else None
        with _db() as conn:
            _project_row(conn, project_id)
            if profile_id and not conn.execute("SELECT 1 FROM autoflow_flow_profiles WHERE id=?", (profile_id,)).fetchone():
                raise HTTPException(404, "Flow profile not found")
            if mode == "STRICT" and not profile_id: raise HTTPException(400, "Strict lock requires a Flow profile")
            conn.execute(
                "INSERT INTO autoflow_flow_project_bindings(project_id,flow_profile_id,lock_mode,updated_at) VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET flow_profile_id=excluded.flow_profile_id,lock_mode=excluded.lock_mode,updated_at=excluded.updated_at",
                (project_id, profile_id, mode, _now()),
            )
            return {"ok": True, "binding": _binding(conn, project_id), "selected": select_flow_profile(conn, project_id, False)}

    @app.post("/api/master-controller/flow/select")
    async def flow_select(request: Request):
        _require_api(request)
        body = await request.json()
        project_id = body.get("project_id")
        project_id = int(project_id) if project_id not in (None, "", 0, "0") else None
        reserve = bool(body.get("reserve", False))
        with _db() as conn:
            selected = select_flow_profile(conn, project_id, reserve)
            return {"ok": True, "selected": selected, "reason": selected.get("selection_reason") if selected else "NO_ELIGIBLE_PROFILE"}

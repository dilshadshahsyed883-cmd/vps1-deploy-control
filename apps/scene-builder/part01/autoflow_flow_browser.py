from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from fastapi import HTTPException, Request, Response, WebSocket
from playwright.sync_api import sync_playwright

from autoflow_control_backend import DB_PATH, _now, _require_api

FLOW_TOOL_URL = "https://flow.google.com/project/f2920195-0c23-44d9-878f-854e882c1d17/tool/aca941d9-3eed-4791-bd75-33f37098c755?mode=EDIT"
MARKER = "AUTOFLOW_FLOW_PHASE2B_BROWSER_V1"


def _db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _columns(con, table: str):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _ensure_schema():
    with _db() as con:
        cols = _columns(con, "autoflow_flow_profiles")
        additions = {
            "tool_url": "TEXT NOT NULL DEFAULT ''",
            "vnc_port": "INTEGER NOT NULL DEFAULT 0",
            "viewer_port": "INTEGER NOT NULL DEFAULT 0",
            "browser_desired": "TEXT NOT NULL DEFAULT 'STOPPED'",
            "browser_state": "TEXT NOT NULL DEFAULT 'STOPPED'",
            "browser_pid": "INTEGER NOT NULL DEFAULT 0",
            "restart_seq": "INTEGER NOT NULL DEFAULT 0",
            "last_browser_url": "TEXT NOT NULL DEFAULT ''",
            "last_browser_title": "TEXT NOT NULL DEFAULT ''",
            "last_browser_seen_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in additions.items():
            if name not in cols:
                con.execute(f"ALTER TABLE autoflow_flow_profiles ADD COLUMN {name} {ddl}")
        rows = con.execute("SELECT id,tool_url,vnc_port,viewer_port FROM autoflow_flow_profiles ORDER BY id").fetchall()
        for r in rows:
            pid = int(r["id"])
            con.execute(
                "UPDATE autoflow_flow_profiles SET tool_url=CASE WHEN tool_url='' THEN ? ELSE tool_url END, vnc_port=CASE WHEN vnc_port=0 THEN ? ELSE vnc_port END, viewer_port=CASE WHEN viewer_port=0 THEN ? ELSE viewer_port END WHERE id=?",
                (FLOW_TOOL_URL, 5910 + pid, 6090 + pid, pid),
            )
        if not rows:
            now = _now()
            cur = con.execute(
                "INSERT INTO autoflow_flow_profiles(name,enabled,is_default,profile_dir,debug_port,display_num,status,login_state,health,manual_daily_limit,detected_daily_limit,used_today,usage_date,tool_url,vnc_port,viewer_port,browser_desired,browser_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "Flow 1", 1, 1, "", 0, 0, "REGISTERED", "UNKNOWN", 50,
                    0, 0, 0, datetime.now(timezone.utc).date().isoformat(), FLOW_TOOL_URL,
                    0, 0, "RUNNING", "STARTING", now, now,
                ),
            )
            pid = int(cur.lastrowid)
            profile_dir = f"/home/ubuntu/.local/share/autoflow-flow/profiles/flow-1-{pid}"
            con.execute(
                "UPDATE autoflow_flow_profiles SET profile_dir=?,debug_port=?,display_num=?,vnc_port=?,viewer_port=? WHERE id=?",
                (profile_dir, 9330 + pid, 101 + pid, 5910 + pid, 6090 + pid, pid),
            )
        con.commit()


def _profile(con, profile_id: int):
    row = con.execute("SELECT * FROM autoflow_flow_profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Flow profile not found")
    return dict(row)


def _cdp_snapshot(p: dict):
    port = int(p.get("debug_port") or 0)
    out = {"reachable": False, "pages": [], "url": "", "title": "", "login_state": "UNKNOWN", "tool_detected": False}
    if not port:
        return out
    try:
        version = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
        version.raise_for_status()
        pages = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
        out["reachable"] = True
        out["pages"] = [{"title": str(x.get("title") or ""), "url": str(x.get("url") or "")} for x in pages if x.get("type") == "page"][:12]
        if out["pages"]:
            out["url"] = out["pages"][0]["url"]
            out["title"] = out["pages"][0]["title"]
        joined = " ".join(x["url"] for x in out["pages"]).lower()
        if "accounts.google.com" in joined or "signin" in joined:
            out["login_state"] = "LOGIN_REQUIRED"
        elif "flow.google.com/project/" in joined:
            out["login_state"] = "FLOW_OPEN"
        elif "flow.google.com" in joined:
            out["login_state"] = "FLOW_HOME"
        else:
            out["login_state"] = "BROWSER_READY"
    except Exception:
        pass
    return out


def _deep_probe(p: dict):
    snap = _cdp_snapshot(p)
    port = int(p.get("debug_port") or 0)
    if not snap["reachable"] or not port:
        return snap
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            pages = [pg for ctx in browser.contexts for pg in ctx.pages]
            page = next((pg for pg in pages if "flow.google.com" in pg.url), pages[0] if pages else None)
            if page:
                body = page.locator("body").inner_text(timeout=8000)[:30000]
                low = body.lower()
                snap["url"] = page.url
                snap["title"] = page.title()
                snap["tool_detected"] = all(x in low for x in ("permanent id", "thumbnail prompt", "generate thumbnails"))
                snap["thumbnail_forge"] = "thumbnail forge" in low
                snap["generate_button_count"] = page.get_by_text("Generate Thumbnails", exact=True).count()
                snap["approve_button_count"] = page.get_by_text("Approve Final", exact=True).count()
                snap["body_sample"] = body[:1600]
                if snap["tool_detected"]:
                    snap["login_state"] = "READY"
                elif "sign in" in low and "google" in low:
                    snap["login_state"] = "LOGIN_REQUIRED"
            browser.close()
    except Exception as exc:
        snap["probe_error"] = f"{type(exc).__name__}: {exc}"[:600]
    return snap


def _write_snapshot(profile_id: int, snap: dict):
    with _db() as con:
        state = "RUNNING" if snap.get("reachable") else "STARTING"
        login = str(snap.get("login_state") or "UNKNOWN")
        health = 100 if login == "READY" else (80 if snap.get("reachable") else 25)
        con.execute(
            "UPDATE autoflow_flow_profiles SET browser_state=?,login_state=?,health=?,last_browser_url=?,last_browser_title=?,last_browser_seen_at=?,last_health_at=?,last_error=?,updated_at=? WHERE id=?",
            (state, login, health, str(snap.get("url") or ""), str(snap.get("title") or ""), _now(), _now(), str(snap.get("probe_error") or ""), _now(), profile_id),
        )
        con.commit()


def _status(profile_id: int, deep: bool = False):
    with _db() as con:
        p = _profile(con, profile_id)
    snap = _deep_probe(p) if deep else _cdp_snapshot(p)
    _write_snapshot(profile_id, snap)
    with _db() as con:
        p = _profile(con, profile_id)
    viewer = f"/autoflow/flow/viewer/{profile_id}/vnc.html?autoconnect=true&resize=scale&path={quote(f'autoflow/flow/viewer/{profile_id}/websockify', safe='/')}"
    return {"ok": True, "profile": p, "browser": snap, "viewer_url": viewer}


def register_autoflow_flow_browser(app):
    _ensure_schema()

    @app.get("/api/master-controller/flow/profiles/{profile_id}/browser")
    def flow_browser_status(profile_id: int, request: Request, deep: bool = False):
        _require_api(request)
        return _status(profile_id, deep)

    @app.post("/api/master-controller/flow/profiles/{profile_id}/browser/config")
    async def flow_browser_config(profile_id: int, request: Request):
        _require_api(request)
        body = await request.json()
        tool_url = str(body.get("tool_url") or "").strip()
        if tool_url and not tool_url.startswith("https://flow.google.com/"):
            raise HTTPException(400, "Tool URL must be on https://flow.google.com/")
        with _db() as con:
            _profile(con, profile_id)
            con.execute("UPDATE autoflow_flow_profiles SET tool_url=?,updated_at=? WHERE id=?", (tool_url or FLOW_TOOL_URL, _now(), profile_id))
            con.commit()
        return _status(profile_id, False)

    @app.post("/api/master-controller/flow/profiles/{profile_id}/browser/action")
    async def flow_browser_action(profile_id: int, request: Request):
        _require_api(request)
        body = await request.json()
        action = str(body.get("action") or "").strip().upper()
        with _db() as con:
            p = _profile(con, profile_id)
            if action == "START":
                con.execute("UPDATE autoflow_flow_profiles SET browser_desired='RUNNING',browser_state='STARTING',updated_at=? WHERE id=?", (_now(), profile_id))
            elif action == "STOP":
                con.execute("UPDATE autoflow_flow_profiles SET browser_desired='STOPPED',browser_state='STOPPING',updated_at=? WHERE id=?", (_now(), profile_id))
            elif action == "RESTART":
                con.execute("UPDATE autoflow_flow_profiles SET browser_desired='RUNNING',browser_state='RESTARTING',restart_seq=restart_seq+1,updated_at=? WHERE id=?", (_now(), profile_id))
            elif action in {"OPEN_TOOL", "TEST"}:
                pass
            else:
                raise HTTPException(400, "Unsupported Flow browser action")
            con.commit()
        if action in {"START", "RESTART"}:
            time.sleep(1.2)
            return _status(profile_id, False)
        if action == "STOP":
            return _status(profile_id, False)
        if action == "OPEN_TOOL":
            with _db() as con:
                p = _profile(con, profile_id)
            snap = _cdp_snapshot(p)
            if not snap.get("reachable"):
                raise HTTPException(409, "Flow browser is not running")
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{int(p['debug_port'])}")
                    pages = [pg for ctx in browser.contexts for pg in ctx.pages]
                    page = next((pg for pg in pages if "flow.google.com" in pg.url), pages[0] if pages else None)
                    if page is None:
                        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                        page = ctx.new_page()
                    page.goto(str(p.get("tool_url") or FLOW_TOOL_URL), wait_until="domcontentloaded", timeout=45000)
                    browser.close()
            except Exception as exc:
                raise HTTPException(502, f"Could not open Flow tool: {exc}")
            return _status(profile_id, True)
        return _status(profile_id, True)

    @app.get("/autoflow/flow/viewer/{profile_id}/{viewer_path:path}")
    def flow_viewer_http(profile_id: int, viewer_path: str, request: Request):
        _require_api(request)
        with _db() as con:
            p = _profile(con, profile_id)
        target_base = f"http://127.0.0.1:{int(p.get('viewer_port') or 0)}"
        safe = "/".join(part for part in viewer_path.split("/") if part not in {"", ".", ".."}) or "vnc.html"
        try:
            upstream = requests.get(f"{target_base}/{safe}", params=dict(request.query_params), timeout=15)
        except requests.RequestException as exc:
            raise HTTPException(503, f"Flow viewer unavailable: {exc}") from exc
        headers = {}
        for key in ("Content-Type", "Cache-Control", "ETag", "Last-Modified"):
            if key in upstream.headers:
                headers[key] = upstream.headers[key]
        return Response(content=upstream.content, status_code=upstream.status_code, headers=headers, media_type=upstream.headers.get("Content-Type"))

    @app.websocket("/autoflow/flow/viewer/{profile_id}/websockify")
    async def flow_viewer_ws(websocket: WebSocket, profile_id: int):
        session = websocket.session
        if not bool(session.get("authenticated")) or (not bool(session.get("is_admin")) and str(session.get("app_access") or "") != "all"):
            await websocket.close(code=4403)
            return
        with _db() as con:
            p = _profile(con, profile_id)
        requested_protocols = [x.strip() for x in str(websocket.headers.get("sec-websocket-protocol") or "").split(",") if x.strip()]
        protocol = requested_protocols[0] if requested_protocols else None
        await websocket.accept(subprotocol=protocol)
        import websockets
        target = f"ws://127.0.0.1:{int(p.get('viewer_port') or 0)}/websockify"
        try:
            async with websockets.connect(target, max_size=None, subprotocols=([protocol] if protocol else None)) as upstream:
                async def client_to_upstream():
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            break
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])
                async def upstream_to_client():
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                done, pending = await asyncio.wait(
                    [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    try: task.result()
                    except Exception: pass
        except Exception:
            try: await websocket.close()
            except Exception: pass

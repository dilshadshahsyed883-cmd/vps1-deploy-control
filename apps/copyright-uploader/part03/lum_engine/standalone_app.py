from __future__ import annotations
import os
import secrets
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth_store import UserStore
from .config import (
    API_VERSION, BASE_PATH, BUILD, COOKIE_SECURE, DATA_ROOT, REPORTS_ROOT, SESSION_SECRET,
    SUITE_AUTH_ENABLED, SUITE_LOGIN_URL, THUMBNAIL_ROOT, ensure_dirs,
)
from .oauth_web import complete_oauth, list_clients, save_client_json, save_shared_client_json, start_oauth
from .project_store import ProjectStore
from .reports import generate_health_report, list_reports, report_path
from .worker import VPSUploadWorker
from desktop_core.modules.auth_manager import AuthManager
from desktop_core.modules.database import Database
from desktop_core.modules.token_utils import classify_google_json, token_status_label

APP_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = APP_ROOT / "templates"
STATIC_ROOT = APP_ROOT / "static"


def route(path: str) -> str:
    path = str(path or "/")
    if not path.startswith("/"):
        path = "/" + path
    if not BASE_PATH:
        return path
    if path == "/":
        return BASE_PATH + "/"
    return BASE_PATH + path


ensure_dirs()
if not SESSION_SECRET:
    raise RuntimeError("LUM_SESSION_SECRET is required")

store = ProjectStore()
users = UserStore()
db = Database()
auth_manager = AuthManager(db)
worker = VPSUploadWorker(store, db)

# LUM_PC_ENGINE_WEB_BRIDGE_V4_IMPORT
from .sheets_bridge import (
    save_sheet_source as web_save_sheet_source,
    delete_sheet_source as web_delete_sheet_source,
    sync_sheet_source_by_id as web_sync_sheet_source_by_id,
    sync_all_sheet_sources as web_sync_all_sheet_sources,
)

# LUM_CHANNELS_PC_SYNC_V5_IMPORTS
from .channels_web import (
    channels_page_context,
    delete_manual_selected,
    install_client_json,
    install_token_json,
    rescan_channel_tokens,
    set_status_bulk,
    test_connections,
)
from .pc_sync_web import apply_snapshot, list_sync_history, save_snapshot_uploads

# LUM_FULL_AUTO_ENGINE_V6_IMPORTS
from pathlib import Path as _V6Path
from fastapi.responses import FileResponse as V6FileResponse, JSONResponse as V6JSONResponse
from desktop_core.modules.token_utils import VALID_TOKEN as V6_VALID_TOKEN, classify_google_json as v6_classify_google_json
from .auto_engine_v6 import (
    V6Store, category_metadata as v6_category_metadata, generate_queue as v6_generate_queue,
    mark_schedule_skipped_today as v6_mark_schedule_skipped_today, planner as v6_planner,
    preflight_profile as v6_preflight_profile, profile_context as v6_profile_context,
    queue_preflight as v6_queue_preflight, save_profile_from_form as v6_save_profile_from_form,
    target_channels as v6_target_channels, parse_schedule_times as v6_parse_schedule_times,
)

app = FastAPI(title="Local Upload Manager", version=BUILD, docs_url=None, redoc_url=None)
v6_store = V6Store()
app.mount(route("/static"), StaticFiles(directory=str(STATIC_ROOT)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_ROOT))
templates.env.globals["base_path"] = BASE_PATH


def redirect(path: str, *, message: str = "", error: str = "") -> RedirectResponse:
    path = route(path)
    query = {}
    if message:
        query["message"] = message
    if error:
        query["error"] = error
    if not query:
        return RedirectResponse(path, status_code=303)
    sep = "&" if "?" in path else "?"
    return RedirectResponse(path + sep + urlencode(query), status_code=303)


def csrf(request: Request) -> str:
    token = str(request.session.get("csrf") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def require_csrf(request: Request, supplied: str) -> None:
    expected = str(request.session.get("csrf") or "")
    if not expected or not secrets.compare_digest(expected, str(supplied or "")):
        raise HTTPException(403, "Invalid CSRF token")


def current_user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "lum_user", None)
    if not user:
        raise HTTPException(401, "Login required")
    return user


def require_admin(user: dict[str, Any]) -> None:
    if not int(user.get("is_admin") or 0):
        raise HTTPException(403, "Administrator access required")


def user_mode(user: dict[str, Any]) -> str:
    mode = str(user.get("effective_ui_mode") or user.get("ui_mode") or "simple").lower().strip()
    return mode if mode in {"simple", "heavy"} else "simple"


def require_heavy(user: dict[str, Any]) -> None:
    if user_mode(user) != "heavy":
        raise HTTPException(403, "Switch to Heavy mode to use this control")


def all_channels() -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM channels ORDER BY id DESC").fetchall()]
    for row in rows:
        token = str(row.get("token_file_path") or "")
        row["token_status"] = token_status_label(classify_google_json(Path(token))) if token else "Missing"
        row["proxy_password"] = "********" if str(row.get("proxy_password") or "") else ""
    return rows


def project_view(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    core_id = int(item.get("core_video_id") or 0)
    if core_id:
        core = db.get_video(core_id) or {}
        if str(item.get("status")) in {"UPLOADING", "UPLOAD_QUEUED", "PAUSED", "FAILED"}:
            item["core_status"] = core.get("status")
            item["progress"] = int(core.get("progress") or item.get("progress") or 0)
            # LUM_WAITING_PROXY_DISPLAY_V1
            if str(item.get("status") or "").upper()=="WAITING_NETWORK" and str(item.get("error_message") or "").startswith("AUTO_PROXY_WAIT|"):
                item["status"]="WAITING_PROXY"
            if core.get("error_message"):
                item["error_message"] = core.get("error_message")
            item["youtube_video_id"] = core.get("youtube_video_id") or item.get("youtube_video_id")
            item["youtube_url"] = core.get("youtube_url") or item.get("youtube_url")
            if str(core.get("recovery_state") or "") == "REVIEW_REQUIRED":
                item["status"] = "REVIEW_REQUIRED"
    return item


def get_projects(user: dict[str, Any], limit: int = 500) -> list[dict[str, Any]]:
    owner = None if int(user.get("is_admin") or 0) else str(user["username"])
    return [project_view(p) for p in store.list(owner=owner, limit=limit)]


def get_logs(user: dict[str, Any], limit: int = 300) -> list[dict[str, Any]]:
    owner = None if int(user.get("is_admin") or 0) else str(user["username"])
    return store.events(owner=owner, limit=limit)


def stats_payload() -> dict[str, Any]:
    channels = all_channels()
    return {
        "counts": store.counts(),
        "channels": len(channels),
        "authenticated_channels": sum(1 for c in channels if str(c.get("token_status") or "").lower().startswith("valid")),
        "active_project_id": worker.active_project_id,
        "engine_enabled": store.setting("engine_enabled", "1") == "1",
        "pause_after_current": store.setting("pause_after_current", "0") == "1",
        "settings": store.settings(),
    }


@app.middleware("http")
async def standalone_auth(request: Request, call_next):
    path = request.url.path
    if path.startswith(route("/static/")) or path == route("/health"):
        return await call_next(request)

    if SUITE_AUTH_ENABLED:
        username = str(request.headers.get("X-Auth-User") or "").strip()
        app_access = str(request.headers.get("X-App-Access") or "").strip().lower()
        is_admin = str(request.headers.get("X-Is-Admin") or "0").strip().lower() in {"1", "true", "yes", "on"}
        if not username:
            return RedirectResponse(SUITE_LOGIN_URL, status_code=303)
        if not is_admin and app_access not in {"lum", "all"}:
            raise HTTPException(403, "Local Upload Manager access required")
        mapped = users.get_by_username(username)
        assigned = str((mapped or {}).get("ui_mode") or "simple").lower().strip()
        if assigned not in {"simple", "heavy"}:
            assigned = "simple"
        user = {
            "id": int((mapped or {}).get("id") or 0),
            "username": username,
            "is_active": 1,
            "is_admin": 1 if is_admin else 0,
            "ui_mode": assigned,
            "suite_authenticated": True,
            "app_access": app_access,
        }
        if is_admin:
            selected = str(request.session.get("lum_admin_mode") or "heavy").lower().strip()
            if selected not in {"simple", "heavy"}:
                selected = "heavy"
            request.session["lum_admin_mode"] = selected
            user["effective_ui_mode"] = selected
            user["mode_access"] = "both"
        else:
            user["effective_ui_mode"] = assigned
            user["mode_access"] = assigned
            request.session.pop("lum_admin_mode", None)
        request.state.lum_user = user
        return await call_next(request)

    if users.count() == 0:
        if path != route("/setup"):
            return RedirectResponse(route("/setup"), status_code=303)
        return await call_next(request)
    if path == route("/setup"):
        return RedirectResponse(route("/login"), status_code=303)
    if path == route("/login"):
        return await call_next(request)
    user_id = int(request.session.get("user_id") or 0)
    user = users.get(user_id) if user_id else None
    if not user or not int(user.get("is_active") or 0):
        request.session.clear()
        return RedirectResponse(route("/login"), status_code=303)
    user = dict(user)
    if int(user.get("is_admin") or 0):
        selected = str(request.session.get("lum_admin_mode") or "heavy").lower().strip()
        if selected not in {"simple", "heavy"}:
            selected = "heavy"
        request.session["lum_admin_mode"] = selected
        user["effective_ui_mode"] = selected
        user["mode_access"] = "both"
    else:
        assigned = user_mode(user).lower().strip()
        if assigned not in {"simple", "heavy"}:
            assigned = "simple"
        user["effective_ui_mode"] = assigned
        user["mode_access"] = assigned
        request.session.pop("lum_admin_mode", None)
    request.state.lum_user = user
    return await call_next(request)


# Session middleware must wrap the authentication middleware so request.session
# is available inside standalone_auth on every protected request.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="lum_session",
    same_site="lax",
    https_only=COOKIE_SECURE,
    max_age=60 * 60 * 24 * 14,
    path=BASE_PATH or "/",
)


@app.on_event("startup")
def startup() -> None:
    worker.start()
    try:
        generate_health_report(store, db, worker)
    except Exception:
        pass


@app.on_event("shutdown")
def shutdown() -> None:
    worker.stop()


@app.get(route("/health"))
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "app": "Local Upload Manager",
        "standalone": not SUITE_AUTH_ENABLED,
        "suite_auth": SUITE_AUTH_ENABLED,
        "build": BUILD,
        "api_version": API_VERSION,
        "worker_alive": bool(worker.thread and worker.thread.is_alive()),
        "active_project_id": worker.active_project_id,
        "serial_concurrency": max(1,min(5,int(V6Store().engine().get("concurrency") or 1))),
        "max_concurrency": 5,
        "multiworker_enabled": True,
        "remote_verification": store.setting("remote_verification_enabled", "1") == "1",
        "verified_cleanup": store.setting("delete_staged_after_verified", "1") == "1",
        "drive_source_delete": False,
        "existing_web_integration": SUITE_AUTH_ENABLED,
        "base_path": BASE_PATH or "/",
        "public_path": route("/"),
    }


@app.get(route("/setup"), response_class=HTMLResponse)
def setup_page(request: Request, error: str = ""):
    if SUITE_AUTH_ENABLED:
        return RedirectResponse(route("/app"), status_code=303)
    return templates.TemplateResponse("setup.html", {"request": request, "error": error, "csrf": csrf(request)})


@app.post(route("/setup"))
def setup_action(request: Request, csrf_token: str = Form(...), username: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    if users.count() != 0:
        return redirect("/login")
    require_csrf(request, csrf_token)
    if password != confirm_password:
        return redirect("/setup", error="Passwords do not match")
    try:
        user_id = users.create(username, password, is_admin=True, ui_mode="heavy")
    except Exception as exc:
        return redirect("/setup", error=str(exc))
    request.session.clear()
    request.session["user_id"] = user_id
    request.session["csrf"] = secrets.token_urlsafe(32)
    request.session["lum_admin_mode"] = "heavy"
    return redirect("/app", message="Administrator account created · Simple and Heavy modes available")


@app.get(route("/login"), response_class=HTMLResponse)
def login_page(request: Request, error: str = "", message: str = ""):
    if SUITE_AUTH_ENABLED:
        return RedirectResponse(SUITE_LOGIN_URL, status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": error, "message": message, "csrf": csrf(request)})


@app.post(route("/login"))
def login_action(request: Request, csrf_token: str = Form(...), username: str = Form(...), password: str = Form(...)):
    require_csrf(request, csrf_token)
    user = users.authenticate(username, password)
    if not user:
        return redirect("/login", error="Invalid username/password or account disabled")
    request.session.clear()
    request.session["user_id"] = int(user["id"])
    request.session["csrf"] = secrets.token_urlsafe(32)
    if int(user.get("is_admin") or 0):
        request.session["lum_admin_mode"] = "heavy"
    return redirect("/app")


@app.post(route("/logout"))
def logout_action(request: Request, csrf_token: str = Form(...)):
    require_csrf(request, csrf_token)
    request.session.clear()
    if SUITE_AUTH_ENABLED:
        return RedirectResponse("/logout", status_code=303)
    return redirect("/login", message="Signed out")


@app.get(route("/"))
def root() -> RedirectResponse:
    return RedirectResponse(route("/app"), status_code=303)


@app.get(route("/app"), response_class=HTMLResponse)
def app_page(request: Request, tab: str = "", message: str = "", error: str = "", embedded: int = 0, source_id: int = 0, auto_profile_id: int = 0, new_profile: int = 0):
    user = current_user(request)
    if SUITE_AUTH_ENABLED and embedded:
        request.session["lum_embedded"] = "1"
    embedded_view = SUITE_AUTH_ENABLED and str(request.session.get("lum_embedded") or "") == "1"
    mode = user_mode(user)
    simple_tabs = {"upload", "authentication"}
    heavy_tabs = {"dashboard", "channels", "sheets", "auto_engine", "add_video", "bulk_import", "queue", "logs"}
    allowed = simple_tabs if mode == "simple" else heavy_tabs
    default_tab = "upload" if mode == "simple" else "dashboard"
    selected = tab if tab in allowed else default_tab
    channels = all_channels()
    projects = get_projects(user)
    logs = get_logs(user) if selected == "logs" else []
    sheet_sources = db.list_sheet_sources() if selected == "sheets" else []
    selected_sheet_source = db.get_sheet_source(int(source_id)) if selected == "sheets" and int(source_id or 0) else None
    web_clients = list_clients()
    channel_context = channels_page_context(db, web_clients) if selected == "channels" else {
        "rows": [], "summary": {}, "groups": [], "sheets": [], "oauth_inventory": {}
    }
    pc_sync_history = list_sync_history() if selected == "channels" else []
    auto_v6 = v6_profile_context(db, v6_store, int(auto_profile_id or 0)) if selected == "auto_engine" else {}
    if selected == "auto_engine" and int(new_profile or 0):
        auto_v6 = dict(auto_v6 or {})
        auto_v6["selected"] = None
        auto_v6["source"] = {}
        auto_v6["videos"] = []
        auto_v6["thumbnails"] = []
        auto_v6["eligible_channels"] = 0
    if selected_sheet_source and not any(int(x.get("id") or 0) == int(selected_sheet_source.get("id") or 0) for x in sheet_sources):
        selected_sheet_source = None
    return templates.TemplateResponse("app.html", {
        "request": request,
        "user": user,
        "mode": mode,
        "tab": selected,
        "message": message,
        "error": error,
        "health": health(),
        "stats": stats_payload(),
        "channels": channels,
        "clients": web_clients,
        "projects": projects,
        "logs": logs,
        "sheet_sources": sheet_sources,
        "selected_sheet_source": selected_sheet_source,
        "channel_rows": channel_context["rows"],
        "channel_summary": channel_context["summary"],
        "channel_groups": channel_context["groups"],
        "channel_sheets": channel_context["sheets"],
        "oauth_inventory": channel_context["oauth_inventory"],
        "pc_sync_history": pc_sync_history,
        "auto_v6": auto_v6,
        "reports": list_reports(),
        "csrf": csrf(request),
        "oauth_url": str(request.session.get("lum_oauth_url") or ""),
        "oauth_session_id": str(request.session.get("lum_oauth_session_id") or ""),
        "build": BUILD,
        "suite_auth": SUITE_AUTH_ENABLED,
        "embedded_view": embedded_view,
    })


@app.get(route("/users"), response_class=HTMLResponse)
def users_page(request: Request, message: str = "", error: str = ""):
    if SUITE_AUTH_ENABLED:
        return RedirectResponse("/user-management", status_code=303)
    user = current_user(request)
    require_admin(user)
    return templates.TemplateResponse("users.html", {
        "request": request, "user": user, "users": users.list(), "csrf": csrf(request),
        "message": message, "error": error, "build": BUILD,
    })


@app.post(route("/users/create"))
def users_create(request: Request, csrf_token: str = Form(...), username: str = Form(...), password: str = Form(...), access_level: str = Form("simple")):
    user = current_user(request); require_admin(user); require_csrf(request, csrf_token)
    try:
        access = str(access_level or "simple").lower().strip()
        if access not in {"simple", "heavy", "administrator"}:
            raise ValueError("Invalid access level")
        is_admin = access == "administrator"
        ui_mode = "heavy" if is_admin else access
        users.create(username, password, is_admin=is_admin, ui_mode=ui_mode)
        label = "Administrator · Both modes" if is_admin else f"{ui_mode.capitalize()} mode"
        return redirect("/users", message=f"User {username} created · {label}")
    except Exception as exc:
        return redirect("/users", error=str(exc))


@app.post(route("/users/{user_id}/update"))
def users_update(user_id: int, request: Request, csrf_token: str = Form(...), access_level: str = Form("simple"), is_active: str = Form("")):
    user = current_user(request); require_admin(user); require_csrf(request, csrf_token)
    try:
        access = str(access_level or "simple").lower().strip()
        if access not in {"simple", "heavy", "administrator"}:
            raise ValueError("Invalid access level")
        is_admin = access == "administrator"
        ui_mode = "heavy" if is_admin else access
        users.update(user_id, ui_mode=ui_mode, is_active=str(is_active).lower() in {"1", "true", "on", "yes"}, is_admin=is_admin)
        return redirect("/users", message="User access updated")
    except Exception as exc:
        return redirect("/users", error=str(exc))


@app.post(route("/mode"))
def switch_mode(request: Request, csrf_token: str = Form(...), mode: str = Form(...)):
    user = current_user(request); require_admin(user); require_csrf(request, csrf_token)
    selected = str(mode or "").lower().strip()
    if selected not in {"simple", "heavy"}:
        raise HTTPException(400, "Invalid mode")
    request.session["lum_admin_mode"] = selected
    default_tab = "upload" if selected == "simple" else "dashboard"
    return redirect(f"/app?tab={default_tab}", message=f"Switched to {selected.capitalize()} mode")


@app.post(route("/users/{user_id}/password"))
def users_password(user_id: int, request: Request, csrf_token: str = Form(...), password: str = Form(...)):
    user = current_user(request); require_admin(user); require_csrf(request, csrf_token)
    try:
        users.set_password(user_id, password)
        return redirect("/users", message="Password changed")
    except Exception as exc:
        return redirect("/users", error=str(exc))


@app.get(route("/reports/{name}"))
def download_report(name: str, request: Request):
    current_user(request)
    try:
        path = report_path(name)
    except Exception:
        raise HTTPException(404, "Report not found")
    return FileResponse(str(path), media_type="text/plain", filename=path.name)


@app.post(route("/actions/report-health"))
def action_report_health(request: Request, csrf_token: str = Form(...)):
    current_user(request); require_csrf(request, csrf_token)
    try:
        path = generate_health_report(store, db, worker)
        return redirect("/app", message=f"Health report created: {path.name}")
    except Exception as exc:
        return redirect("/app", error=f"Health report failed: {exc}")


@app.post(route("/actions/add-video"))
async def add_video(
    request: Request, csrf_token: str = Form(...), drive_url: str = Form(...), title: str = Form(...),
    description: str = Form(""), tags: str = Form(""), category_id: str = Form("22"), privacy_status: str = Form("private"),
    app_upload_at: str = Form(""), made_for_kids: str = Form(""), playlist_id: str = Form(""), channel_db_id: int = Form(...),
    group_name: str = Form(""), thumbnail: UploadFile | None = File(default=None),
):
    user = current_user(request); require_csrf(request, csrf_token)
    thumb_path = ""
    try:
        if thumbnail and thumbnail.filename:
            suffix = Path(thumbnail.filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png"}:
                raise ValueError("Thumbnail must be JPG or PNG")
            payload = await thumbnail.read()
            if len(payload) > 2 * 1024 * 1024:
                raise ValueError("Thumbnail must be 2 MB or smaller")
            temp = THUMBNAIL_ROOT / f"pending_{os.urandom(8).hex()}{suffix}"
            temp.write_bytes(payload); thumb_path = str(temp)
        app_upload_at_utc = app_upload_at
        if str(app_upload_at or "").strip():
            from datetime import datetime as _pkt_datetime, timezone as _pkt_utc
            from zoneinfo import ZoneInfo as _PktZoneInfo
            _pkt_value = _pkt_datetime.fromisoformat(str(app_upload_at).strip().replace("Z", "+00:00"))
            if _pkt_value.tzinfo is None:
                _pkt_value = _pkt_value.replace(tzinfo=_PktZoneInfo("Asia/Karachi"))
            app_upload_at_utc = _pkt_value.astimezone(_pkt_utc.utc).replace(microsecond=0).isoformat()
        project_id = store.create_project({
            "owner_username": str(user["username"]), "ui_mode": user_mode(user),
            "source_mode": "drive_link", "drive_url": drive_url, "thumbnail_path": thumb_path,
            "title": title, "description": description, "tags": tags, "category_id": category_id,
            "privacy_status": privacy_status, "app_upload_at": app_upload_at_utc,
            "made_for_kids": str(made_for_kids).lower() in {"1", "true", "on", "yes"},
            "playlist_id": playlist_id, "channel_db_id": channel_db_id, "group_name": group_name,
        })
        if thumb_path:
            final = THUMBNAIL_ROOT / f"{project_id}{Path(thumb_path).suffix.lower()}"
            Path(thumb_path).replace(final)
            store.update(project_id, thumbnail_path=str(final))
        next_tab = "upload" if user_mode(user) == "simple" else "queue"
        return redirect(f"/app?tab={next_tab}", message=f"Project #{project_id} added to VPS queue")
    except Exception as exc:
        if thumb_path:
            Path(thumb_path).unlink(missing_ok=True)
        next_tab = "upload" if user_mode(user) == "simple" else "add_video"
        return redirect(f"/app?tab={next_tab}", error=str(exc))


@app.post(route("/actions/oauth-client"))
async def upload_oauth_client(request: Request, csrf_token: str = Form(...), client_json: UploadFile = File(...)):
    user = current_user(request); require_csrf(request, csrf_token)
    try:
        result = save_shared_client_json(await client_json.read())
        tab = "authentication" if user_mode(user) == "simple" else "channels"
        return redirect(f"/app?tab={tab}", message=f"Shared OAuth client ready: {result['filename']}")
    except Exception as exc:
        tab = "authentication" if user_mode(user) == "simple" else "channels"
        return redirect(f"/app?tab={tab}", error=str(exc))


@app.post(route("/actions/oauth-start"))
def oauth_start_action(
    request: Request,
    csrf_token: str = Form(...),
    target_channel_db_id: int = Form(...),
):
    user = current_user(request); require_csrf(request, csrf_token)
    tab = "authentication" if user_mode(user) == "simple" else "channels"
    try:
        target_id = int(target_channel_db_id or 0)
        if target_id <= 0:
            raise ValueError("Select a channel before starting Google authorization.")
        target = db.get_channel(target_id)
        if not target:
            raise ValueError("Selected channel no longer exists. Refresh Accounts & Tokens and select it again.")
        expected_channel_id = str(target.get("channel_id") or "").strip()
        if not (expected_channel_id.startswith("UC") and len(expected_channel_id) >= 20):
            raise ValueError("Selected channel does not have a verified UC Channel ID. Add the exact /channel/UC... identity in Sheets, sync it, then authorize.")

        # Desktop parity: identity, browser/profile and proxy all come from the
        # selected channel row. The web form cannot generalize an OAuth result
        # and attach it to a different channel afterwards.
        target_email = str(target.get("gmail") or target.get("email") or "").strip()
        profile_name = str(target.get("browser_profile_name") or target.get("browser_profile") or "").strip()
        proxy_config = {
            "proxy_scheme": str(target.get("proxy_scheme") or "http").strip(),
            "proxy_host": str(target.get("proxy_host") or "").strip(),
            "proxy_port": str(target.get("proxy_port") or "").strip(),
            "proxy_username": str(target.get("proxy_username") or "").strip(),
            "proxy_password": str(target.get("proxy_password") or ""),
        }
        if not proxy_config["proxy_host"]:
            proxy_config = {}

        result = start_oauth(
            "shared_oauth_client",
            proxy_config=proxy_config,
            profile_name=profile_name,
            target_channel_db_id=target_id,
            target_email=target_email,
        )
        request.session["lum_oauth_url"] = result["authorization_url"]
        request.session["lum_oauth_session_id"] = result["session_id"]
        request.session["lum_oauth_target_channel_db_id"] = target_id
        request.session["lum_oauth_target_channel_id"] = expected_channel_id
        request.session["lum_oauth_target_channel_name"] = str(target.get("channel_name") or expected_channel_id)
        return redirect(
            f"/app?tab={tab}",
            message=f"Authorization locked to {target.get('channel_name') or 'selected channel'} ({expected_channel_id}).",
        )
    except Exception as exc:
        return redirect(f"/app?tab={tab}", error=str(exc))


@app.post(route("/actions/oauth-complete"))
def oauth_complete_action(request: Request, csrf_token: str = Form(...), callback_url: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token)
    session_id = str(request.session.get("lum_oauth_session_id") or "")
    tab = "authentication" if user_mode(user) == "simple" else "channels"
    if not session_id:
        return redirect(f"/app?tab={tab}", error="Start authentication first")
    try:
        result = complete_oauth(db, session_id, callback_url)
        request.session.pop("lum_oauth_url", None); request.session.pop("lum_oauth_session_id", None)
        request.session.pop("lum_oauth_target_channel_db_id", None); request.session.pop("lum_oauth_target_channel_id", None); request.session.pop("lum_oauth_target_channel_name", None)
        return redirect(f"/app?tab={tab}", message=f"Authenticated: {result['channel_name']} ({result['channel_id']})")
    except Exception as exc:
        return redirect(f"/app?tab={tab}", error=str(exc))


@app.post(route("/actions/channel-network/{channel_id}"))
def channel_network(channel_id: int, request: Request, csrf_token: str = Form(...), profile_name: str = Form(""), proxy_scheme: str = Form("http"), proxy_host: str = Form(""), proxy_port: str = Form(""), proxy_username: str = Form(""), proxy_password: str = Form("")):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        db.update_channel_network_profile(channel_id, browser_profile_name=profile_name, proxy_scheme=proxy_scheme, proxy_host=proxy_host, proxy_port=proxy_port, proxy_username=proxy_username, proxy_password=proxy_password)
        return redirect("/app?tab=channels", message="Channel network profile updated")
    except Exception as exc:
        return redirect("/app?tab=channels", error=str(exc))


def _owned_or_admin(user: dict[str, Any], project_id: int) -> dict[str, Any]:
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not int(user.get("is_admin") or 0) and str(project.get("owner_username") or "").lower() != str(user.get("username") or "").lower():
        raise HTTPException(403, "Project does not belong to this user")
    return project


@app.post(route("/actions/retry/{project_id}"))
def retry_project(project_id: int, request: Request, csrf_token: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token); project = _owned_or_admin(user, project_id)
    try:
        core_id = int(project.get("core_video_id") or 0)
        if core_id:
            core = db.get_video(core_id) or {}
            if str(core.get("recovery_state") or "") == "REVIEW_REQUIRED":
                raise ValueError("Crash review required before a fresh upload can be authorized")
            if str(core.get("status") or "") == "FAILED":
                db.queue_failed_for_retry(exclude_auto_engine=True, video_ids=[core_id])
            elif str(core.get("status") or "") == "PAUSED":
                db.resume_paused(exclude_auto_engine=True)
        if not store.retry(project_id):
            raise ValueError("Project is not retryable")
        tab = "upload" if user_mode(user) == "simple" else "queue"
        return redirect(f"/app?tab={tab}", message=f"Project #{project_id} returned to queue")
    except Exception as exc:
        tab = "upload" if user_mode(user) == "simple" else "queue"
        return redirect(f"/app?tab={tab}", error=str(exc))


@app.post(route("/actions/authorize-fresh/{project_id}"))
def authorize_fresh(project_id: int, request: Request, csrf_token: str = Form(...), confirm_text: str = Form("")):
    user = current_user(request); require_csrf(request, csrf_token); project = _owned_or_admin(user, project_id)
    if confirm_text.strip().upper() != "NO REMOTE VIDEO":
        return redirect("/app?tab=queue", error="Type NO REMOTE VIDEO only after checking YouTube Studio")
    try:
        core_id = int(project.get("core_video_id") or 0)
        if not core_id or not db.resolve_recovery_review([core_id]):
            raise ValueError("Project is not awaiting crash review")
        store.update(project_id, status="QUEUED", error_message="User confirmed no remote upload exists; fresh retry authorized.")
        store.event(project_id, "Fresh retry authorized after manual YouTube Studio review", "WARNING")
        return redirect("/app?tab=queue", message=f"Fresh retry authorized for project #{project_id}")
    except Exception as exc:
        return redirect("/app?tab=queue", error=str(exc))


@app.post(route("/actions/pause-after-current"))
def pause_after_current(request: Request, csrf_token: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    store.set_setting("pause_after_current", "1")
    return redirect("/app?tab=queue", message="Uploader will pause after the current project")


@app.post(route("/actions/stop-current"))
def stop_current(request: Request, csrf_token: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    worker.request_stop_current()
    return redirect("/app?tab=queue", message="Safe stop requested for the current upload")


@app.post(route("/actions/resume"))
def resume(request: Request, csrf_token: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    store.set_setting("engine_enabled", "1"); store.set_setting("pause_after_current", "0")
    return redirect("/app?tab=queue", message="Uploader resumed")



# LUM_PC_ENGINE_WEB_BRIDGE_V4_ROUTES
@app.post(route("/actions/sheets/save"))
def sheets_save(
    request: Request,
    csrf_token: str = Form(...),
    source_id: str = Form(""),
    sheet_name: str = Form(""),
    sheet_url_or_id: str = Form(...),
    enabled: str = Form(""),
):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        sid = int(source_id) if str(source_id or "").strip() else None
        saved_id = web_save_sheet_source(
            db,
            source_id=sid,
            sheet_name=sheet_name,
            sheet_url_or_id=sheet_url_or_id,
            enabled=str(enabled).lower() in {"1", "true", "on", "yes"},
        )
        return redirect(
            f"/app?tab=sheets&source_id={saved_id}",
            message="Sheet source saved.",
        )
    except Exception as exc:
        return redirect(
            f"/app?tab=sheets" + (f"&source_id={source_id}" if str(source_id or "").strip().isdigit() else ""),
            error=str(exc),
        )


@app.post(route("/actions/sheets/delete"))
def sheets_delete(
    request: Request,
    csrf_token: str = Form(...),
    source_id: int = Form(...),
):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        web_delete_sheet_source(db, int(source_id))
        return redirect(
            "/app?tab=sheets",
            message="Sheet source removed from current inventory. Saved token/history data was retained.",
        )
    except Exception as exc:
        return redirect(
            f"/app?tab=sheets&source_id={int(source_id)}",
            error=str(exc),
        )


@app.post(route("/actions/sheets/sync"))
def sheets_sync(
    request: Request,
    csrf_token: str = Form(...),
    source_id: int = Form(...),
):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    if store.setting("sheets_sync_enabled", "0") != "1":
        return redirect(
            f"/app?tab=sheets&source_id={int(source_id)}",
            error="Enable Sheets Sync on Dashboard before running synchronization.",
        )
    result = web_sync_sheet_source_by_id(db, int(source_id))
    if result.get("ok"):
        return redirect(
            f"/app?tab=sheets&source_id={int(source_id)}",
            message=f"Sheet synced · {int(result.get('rows') or 0)} row(s) · {int(result.get('groups_touched') or 0)} group(s).",
        )
    return redirect(
        f"/app?tab=sheets&source_id={int(source_id)}",
        error=str(result.get("error") or result.get("status") or "Sheet sync failed."),
    )


@app.post(route("/actions/sheets/sync-all"))
def sheets_sync_all(request: Request, csrf_token: str = Form(...)):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    if store.setting("sheets_sync_enabled", "0") != "1":
        return redirect(
            "/app?tab=sheets",
            error="Enable Sheets Sync on Dashboard before running synchronization.",
        )
    try:
        result = web_sync_all_sheet_sources(db)
    except Exception as exc:
        return redirect("/app?tab=sheets", error=str(exc))
    if result.get("ok"):
        return redirect(
            "/app?tab=sheets",
            message=f"Updated {int(result.get('count') or 0)} enabled sheet(s) · {int(result.get('rows') or 0)} row(s).",
        )
    failures = [
        str(r.get("sheet_name") or r.get("source_id") or "Sheet")
        for r in result.get("results", [])
        if not r.get("ok")
    ]
    return redirect(
        "/app?tab=sheets",
        error=f"Sync completed with {int(result.get('failed') or 0)} failed source(s): " + ", ".join(failures[:5]),
    )



# LUM_CHANNELS_PC_SYNC_V5_ROUTES
@app.post(route("/actions/channels/status"))
def channels_status(
    request: Request,
    csrf_token: str = Form(...),
    status: str = Form(...),
    channel_ids: list[int] = Form([]),
):
    user=current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        updated=set_status_bulk(db, channel_ids, status)
        return redirect("/app?tab=channels", message=f"Updated {updated} channel(s) to {str(status).upper()}.")
    except Exception as exc:
        return redirect("/app?tab=channels", error=str(exc))


@app.post(route("/actions/channels/delete-manual"))
def channels_delete_manual(
    request: Request,
    csrf_token: str = Form(...),
    channel_ids: list[int] = Form([]),
):
    user=current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        result=delete_manual_selected(db, channel_ids)
        return redirect(
            "/app?tab=channels",
            message=(
                f"Deleted manual channels: {int(result.get('deleted') or 0)} · "
                f"Blocked by queue/history: {int(result.get('blocked_with_queue') or 0)} · "
                f"Sheet rows skipped: {int(result.get('not_manual') or 0)}"
            ),
        )
    except Exception as exc:
        return redirect("/app?tab=channels", error=str(exc))


@app.post(route("/actions/channels/rescan-tokens"))
def channels_rescan_tokens(
    request: Request,
    csrf_token: str = Form(...),
    scope: str = Form("selected"),
    channel_ids: list[int] = Form([]),
):
    user=current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        ids=None if str(scope).lower()=="all_missing" else channel_ids
        result=rescan_channel_tokens(db, ids)
        return redirect(
            "/app?tab=channels",
            message=(
                f"Token scan · checked {result['checked']} · updated {result['updated']} · "
                f"missing {result['missing']} · invalid {result['invalid']}"
            ),
        )
    except Exception as exc:
        return redirect("/app?tab=channels", error=str(exc))


@app.post(route("/actions/channels/test-connections"))
def channels_test_connections(
    request: Request,
    csrf_token: str = Form(...),
    channel_ids: list[int] = Form([]),
):
    user=current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    try:
        result=test_connections(db, channel_ids)
        return redirect(
            "/app?tab=channels",
            message=f"Connection tests · {result['passed']} passed · {result['failed']} failed.",
        )
    except Exception as exc:
        return redirect("/app?tab=channels", error=str(exc))


@app.post(route("/actions/channels/json-upload"))
async def channels_json_upload(
    request: Request,
    csrf_token: str = Form(...),
    secret_kind: str = Form(...),
    replace_existing: str = Form("0"),
    files: list[UploadFile] = File(...),
):
    user=current_user(request); require_admin(user); require_csrf(request, csrf_token); require_heavy(user)
    kind=str(secret_kind or "").strip().lower()
    if kind not in {"client","token"}:
        return redirect("/app?tab=channels", error="Invalid JSON storage type.")
    installed=kept=invalid=conflict=0
    errors=[]
    replace=str(replace_existing).lower() in {"1","true","on","yes"}
    for upload in files[:250]:
        try:
            payload=await upload.read()
            if kind=="client":
                item=install_client_json(upload.filename or "client.json", payload, replace_valid=replace)
                if item.get("status")=="INSTALLED":
                    installed+=1
                    try:
                        save_client_json(upload.filename or item.get("name") or "client.json", payload)
                    except Exception as exc:
                        errors.append(f"Web OAuth mirror {upload.filename}: {exc}")
                elif item.get("status")=="KEPT": kept+=1
                else:
                    invalid+=1; errors.append(f"{upload.filename}: {item.get('reason')}")
            else:
                item=install_token_json(db, upload.filename or "token.json", payload, replace_valid=replace)
                if item.get("status")=="INSTALLED": installed+=1
                elif item.get("status")=="KEPT": kept+=1
                elif item.get("status") in {"CONFLICT","UNMATCHED"}:
                    conflict+=1; errors.append(f"{upload.filename}: {item.get('reason')}")
                else:
                    invalid+=1; errors.append(f"{upload.filename}: {item.get('reason')}")
        except Exception as exc:
            invalid+=1; errors.append(f"{upload.filename}: {exc}")
    if kind=="token":
        try: rescan_channel_tokens(db, None)
        except Exception as exc: errors.append(f"Post-upload token scan: {exc}")
    msg=f"{'OAuth clients' if kind=='client' else 'Channel tokens'} · installed {installed} · kept {kept} · invalid {invalid} · conflicts {conflict}"
    if errors:
        return redirect("/app?tab=channels", error=msg+" · "+"; ".join(errors[:3]))
    return redirect("/app?tab=channels", message=msg)


@app.post(route("/actions/pc-sync/upload"))
async def pc_sync_upload(
    request: Request,
    csrf_token: str = Form(...),
    source_label: str = Form(""),
    files: list[UploadFile] = File(...),
):
    user=current_user(request); require_admin(user); require_csrf(request, csrf_token); require_heavy(user)
    payloads=[]
    total=0
    for upload in files[:5000]:
        data=await upload.read()
        total+=len(data)
        if total>2*1024*1024*1024:
            raise HTTPException(413, "PC Sync snapshot exceeds 2 GB safety limit.")
        payloads.append((upload.filename or "upload.bin", data))
    return save_snapshot_uploads(db, payloads, source_label)


@app.post(route("/actions/pc-sync/apply"))
def pc_sync_apply(
    request: Request,
    csrf_token: str = Form(...),
    snapshot_id: str = Form(...),
):
    user=current_user(request); require_admin(user); require_csrf(request, csrf_token); require_heavy(user)
    return apply_snapshot(db, snapshot_id)





# LUM_FULL_AUTO_ENGINE_V6_ROUTES

def _v6_selected_ids(form, key: str = "video_ids"):
    try:
        return sorted({int(x) for x in form.getlist(key) if str(x).strip().isdigit() and int(x)>0})
    except Exception:
        value=form.get(key) if hasattr(form,"get") else None
        return [int(value)] if str(value or "").isdigit() else []


def _v6_profile(profile_id: int):
    profile=db.get_auto_engine_profile(int(profile_id))
    if not profile:
        raise ValueError("Auto Engine profile not found.")
    return profile


@app.post(route("/actions/auto-v6/profile/save"))
async def auto_v6_profile_save(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    try:
        raw=str(form.get("profile_id") or "").strip()
        pid=int(raw) if raw.isdigit() else None
        saved=v6_save_profile_from_form(db,v6_store,dict(form),pid)
        return redirect(f"/app?tab=auto_engine&auto_profile_id={saved}",message=f"Auto Engine profile #{saved} saved.")
    except Exception as exc:
        return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/profile/toggle"))
def auto_v6_profile_toggle(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...),enabled: str=Form("0")):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        state=str(enabled).lower() in {"1","true","on","yes"}
        db.set_auto_engine_profile_enabled(int(profile_id),state)
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",message=f"Profile {'enabled' if state else 'disabled'}.")
    except Exception as exc:
        return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/profile/delete"))
def auto_v6_profile_delete(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        nonterminal=[j for j in db.list_auto_engine_jobs(limit=5000) if int(j.get("auto_engine_profile_id") or 0)==int(profile_id) and str(j.get("status") or "").upper() not in {"UPLOADED","PUBLISHED","FAILED","CANCELLED"}]
        if nonterminal: raise ValueError("Profile still has queued/running/review jobs. Finish or safely remove those jobs before deleting the profile.")
        db.delete_auto_engine_profile(int(profile_id))
        return redirect("/app?tab=auto_engine",message="Profile removed. Its permanent thumbnail files/history were retained for recovery.")
    except Exception as exc:
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",error=str(exc))


@app.post(route("/actions/auto-v6/sync-scan"))
def auto_v6_sync_scan(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        profile=_v6_profile(profile_id)
        result=v6_preflight_profile(db,v6_store,profile)
        return V6JSONResponse(result)
    except Exception as exc:
        return V6JSONResponse({"ok":False,"errors":[str(exc)],"warnings":[]},status_code=400)


@app.post(route("/actions/auto-v6/queue-preflight"))
def auto_v6_queue_preflight(request: Request,csrf_token: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        return V6JSONResponse(v6_queue_preflight(db,v6_store))
    except Exception as exc:
        return V6JSONResponse({"ok":False,"issues":[str(exc)],"warnings":[]},status_code=400)


@app.get(route("/actions/auto-v6/preview"))
def auto_v6_preview(request: Request,profile_id: int):
    current_user(request); require_heavy(current_user(request))
    try:
        result=v6_planner(db,v6_store,_v6_profile(profile_id),preview_only=True)
        return V6JSONResponse(result)
    except Exception as exc:
        return V6JSONResponse({"ready":False,"jobs":[],"blocked":[str(exc)]},status_code=400)


@app.post(route("/actions/auto-v6/generate"))
def auto_v6_generate(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...),confirmation: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        if str(confirmation).strip()!="GENERATE": raise ValueError('Type GENERATE exactly to create real queue jobs.')
        profile=_v6_profile(profile_id)
        result=v6_generate_queue(db,v6_store,profile)
        v6_mark_schedule_skipped_today(db,profile)
        if int(result.get("created") or 0) and str(profile.get("work_mode") or "") == "auto":
            v6_store.set_engine(running=True,pause_after_current=False)
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",message=f"Generated {int(result.get('created') or 0)} job(s); skipped {int(result.get('skipped_jobs') or 0)} ineligible job(s); duplicate blocks {int(result.get('duplicates_blocked') or 0)}.")
    except Exception as exc:
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",error=str(exc))


# LUM_AJAX_GENERATE_STATUS_V1
@app.post(route("/actions/auto-v6/generate-json"))
def auto_v6_generate_json(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...),confirmation: str=Form("GENERATE")):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    pid=int(profile_id)
    try:
        if str(confirmation).strip()!="GENERATE":
            raise ValueError("Generate confirmation is invalid.")
        profile=_v6_profile(pid)
        result=v6_generate_queue(db,v6_store,profile)
        v6_mark_schedule_skipped_today(db,profile)
        created=int(result.get("created") or 0)
        if created and str(profile.get("work_mode") or "") == "auto":
            v6_store.set_engine(running=True,pause_after_current=False)
        rows=db.list_auto_engine_jobs(limit=5000)
        terminal={"UPLOADED","PUBLISHED","FAILED","CANCELLED"}
        active=[j for j in rows if str(j.get("status") or "").upper() not in terminal]
        profile_active=[j for j in active if int(j.get("auto_engine_profile_id") or 0)==pid]
        videos=len(v6_store.drive_videos(pid,True))
        thumbs=sum(1 for t in v6_store.thumbs(pid) if str(t.get("status") or "").upper()=="ACTIVE")
        if created<=0:
            return V6JSONResponse({
                "ok":False,"profile_id":pid,"created":0,
                "duplicates_blocked":int(result.get("duplicates_blocked") or 0),
                "queue_total":len(active),"profile_queue_total":len(profile_active),
                "available_videos":videos,"active_thumbnails":thumbs,
                "errors":["No new queue jobs were created. The selected items may already be reserved or blocked as duplicates. Run Preview to see eligibility."],
            },status_code=409)
        return V6JSONResponse({
            "ok":True,"profile_id":pid,"created":created,
            "duplicates_blocked":int(result.get("duplicates_blocked") or 0),
            "target_jobs":int(result.get("target_jobs") or 0),
            "eligible_jobs":int(result.get("eligible_jobs") or created),
            "skipped_jobs":int(result.get("skipped_jobs") or 0),
            "warnings":list(result.get("warnings") or []),
            "queue_total":len(active),"profile_queue_total":len(profile_active),
            "available_videos":videos,"active_thumbnails":thumbs,
            "message":f"Generated {created} queue job(s); skipped {int(result.get('skipped_jobs') or 0)} ineligible job(s).",
        })
    except Exception as exc:
        return V6JSONResponse({"ok":False,"profile_id":pid,"created":0,"errors":[str(exc)]},status_code=400)


@app.post(route("/actions/auto-v6/start-all"))
def auto_v6_start_all(request: Request,csrf_token: str=Form(...),confirmation: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    if str(confirmation).strip()!="START ALL":
        return redirect("/app?tab=auto_engine",error='Type START ALL exactly.')
    created=0; skipped=[]; partial_skipped=0
    for profile in db.list_auto_engine_profiles():
        if int(profile.get("enabled") or 0)!=1: continue
        try:
            result=v6_generate_queue(db,v6_store,profile)
            created+=int(result.get("created") or 0)
            partial_skipped+=int(result.get("skipped_jobs") or 0)
            v6_mark_schedule_skipped_today(db,profile)
        except Exception as exc:
            skipped.append(f"{profile.get('category_name')}: {exc}")
    if created: v6_store.set_engine(running=True,pause_after_current=False)
    text=f"Start All created {created} job(s)."
    if partial_skipped: text+=f" {partial_skipped} ineligible job(s) skipped without blocking their profiles."
    if skipped: text+=f" {len(skipped)} profile(s) skipped by fatal profile-level errors: "+"; ".join(skipped[:3])
    return redirect("/app?tab=auto_engine",message=text)


@app.post(route("/actions/auto-v6/engine"))
def auto_v6_engine(request: Request,csrf_token: str=Form(...),action: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    act=str(action or "").strip().lower()
    if act in {"start","resume"}: v6_store.set_engine(running=True,pause_after_current=False,error="")
    elif act=="pause_after_current": v6_store.set_engine(running=True,pause_after_current=True)
    elif act=="stop": v6_store.set_engine(running=False,pause_after_current=False)
    elif act=="arm_scheduler": v6_store.set_scheduler_armed(True)
    elif act=="disarm_scheduler": v6_store.set_scheduler_armed(False)
    else: return redirect("/app?tab=auto_engine",error="Unknown Auto Engine control.")
    return redirect("/app?tab=auto_engine",message=f"Auto Engine control applied: {act.replace('_',' ')}.")


@app.post(route("/actions/auto-v6/concurrency"))
def auto_v6_concurrency(
    request: Request,
    csrf_token: str=Form(...),
    concurrency: int=Form(1),
    profile_id: int=Form(0),
):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        saved=v6_store.set_concurrency(int(concurrency))
        suffix=f"&auto_profile_id={int(profile_id)}" if int(profile_id or 0)>0 else ""
        return redirect(f"/app?tab=auto_engine{suffix}",message=f"Concurrent Uploads set to {saved}. Effective worker pool updates dynamically from 1 to 5.")
    except Exception as exc:
        return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/profile-schedules/save"))
async def auto_v6_profile_schedules_save(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    updated=0
    errors=[]
    current_raw=str(form.get("current_profile_id") or "0")
    current_profile_id=int(current_raw) if current_raw.isdigit() else 0
    for profile in db.list_auto_engine_profiles():
        pid=int(profile.get("id") or 0)
        try:
            raw_enabled=str(form.get(f"schedule_enabled_{pid}") or "0").lower()
            raw_times=str(form.get(f"schedule_times_{pid}") or profile.get("schedule_times") or "09:00")
            times=",".join(v6_parse_schedule_times(raw_times))
            data=dict(profile)
            data["schedule_enabled"]=raw_enabled in {"1","true","on","yes"}
            data["schedule_times"]=times
            db.save_auto_engine_profile(data,pid)
            updated+=1
        except Exception as exc:
            errors.append(f"{profile.get('category_name') or pid}: {exc}")
    suffix=f"&auto_profile_id={current_profile_id}" if current_profile_id>0 else ""
    if errors:
        return redirect(f"/app?tab=auto_engine{suffix}",error=f"Updated {updated} schedule(s); "+"; ".join(errors[:3]))
    return redirect(f"/app?tab=auto_engine{suffix}",message=f"Profile schedules updated for {updated} profile(s).")


@app.post(route("/actions/auto-v6/retry-failed"))
async def auto_v6_retry_failed(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    ids=_v6_selected_ids(form)
    candidates=[j for j in db.list_auto_engine_jobs(limit=5000) if str(j.get("status") or "")=="FAILED" and str(j.get("recovery_state") or "")!="REVIEW_REQUIRED"]
    if ids: candidates=[j for j in candidates if int(j.get("id") or 0) in ids]
    safe=[]; blocked=0
    for job in candidates:
        current=db.get_video(int(job["id"])) or {}
        if str(current.get("channel_status") or "")!="ACTIVE": blocked+=1; continue
        if v6_classify_google_json(str(current.get("token_file_path") or ""))!=V6_VALID_TOKEN: blocked+=1; continue
        safe.append(int(job["id"]))
    count=db.queue_failed_for_retry(auto_engine_only=True,video_ids=safe) if safe else 0
    if count: v6_store.set_engine(running=True,pause_after_current=False)
    return redirect("/app?tab=auto_engine",message=f"Retry queued {count}; blocked by channel/token safety {blocked}.")


@app.post(route("/actions/auto-v6/resume-paused"))
def auto_v6_resume_paused(request: Request,csrf_token: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    count=db.resume_paused(auto_engine_only=True)
    if count: v6_store.set_engine(running=True,pause_after_current=False)
    return redirect("/app?tab=auto_engine",message=f"Resumed {count} safe paused job(s).")


@app.post(route("/actions/auto-v6/clear-done"))
def auto_v6_clear_done(request: Request,csrf_token: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    count=db.clear_completed_auto_engine_jobs()
    return redirect("/app?tab=auto_engine",message=f"Cleared {count} completed queue row(s). Permanent upload/script history was retained.")


@app.post(route("/actions/auto-v6/remove-selected"))
async def auto_v6_remove_selected(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    ids=_v6_selected_ids(form)
    count=db.delete_auto_engine_jobs(ids)
    with v6_store.connect() as con:
        for vid in ids:
            if not db.get_video(vid):
                con.execute("UPDATE job_sources SET state='CANCELLED',updated_at=CURRENT_TIMESTAMP WHERE video_id=?",(vid,))
    return redirect("/app?tab=auto_engine",message=f"Removed {count} safe queue job(s). Active uploads were protected.")


@app.post(route("/actions/auto-v6/recovery-clear"))
async def auto_v6_recovery_clear(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    if str(form.get("confirmation") or "").strip()!="I CHECKED YOUTUBE":
        return redirect("/app?tab=auto_engine",error='Type I CHECKED YOUTUBE exactly after checking YouTube Studio.')
    ids=_v6_selected_ids(form)
    count=db.resolve_recovery_review(ids)
    with v6_store.connect() as con:
        for vid in ids:
            con.execute("UPDATE job_sources SET state='QUEUED',last_error='',updated_at=CURRENT_TIMESTAMP WHERE video_id=?",(vid,))
    return redirect("/app?tab=auto_engine",message=f"Recovery review cleared for {count} job(s).")


@app.post(route("/actions/auto-v6/thumbnails/upload"))
async def auto_v6_thumbnail_upload(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...),files: list[UploadFile]=File(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    profile=_v6_profile(profile_id); installed=duplicates=invalid=0; errors=[]
    source=v6_store.source(int(profile_id))
    if str(source.get("thumbnail_source_mode") or "pc_drive") == "drive":
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",error="This profile is Drive-only for thumbnails. Change Thumbnail Source Mode to PC or PC + Drive before uploading from PC.")
    for item in files[:500]:
        try:
            data=await item.read()
            result=v6_store.add_thumbnail_bytes(int(profile_id),str(profile.get("category_name") or ""),item.filename or "thumb.jpg",data,"PC")
            if result.get("status")=="INSTALLED": installed+=1
            else: duplicates+=1
        except Exception as exc:
            invalid+=1; errors.append(f"{item.filename}: {exc}")
    message=f"PC thumbnails: installed {installed}, duplicates {duplicates}, invalid {invalid}."
    return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",message=message if not errors else message+" "+"; ".join(errors[:2]))


@app.post(route("/actions/auto-v6/thumbnails/sync-drive"))
def auto_v6_thumbnail_sync_drive(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    try:
        p=_v6_profile(profile_id); source=v6_store.source(int(profile_id))
        if str(source.get("thumbnail_source_mode") or "pc_drive") == "pc":
            raise ValueError("This profile is PC-upload-only for thumbnails. Change Thumbnail Source Mode to Drive or PC + Drive before syncing Drive thumbnails.")
        result=v6_store.sync_drive_thumbnails(int(profile_id),str(p.get("category_name") or ""))
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",message=f"Drive thumbnails · installed {result.get('installed',0)} · duplicates {result.get('duplicates',0)} · source removed {result.get('removed_marked',0)}.")
    except Exception as exc:
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",error=str(exc))


@app.post(route("/actions/auto-v6/thumbnails/status"))
async def auto_v6_thumbnail_status(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    ids=_v6_selected_ids(form,"asset_ids"); status=str(form.get("status") or "").upper()
    try:
        count=v6_store.set_thumb_status(ids,status)
        return redirect("/app?tab=auto_engine",message=f"Updated {count} thumbnail(s) to {status}.")
    except Exception as exc: return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/thumbnails/move"))
async def auto_v6_thumbnail_move(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    ids=_v6_selected_ids(form,"asset_ids"); target_id=int(form.get("target_profile_id") or 0)
    try:
        target=_v6_profile(target_id); moved,blocked,duplicates=v6_store.move_thumbs(ids,target_id,str(target.get("category_name") or ""),db)
        return redirect(f"/app?tab=auto_engine&auto_profile_id={target_id}",message=f"Moved {moved} thumbnail(s) to {target.get('category_name')} · live-job protected {len(blocked)} · duplicates skipped {len(duplicates)}.")
    except Exception as exc: return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/thumbnails/trash"))
async def auto_v6_thumbnail_trash(request: Request):
    user=current_user(request); require_heavy(user)
    form=await request.form(); require_csrf(request,str(form.get("csrf_token") or ""))
    ids=_v6_selected_ids(form,"asset_ids")
    moved,blocked=v6_store.trash_thumbs(ids,db)
    return redirect("/app?tab=auto_engine",message=f"Moved {moved} thumbnail(s) to 7-day Trash; {len(blocked)} attached to live queue and protected.")


@app.post(route("/actions/auto-v6/thumbnails/restore"))
def auto_v6_thumbnail_restore(request: Request,csrf_token: str=Form(...),asset_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    asset=v6_store.thumb(int(asset_id)); name=""
    if asset:
        p=db.get_auto_engine_profile(int(asset.get("profile_id") or 0)); name=str((p or {}).get("category_name") or "")
    ok=v6_store.restore_thumb(int(asset_id),name)
    return redirect("/app?tab=auto_engine",message="Thumbnail restored as DISABLED; enable it when ready." if ok else "Thumbnail could not be restored.")


@app.post(route("/actions/auto-v6/thumbnails/empty-trash"))
def auto_v6_thumbnail_empty_trash(request: Request,csrf_token: str=Form(...),confirmation: str=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    if str(confirmation or "").strip() != "EMPTY TRASH":
        return redirect("/app?tab=auto_engine",error='Type EMPTY TRASH exactly to permanently remove trashed thumbnails.')
    count=v6_store.empty_trash()
    return redirect("/app?tab=auto_engine",message=f"Permanently deleted {count} thumbnail(s) from Trash.")


@app.post(route("/actions/auto-v6/thumbnails/reset-rotation"))
def auto_v6_thumbnail_reset_rotation(request: Request,csrf_token: str=Form(...),profile_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    with db.connect() as con: con.execute("UPDATE auto_engine_profiles SET thumbnail_cursor=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",(int(profile_id),))
    return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile_id)}",message="Thumbnail rotation cursor reset to #1.")


@app.get(route("/actions/auto-v6/thumbnail/{asset_id}"))
def auto_v6_thumbnail_file(request: Request,asset_id: int):
    user=current_user(request); require_heavy(user)
    asset=v6_store.thumb(int(asset_id))
    if not asset: raise HTTPException(404,"Thumbnail not found.")
    path=_V6Path(str(asset.get("stored_path") or ""))
    if not path.is_file(): raise HTTPException(404,"Thumbnail file is missing.")
    media={".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",".webp":"image/webp"}.get(path.suffix.lower(),"application/octet-stream")
    return V6FileResponse(str(path),media_type=media,headers={"Content-Disposition":f'inline; filename="{path.name}"'})


@app.get(route("/actions/auto-v6/upload-records.csv"))
def auto_v6_upload_records_csv(request: Request):
    user=current_user(request); require_heavy(user)
    v6_store.import_pc_filename_history(db)
    path=v6_store.export_records_csv()
    return V6FileResponse(str(path),filename="auto_engine_upload_records.csv",media_type="text/csv")


@app.post(route("/actions/auto-v6/missed/run"))
def auto_v6_missed_run(request: Request,csrf_token: str=Form(...),missed_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    with v6_store.connect() as con: row=con.execute("SELECT * FROM missed_alarms WHERE id=? AND state='REVIEW_REQUIRED'",(int(missed_id),)).fetchone()
    if not row: return redirect("/app?tab=auto_engine",error="Missed alarm review item not found.")
    item=dict(row); profile=_v6_profile(int(item["profile_id"])); channels=v6_target_channels(db,profile)
    max_jobs=len(channels) if str(profile.get("upload_scope") or "per_channel")=="per_channel" else 1
    try:
        result=v6_generate_queue(db,v6_store,profile,run_key=str(item["run_key"]),max_jobs_override=max_jobs)
        v6_store.resolve_missed(int(missed_id),"RUN")
        if int(result.get("created") or 0): v6_store.set_engine(running=True,pause_after_current=False)
        return redirect(f"/app?tab=auto_engine&auto_profile_id={int(profile['id'])}",message=f"Missed alarm run now: created {int(result.get('created') or 0)} job(s); skipped {int(result.get('skipped_jobs') or 0)} ineligible job(s).")
    except Exception as exc: return redirect("/app?tab=auto_engine",error=str(exc))


@app.post(route("/actions/auto-v6/missed/skip"))
def auto_v6_missed_skip(request: Request,csrf_token: str=Form(...),missed_id: int=Form(...)):
    user=current_user(request); require_csrf(request,csrf_token); require_heavy(user)
    with v6_store.connect() as con: row=con.execute("SELECT * FROM missed_alarms WHERE id=? AND state='REVIEW_REQUIRED'",(int(missed_id),)).fetchone()
    if not row: return redirect("/app?tab=auto_engine",error="Missed alarm review item not found.")
    item=dict(row); db.record_auto_engine_run(int(item["profile_id"]),str(item["run_key"]),0); v6_store.resolve_missed(int(missed_id),"SKIPPED")
    return redirect("/app?tab=auto_engine",message="Missed alarm skipped for today.")


@app.post(route("/actions/settings"))
def save_settings(request: Request, csrf_token: str = Form(...), sheets_sync_enabled: str = Form("0"), auto_engine_enabled: str = Form("0"), bulk_import_enabled: str = Form("0"), delete_staged_after_verified: str = Form("1")):
    user = current_user(request); require_csrf(request, csrf_token); require_heavy(user)
    for key, value in {
        "sheets_sync_enabled": sheets_sync_enabled,
        "auto_engine_enabled": auto_engine_enabled,
        "bulk_import_enabled": bulk_import_enabled,
        "delete_staged_after_verified": delete_staged_after_verified,
    }.items():
        store.set_setting(key, "1" if str(value).lower() in {"1", "true", "on", "yes"} else "0")
    store.set_setting("max_active_uploads", "1")
    store.set_setting("remote_verification_enabled", "1")
    store.set_setting("drive_delete_source_after_verified", "0")
    return redirect("/app?tab=dashboard", message="Settings saved")

# LUM_DUAL_AUTH_CONNECTIONS_V2_ROUTES
from fastapi import HTTPException as _DualAuthHTTPException
from fastapi.responses import JSONResponse as _DualAuthJSONResponse, RedirectResponse as _DualAuthRedirectResponse
from .dual_auth_connections import DualAuthConnections as _DualAuthConnections


# Initialize at application import so schema, queue snapshots, and the legacy
# compatibility projection exist before worker traffic reaches the app.
_dual_auth = _DualAuthConnections(db)


def _dual_auth_is_admin(user):
    if isinstance(user, dict):
        return bool(user.get("is_admin"))
    return bool(getattr(user, "is_admin", False))


@app.get(route("/actions/auth-connections/status"))
def dual_auth_status(request: Request):
    current_user(request)
    payload = _dual_auth.status()
    payload.update(_dual_auth.inventory())
    return _DualAuthJSONResponse(payload)


@app.post(route("/actions/google-oauth/configure"))
async def dual_auth_google_configure(request: Request):
    user = current_user(request)
    if not _dual_auth_is_admin(user):
        raise _DualAuthHTTPException(status_code=403, detail="Administrator access required.")
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    try:
        status = _dual_auth.save_client(
            str(form.get("client_id") or ""),
            str(form.get("client_secret") or ""),
        )
        return redirect("/app?tab=authentication", message=f"Google Web OAuth app saved securely ({status.get('client_id_hint') or 'configured'}).")
    except Exception as exc:
        return redirect("/app?tab=authentication", error=str(exc))


@app.post(route("/actions/google-oauth/connect"))
async def dual_auth_google_connect(request: Request):
    user = current_user(request)
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    try:
        url = _dual_auth.begin(int(str(form.get("channel_db_id") or "0")), user=user)
        return _DualAuthRedirectResponse(url=url, status_code=303)
    except Exception as exc:
        return redirect("/app?tab=authentication", error=str(exc))


@app.get(route("/oauth/google/callback"))
def dual_auth_google_callback(request: Request):
    user = current_user(request)
    try:
        google_error = str(request.query_params.get("error") or "").strip()
        if google_error:
            raise RuntimeError(f"Google authorization was not completed: {google_error}")
        result = _dual_auth.complete(
            state=str(request.query_params.get("state") or ""),
            query_string=str(request.url.query or ""),
            user=user,
        )
        label = str(result.get("channel_name") or result.get("channel_id") or "YouTube channel")
        suffix = " Web OAuth is active." if result.get("activated") else " Web OAuth is connected; activate it when ready."
        return redirect("/app?tab=authentication", message=label + suffix)
    except Exception as exc:
        return redirect("/app?tab=authentication", error=str(exc))


@app.post(route("/actions/auth-connections/test"))
async def dual_auth_test(request: Request):
    current_user(request)
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    try:
        result = _dual_auth.test_connection(
            int(str(form.get("channel_db_id") or "0")),
            str(form.get("method") or ""),
        )
        return redirect("/app?tab=authentication", message=f"{result.get('channel_name') or 'Channel'}: {result.get('method')} connection is working.")
    except Exception as exc:
        return redirect("/app?tab=authentication", error=str(exc))


@app.post(route("/actions/auth-connections/activate"))
async def dual_auth_activate(request: Request):
    current_user(request)
    form = await request.form()
    require_csrf(request, str(form.get("csrf_token") or ""))
    try:
        result = _dual_auth.select_active(
            int(str(form.get("channel_db_id") or "0")),
            str(form.get("method") or ""),
            verify=True,
        )
        return redirect("/app?tab=authentication", message=f"{result.get('channel_name') or 'Channel'} now uses {result.get('method')} authentication for new jobs.")
    except Exception as exc:
        return redirect("/app?tab=authentication", error=str(exc))


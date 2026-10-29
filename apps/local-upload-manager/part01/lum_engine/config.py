from __future__ import annotations
import os
from pathlib import Path

BUILD = "2026.08.21-local-upload-manager-standalone-v2.6-path-lum"
API_VERSION = 5

DATA_ROOT = Path(os.getenv("LUM_DATA_DIR", "/var/lib/local-upload-manager"))
CORE_DATA_ROOT = DATA_ROOT / "core"
PROJECT_DB = DATA_ROOT / "projects.sqlite3"
USERS_DB = DATA_ROOT / "users.sqlite3"
STAGING_ROOT = DATA_ROOT / "staging"
THUMBNAIL_ROOT = DATA_ROOT / "thumbnails"
REPORTS_ROOT = DATA_ROOT / "reports"
SECURE_ROOT = DATA_ROOT / "secure"
OAUTH_CLIENT_ROOT = SECURE_ROOT / "oauth_clients"
OAUTH_SESSION_ROOT = SECURE_ROOT / "oauth_sessions"
WEB_HOST = os.getenv("LUM_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("LUM_PORT", "8015"))
SESSION_SECRET = os.getenv("LUM_SESSION_SECRET", "")
COOKIE_SECURE = os.getenv("LUM_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}
SUITE_AUTH_ENABLED = os.getenv("LUM_SUITE_AUTH", "0").strip().lower() in {"1", "true", "yes", "on"}
SUITE_LOGIN_URL = os.getenv("LUM_SUITE_LOGIN_URL", "/login").strip() or "/login"

_raw_base_path = os.getenv("LUM_BASE_PATH", "/LUM").strip()
if _raw_base_path in {"", "/"}:
    BASE_PATH = ""
else:
    BASE_PATH = "/" + _raw_base_path.strip("/")

# Reused desktop core stores all mutable state below this permanent VPS path.
os.environ.setdefault("YOUTUBE_UPLOAD_MANAGER_DATA_DIR", str(CORE_DATA_ROOT))

DEFAULT_SETTINGS = {
    "engine_enabled": "0" if os.getenv("LUM_ENGINE_INSTANCE", "engine1") in {"engine2", "shorts", "song"} else "1",
    "pause_after_current": "0",
    "delete_staged_after_verified": "1",
    "remote_verification_enabled": "1",
    "source_mode": "drive_link",
    "max_active_uploads": "1",
    "sheets_sync_enabled": "0",
    "auto_engine_enabled": "0",
    "bulk_import_enabled": "0",
    "drive_delete_source_after_verified": "0",
}


def ensure_dirs() -> None:
    for folder in (
        DATA_ROOT, CORE_DATA_ROOT, STAGING_ROOT, THUMBNAIL_ROOT, REPORTS_ROOT,
        SECURE_ROOT, OAUTH_CLIENT_ROOT, OAUTH_SESSION_ROOT,
    ):
        folder.mkdir(parents=True, exist_ok=True)
    for folder in (DATA_ROOT, SECURE_ROOT, OAUTH_CLIENT_ROOT, OAUTH_SESSION_ROOT):
        try:
            folder.chmod(0o700)
        except OSError:
            pass

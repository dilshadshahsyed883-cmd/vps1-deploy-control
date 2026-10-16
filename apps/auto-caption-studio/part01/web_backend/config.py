from __future__ import annotations

import os
from pathlib import Path


def app_root() -> Path:
    return Path(
        os.environ.get(
            "AUTO_CAPTION_APP_ROOT",
            "/home/ubuntu/apps/auto_caption_studio",
        )
    ).expanduser()


def code_root() -> Path:
    configured = os.environ.get("AUTO_CAPTION_CODE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    installed = app_root() / "current"
    if installed.is_dir():
        return installed
    return Path(__file__).resolve().parents[1]


def database_path() -> Path:
    return Path(
        os.environ.get(
            "AUTO_CAPTION_DB_PATH",
            str(app_root() / "data/database/auto_caption.db"),
        )
    ).expanduser()


def projects_root() -> Path:
    return app_root() / "data/projects"


def uploads_root() -> Path:
    return app_root() / "data/uploads"


def outputs_root() -> Path:
    return app_root() / "data/outputs"


def previews_root() -> Path:
    return app_root() / "data/previews"


def logs_root() -> Path:
    return app_root() / "logs"


def web_root() -> Path:
    return code_root() / "web_frontend"


def lease_seconds() -> int:
    return max(15, int(os.environ.get("AUTO_CAPTION_LEASE_SECONDS", "90")))


def heartbeat_seconds() -> float:
    return max(1.0, float(os.environ.get("AUTO_CAPTION_HEARTBEAT_SECONDS", "10")))


def max_attempts() -> int:
    return max(1, int(os.environ.get("AUTO_CAPTION_MAX_ATTEMPTS", "3")))

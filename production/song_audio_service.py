"""Fail-closed Song Studio Audio Downloader service configuration and preflight.

This module prepares the independent 8152 process. It does not switch web
routes, start or stop workers, or grant authorization on its own.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess

from production import runtime

SONG_UNIT = "vps1-production-song-studio.service"
VOICE_UNIT = "vps1-production-voice-studio.service"
SCENE_UNIT = "vps1-scene-candidate.service"
SONG_PORT = 8152
SONG_ENV = Path("/etc/fresh-website/production-song-studio.env")
SCENE_ENV = Path("/etc/fresh-website/scene-builder.env")
VOICE_ENV = Path("/etc/fresh-website/production-voice-studio.env")


def canonical_root() -> Path:
    """Never use Song's empty default directory as replacement for the live DB."""
    db = runtime.voice_audio_db_from_live_config()
    root = db.parent.parent
    if root == Path("/") or not root.is_absolute() or ".." in root.parts:
        raise RuntimeError("Unsafe Song Audio canonical root")
    for node in (root, root.parent, root / "data", db):
        if node.is_symlink():
            raise RuntimeError("Linked Song Audio queue path is prohibited")
    if not root.is_dir() or not db.is_file():
        raise RuntimeError("Existing canonical Song Audio queue is missing")
    if root.resolve(strict=True) != root or db.resolve(strict=True) != db:
        raise RuntimeError("Song Audio canonical queue resolves outside real directories")
    if not (root.is_relative_to(Path("/var/lib/fresh-website")) or
            root == Path("/home/ubuntu/apps/audio_downloader_voice_studio")):
        raise RuntimeError("Song Audio root outside approved persistent paths")
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only=ON")
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if not {"status", "job_id", "owner_username", "song_maker_status"} <= cols:
            raise RuntimeError("Canonical Song Audio queue schema not ready")
    return root


def render_unit(revision: str, root: Path) -> str:
    """Dedicated single-worker unit, no Voice synthesis or public listener."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Exact protected-main release revision required")
    root = Path(root)
    if (not root.is_absolute() or ".." in root.parts or
            any(c.isspace() for c in str(root)) or root == Path("/")):
        raise ValueError("Unsafe systemd write path")
    interpreter = runtime.BASE / "venvs/song-studio" / revision / "bin/python"
    return f"""[Unit]
Description=VPS1 Song Studio Audio Downloader (independent)
After=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/srv/fresh-website/current/song-studio/part01
EnvironmentFile=/etc/fresh-website/production-song-studio.env
Environment=PATH=/srv/fresh-website/venvs/song-studio/{revision}/bin:/usr/local/bin:/usr/bin:/bin
ExecStart={interpreter} -m uvicorn song_audio_app:app --host 127.0.0.1 --port 8152 --workers 1
Restart=on-failure
RestartSec=3
TimeoutStopSec=35
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={root} -/home/ubuntu/.config/masterctl
RestrictSUIDSGID=yes

[Install]
WantedBy=multi-user.target
"""


def render_env(root: Path) -> str:
    root = Path(root)
    if root == Path("/") or not root.is_absolute() or ".." in root.parts or any(c.isspace() for c in str(root)):
        raise ValueError("Unsafe Song Audio environment path")
    # Secrets remain in existing root-protected host files, never GitHub.
    return ("PYTHONDONTWRITEBYTECODE=1\n"
            "AUDIO_DOWNLOADER_ROOT=" + str(root) + "\n"
            "SONG_AUDIO_CUTOVER_ACTIVE=1\n"
            "RCLONE_CONFIG=/home/ubuntu/.config/masterctl/rclone.conf\n")


def preflight() -> dict:
    """Read-only: require real worker/queue identity, idle state and unused port."""
    root = canonical_root()
    if not VOICE_ENV.is_file() or VOICE_ENV.is_symlink():
        raise RuntimeError("Original Voice configuration is unavailable")
    if not SCENE_ENV.is_file() or SCENE_ENV.is_symlink():
        raise RuntimeError("Scene candidate configuration is unavailable")
    for service in (VOICE_UNIT, SCENE_UNIT):
        if subprocess.run(["systemctl", "is-active", "--quiet", service],
                          check=False, capture_output=True).returncode:
            raise RuntimeError(service + " inactive; cutover blocked")
    # Preserve every existing release/tenant safety gate. Pending Song Maker
    # handoffs may be allowed to complete NATURALLY on the existing worker;
    # no rows or statuses are ever modified by the release helper.
    from production.song_audio_handoff_preflight import require_drain
    require_drain(runtime.DATA / "voice-studio", root / "data/jobs.sqlite3")
    runtime.free_port(SONG_PORT)
    return {"audio_db": str(root / "data/jobs.sqlite3"),
            "port": SONG_PORT,
            "song_service": SONG_UNIT,
            "legacy_worker": VOICE_UNIT,
            "scene_service": SCENE_UNIT,
            "route_cutover_performed": False}

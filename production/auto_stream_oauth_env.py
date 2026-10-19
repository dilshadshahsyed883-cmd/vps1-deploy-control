"""Stable, isolated Auto Stream OAuth vault encryption key provisioning.

Called only by the app-scoped root installer. Never rotates keys or copies
Auto Uploader credentials. No secret value is printed or returned.
"""
from __future__ import annotations
import base64
import os
from pathlib import Path
import secrets
import sqlite3
import stat

KEY = "AUTO_STREAM_FERNET_KEY"

def valid_key(value: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
        return len(decoded) == 32 and base64.urlsafe_b64encode(decoded).decode() == value
    except (ValueError, UnicodeError, base64.binascii.Error):
        return False

def encrypted_records_present(state: Path) -> bool:
    db = Path(state) / "auto-stream-engine" / "integrations.sqlite3"
    if not db.exists():
        return False
    if db.is_symlink() or not db.is_file():
        raise RuntimeError("Untrusted Auto Stream integration database")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        tables = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for name, field in (("channels", "token"), ("live_control", "secret")):
            if name in tables and connection.execute(
                    f"SELECT 1 FROM {name} WHERE {field} IS NOT NULL LIMIT 1").fetchone():
                return True
    return False

def ensure_key(env_path: Path, state: Path) -> str:
    """Returns NEW or PRESERVED only, never the Fernet key."""
    if Path(env_path).is_symlink():
        raise RuntimeError("Symlink Auto Stream env file refused")
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(env_path, flags), "r+", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or (info.st_mode & 0o077)):
            raise RuntimeError("Unsafe Auto Stream environment file permissions")
        values = [line.partition("=")[2].strip().strip('"')
                  for line in stream.read().splitlines() if line.startswith(KEY + "=")]
        if values:
            if len(values) != 1 or not valid_key(values[0]):
                raise RuntimeError("Existing Auto Stream vault key invalid; refusing rotation")
            return "PRESERVED"
        if encrypted_records_present(Path(state)):
            raise RuntimeError("Encrypted Auto Stream records exist without a vault key")
        stream.write("\n" + KEY + "=" +
                     base64.urlsafe_b64encode(secrets.token_bytes(32)).decode() + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return "NEW"

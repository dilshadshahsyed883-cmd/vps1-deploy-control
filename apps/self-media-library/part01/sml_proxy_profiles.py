"""Independent SML proxy profiles, modeled after Video Merger's proxy tab.

Credentials remain in SML_DATA_ROOT/proxy_profiles.json (0600); this module
never changes Video Merger or Audio Downloader configuration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import uuid
from urllib.parse import quote
from urllib.request import Request, ProxyHandler, build_opener

_LOCK = threading.RLock()


def config_path() -> Path:
    return Path(os.environ.get("SML_DATA_ROOT", "./data")).expanduser() / "proxy_profiles.json"


def _empty():
    return {"enabled": False, "active_id": "", "profiles": []}


def _load():
    path = config_path()
    if not path.exists():
        return _empty()
    if path.is_symlink():
        raise ValueError("SML proxy configuration cannot be a symlink")
    st = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_size > 65536:
        raise ValueError("SML proxy configuration file must be private (0600)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("SML proxy configuration is invalid") from None
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        raise ValueError("SML proxy configuration is invalid")
    return data


def _write(data):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("SML proxy configuration cannot be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".sml-proxy-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def profile_url(profile):
    return "http://{}:{}@{}:{}".format(
        quote(str(profile["username"]), safe=""),
        quote(str(profile["password"]), safe=""),
        profile["host"], int(profile["port"]),
    )


def state():
    with _LOCK:
        data = _load()
        selected = data.get("active_id", "")
        return {
            "enabled": data.get("enabled") is True,
            "active_id": selected,
            "profiles": [
                {"id": p["id"], "name": p["name"], "host": p["host"],
                 "port": p["port"], "username": p["username"],
                 "has_password": bool(p.get("password")),
                 "active": p["id"] == selected}
                for p in data["profiles"]
            ],
        }


def active_url():
    with _LOCK:
        data = _load()
        if data.get("enabled") is not True:
            return ""
        item = next((p for p in data["profiles"]
                     if p["id"] == data.get("active_id")), None)
        if not item:
            return ""
        return profile_url(item)


def _fields(payload, previous=None):
    name = str(payload.get("name") or "").strip()[:80]
    host = str(payload.get("host") or "").strip()[:255]
    user = str(payload.get("username") or "").strip()[:255]
    password = str(payload.get("password") or "")
    if not password and previous:
        password = previous["password"]
    try:
        port = int(payload.get("port") or 0)
    except (ValueError, TypeError):
        raise ValueError("Proxy port must be a number") from None
    if not name or not host or not user or not password:
        raise ValueError("Name, host, username and password are required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) or not 1 <= port <= 65535:
        raise ValueError("Enter a valid proxy host and port (1–65535)")
    if len(password) > 1024:
        raise ValueError("Proxy password too long")
    return {"name": name, "host": host, "port": port,
            "username": user, "password": password}


def save(payload):
    with _LOCK:
        data = _load()
        pid = str(payload.get("id") or "").strip()
        existing = next((p for p in data["profiles"] if p["id"] == pid), None)
        if pid and not existing:
            raise ValueError("Proxy profile not found")
        fields = _fields(payload, existing)
        if any(p["name"].casefold() == fields["name"].casefold() and
               p["id"] != pid for p in data["profiles"]):
            raise ValueError("Proxy profile name already exists")
        if existing:
            existing.update(fields)
        else:
            if len(data["profiles"]) >= 30:
                raise ValueError("Maximum 30 proxy profiles")
            pid = uuid.uuid4().hex[:12]
            data["profiles"].append({"id": pid, **fields})
            if not data.get("active_id"):
                data["active_id"] = pid
        _write(data)
        return pid


def set_active(pid):
    with _LOCK:
        data = _load()
        if not any(p["id"] == pid for p in data["profiles"]):
            raise ValueError("Proxy profile not found")
        data["active_id"] = pid
        _write(data)


def toggle(enabled):
    with _LOCK:
        data = _load()
        if enabled and not any(p["id"] == data.get("active_id")
                               for p in data["profiles"]):
            raise ValueError("Add and activate a proxy profile first")
        data["enabled"] = bool(enabled)
        _write(data)


def delete(pid):
    with _LOCK:
        data = _load()
        old_len = len(data["profiles"])
        data["profiles"] = [p for p in data["profiles"] if p["id"] != pid]
        if len(data["profiles"]) == old_len:
            raise ValueError("Proxy profile not found")
        if pid == data.get("active_id"):
            data["active_id"] = data["profiles"][0]["id"] if data["profiles"] else ""
            data["enabled"] = False
        if not data["profiles"]:
            data["enabled"] = False
        _write(data)


def test(pid):
    """Connectivity test only. An authenticated YouTube video is NOT verified."""
    with _LOCK:
        data = _load()
        profile = next((p.copy() for p in data["profiles"] if p["id"] == pid), None)
    if not profile:
        raise ValueError("Proxy profile not found")
    url = profile_url(profile)
    opener = build_opener(ProxyHandler({"http": url, "https": url}))
    try:
        with opener.open(Request("https://www.youtube.com/generate_204",
                                 headers={"User-Agent": "SML-Proxy-Test/1.0"}), timeout=18) as response:
            status = response.status
        if not 200 <= status < 400:
            raise OSError("proxy handshake failed")
    except Exception:
        # Never return provider/proxy raw errors: they can include credentials.
        raise ValueError("Proxy connectivity test failed; check host, port and credentials") from None
    return {"ok": True, "connectivity": "PASS",
            "youtube_video_access": "NOT_VERIFIED"}

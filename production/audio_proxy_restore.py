"""Audio-only legacy proxy restoration; never logs credentials or owner settings."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import stat
import urllib.error
import urllib.parse
import urllib.request


class ProxyRestoreBlocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise ProxyRestoreBlocked("AUDIO_PROXY_RESTORE_BLOCKED: " + reason)


def plan(root):
    path = Path(root) / "data/proxy_settings.json"
    require(path.is_file() and not path.is_symlink(), "NO_SAVED_PROXY_CONFIG")
    st = path.stat(follow_symlinks=False)
    require(stat.S_ISREG(st.st_mode) and st.st_size <= 65536, "UNTRUSTED_PROXY_CONFIG")
    require(st.st_mode & 0o077 == 0, "INSECURE_PROXY_CONFIG_PERMISSIONS")
    before = path.read_bytes()
    try:
        cfg = json.loads(before)
    except (ValueError, UnicodeDecodeError):
        raise ProxyRestoreBlocked("AUDIO_PROXY_RESTORE_BLOCKED: INVALID_PROXY_CONFIG") from None
    require(isinstance(cfg, dict), "INVALID_PROXY_CONFIG")
    if cfg.get("enabled") is True:
        return None
    require(cfg.get("enabled") is False, "AMBIGUOUS_PROXY_TOGGLE")
    profiles = cfg.get("profiles")
    require(isinstance(profiles, list), "INVALID_PROFILE_SET")
    chosen = next((p for p in profiles if isinstance(p, dict) and p.get("id") == cfg.get("active_id")), None)
    require(chosen is not None, "NO_SELECTED_PROXY_PROFILE")
    host, port = chosen.get("host"), chosen.get("port")
    require(isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*", host)
            and isinstance(port, int) and 1 <= port <= 65535, "INVALID_SELECTED_PROXY")
    user = str(chosen.get("username") or "")
    secret = str(chosen.get("password") or "")
    credentials = (urllib.parse.quote(user, safe="") + ":" +
                   urllib.parse.quote(secret, safe="") + "@") if user else ""
    proxy = "http://" + credentials + host + ":" + str(port)
    # Real proxy routing check; actual media + Drive are independently checked by
    # the deployed service-user disposable worker smoke after transactional install.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        req = urllib.request.Request("https://www.youtube.com/robots.txt",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=20) as response:
            require(response.status == 200, "YOUTUBE_PROXY_CONNECTIVITY_NOT_200")
            response.read(16)
    except urllib.error.HTTPError as exc:
        raise ProxyRestoreBlocked("AUDIO_PROXY_RESTORE_BLOCKED: " +
                                  ("PROXY_AUTH_407" if exc.code == 407 else
                                   "YOUTUBE_CONNECTIVITY_HTTP_" + str(exc.code))) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ProxyRestoreBlocked("AUDIO_PROXY_RESTORE_BLOCKED: PROXY_CONNECTION_FAILED") from None
    cfg["enabled"] = True
    after = (json.dumps(cfg, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    return {"path": path, "before": before, "after": after, "uid": st.st_uid, "gid": st.st_gid}


def replace_private(item, data):
    """Write atomically with service owner and 0600 (root installer must not take ownership)."""
    path = item["path"]
    tmp = path.with_name("." + path.name + ".restore-" + str(os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchown(fd, item["uid"], item["gid"])
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()

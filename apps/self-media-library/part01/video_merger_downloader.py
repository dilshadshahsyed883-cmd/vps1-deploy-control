"""SML video-only YouTube intake, adapted from Video Merger's 2026-10-21 downloader.

This is an independent copy of the download strategy, NOT an import of Video
Merger's app/process/queue. Only the resulting verified video goes to SML's
existing source registration and Process pipeline. Shared proxy state is
strictly read-only and only used if the user explicitly selected proxy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote

from media_catalog import probe_video
from sml_proxy_profiles import active_url as sml_active_proxy_url

# Video Merger's actual production per-app state path (production/runtime.py).
VIDEO_PROXY_FILE = Path(
    "/var/lib/fresh-website/video-merger/video-merger/config/proxy_profiles.json"
)
# Video Merger already uses Audio's verified route read-only as its fallback.
AUDIO_PROXY_FILE = Path(
    "/var/lib/fresh-website/voice-studio/audio-downloader/data/proxy_settings.json"
)
MAX_BYTES = 1200 * 1024 * 1024
PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
    "https_proxy", "all_proxy", "NO_PROXY", "no_proxy",
)
_CLIENTS = ("default", "tv,web_safari")


def _read_active_profile(path: Path):
    """Never follow symlinks, change proxy ownership, or expose credentials."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return None
        if info.st_mode & 0o077 or not 0 < info.st_size <= 65536:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("enabled") is not True:
            return None
        profiles = payload.get("profiles", [])
        if not isinstance(profiles, list):
            return None
        selected = next(
            (p for p in profiles if isinstance(p, dict) and p.get("id") == payload.get("active_id")),
            None,
        )
        if not selected:
            return None
        host = str(selected.get("host") or "")
        port = int(selected.get("port") or 0)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) or not 1 <= port <= 65535:
            return None
        if not selected.get("username") or not selected.get("password"):
            return None
        return selected
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def _profile_url(profile):
    return "http://{}:{}@{}:{}".format(
        quote(str(profile["username"]), safe=""),
        quote(str(profile["password"]), safe=""),
        profile["host"], int(profile["port"]),
    )


def _proxy_routes(use_proxy: bool):
    if not use_proxy:
        return [None]  # Never silently use a saved proxy for a direct job.
    # The SML-managed active profile wins; existing VM/Audio routes remain
    # read-only fallbacks, never mutated by this app.
    local_url = sml_active_proxy_url()
    video_url = os.getenv("SML_YTDLP_PROXY_URL", "").strip()
    vm = _read_active_profile(VIDEO_PROXY_FILE)
    audio = _read_active_profile(AUDIO_PROXY_FILE)
    first = local_url or video_url or (_profile_url(vm) if vm else "")
    second = _profile_url(audio) if audio else ""
    routes = [first] if first else []
    if second and second not in routes:
        routes.append(second)
    if not routes:
        raise RuntimeError(
            "Proxy profile is not configured. Configure the Video Merger/Audio "
            "Downloader proxy or use Retry direct."
        )
    return routes


def proxy_available() -> bool:
    try:
        return bool(_proxy_routes(True))
    except RuntimeError:
        return False


def _cookie_file():
    # Optional SML-owned, owner-authorized Netscape session. Never read browser
    # databases, private profiles of another app or cookie files by default.
    raw = os.getenv("SML_YOUTUBE_COOKIES_FILE", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeError("YOUTUBE_SESSION_CONFIGURATION: SML session file unavailable")
    info = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_size > 1024 * 1024):
        raise RuntimeError("YOUTUBE_SESSION_CONFIGURATION: Private SML session file required")
    content = path.read_bytes()
    if not content.startswith((b"# Netscape HTTP Cookie File", b"# HTTP Cookie File")):
        raise RuntimeError("YOUTUBE_SESSION_CONFIGURATION: Netscape cookies required")
    valid = False
    for line in content.decode("utf-8").splitlines():
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.removeprefix("#HttpOnly_").split("\t")
        if len(parts) != 7:
            raise RuntimeError("YOUTUBE_SESSION_CONFIGURATION: Invalid session cookie format")
        domain = parts[0].lstrip(".").lower()
        if domain in ("youtube.com", "google.com") or domain.endswith((".youtube.com", ".google.com")):
            expires = int(parts[4])
            valid |= expires == 0 or expires > time.time()
    if not valid:
        raise RuntimeError("YOUTUBE_SESSION_CONFIGURATION: No unexpired YouTube session")
    return content


def _js_runtime():
    for name in ("deno", "node"):
        executable = shutil.which(name)
        if executable:
            return name + ":" + executable
    # Video Merger's immutable active Deno may be reused read-only without
    # changing the other application's environment or deployment.
    venvs = Path("/srv/fresh-website/venvs/video-merger")
    if venvs.is_dir():
        for candidate in sorted(venvs.glob("*/bin/deno"), reverse=True):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return "deno:" + str(candidate)
    return None


def _redact(message: str, routes: list[str]) -> str:
    result = str(message or "")
    for route in routes:
        if route:
            result = result.replace(route, "[private proxy]")
    cookie_path = os.getenv("SML_YOUTUBE_COOKIES_FILE", "")
    if cookie_path:
        result = result.replace(cookie_path, "[private session]")
    result = re.sub(r"https?://[^\s@]+@", "http://***@", result)
    return result[-450:]


def _failure(message):
    low = message.lower().replace("’", "'")
    if any(s in low for s in ("not a bot", "sign in", "cookies are no longer valid", "confirm your age")):
        return RuntimeError(
            "YOUTUBE_AUTH_REQUIRED: YouTube requested a verified session. "
            "Video Merger's alternate playback clients were tried; an authorized "
            "SML session or later manual retry may be required."
        )
    if "po token" in low or "proof of origin" in low:
        return RuntimeError("YOUTUBE_PROVIDER_CHALLENGE: YouTube requires additional verification.")
    if "429" in low or "too many requests" in low:
        return RuntimeError("YOUTUBE_RATE_LIMIT: YouTube rate limited this session/IP; retry later.")
    if "407" in low or "proxy authentication" in low:
        return RuntimeError("PROXY_AUTH_FAILED: Selected proxy rejected authentication.")
    if "403" in low:
        return RuntimeError("YOUTUBE_ACCESS_DENIED: Source access was rejected by YouTube.")
    if any(s in low for s in ("tunnel connection failed: 503", "unable to connect to proxy", "proxyerror")):
        return RuntimeError("PROXY_CONNECTION_FAILED: Selected proxy tunnel unavailable.")
    return RuntimeError("YOUTUBE_DOWNLOAD_FAILED: " + message[-300:])


def _run_attempt(url, directory, route, client, session, on_download_percent, timeout):
    """Video Merger's bounded yt-dlp client attempt and private config pattern."""
    directory.mkdir(mode=0o700)
    config = directory / "yt-dlp.conf"
    options = [
        "--no-playlist", "--no-cache-dir", "--socket-timeout 30",
        "--retries 1", "--fragment-retries 1", "--extractor-retries 0",
        "--proxy " + shlex.quote(route or ""),
    ]
    runtime = _js_runtime()
    if runtime:
        options.append("--js-runtimes " + shlex.quote(runtime))
    if session:
        cookies = directory / "session.txt"
        cookies.write_bytes(session)
        cookies.chmod(0o600)
        options.append("--cookies " + shlex.quote(str(cookies)))
    config.write_text("\n".join(options) + "\n", encoding="utf-8")
    config.chmod(0o600)
    command = [
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--config-locations", str(config),
        "--newline", "--no-warnings", "--progress-template",
        "download:%(progress._percent_str)s", "--max-filesize", "1200M",
        # --no-playlist limits intake to one YouTube video. Do not use
        # --max-downloads=1: yt-dlp counts successful media downloads when
        # merging separate video+audio streams and can exit non-zero after
        # the finished output, making SML incorrectly mark the job failed.
        "--merge-output-format", "mp4",
        "-f", "bv*+ba/best/bv*", "-o", str(directory / "video.%(ext)s"),
    ]
    if client != "default":
        command += ["--extractor-args", "youtube:player_client=" + client]
    command += [url]
    environment = os.environ.copy()
    for key in PROXY_ENV_KEYS:
        environment.pop(key, None)
    # Proxy secrets never enter the command-line argument list.
    if route:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            environment[key] = route
    errors = []
    expired = threading.Event()
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace", bufsize=1, env=environment) as proc:
        def kill_expired():
            expired.set()
            proc.kill()
        timer = threading.Timer(timeout, kill_expired)
        timer.daemon = True
        timer.start()
        try:
            for line in proc.stdout:
                match = re.search(r"download:\s*(\d{1,3}(?:\.\d+)?)\s*%", line)
                if match:
                    on_download_percent(min(100, int(float(match.group(1)))))
                elif line.strip():
                    errors.append(line.strip())
                    if len(errors) > 16:
                        errors.pop(0)
            code = proc.wait()
        finally:
            timer.cancel()
    if expired.is_set():
        raise RuntimeError("YouTube download timed out")
    if code:
        raise RuntimeError(" ".join(errors)[-1200:] or "yt-dlp exited " + str(code))
    files = sorted(
        (f for f in directory.glob("video.*")
         if f.is_file() and f.suffix.lower() not in (".part", ".ytdl", ".json", ".jpg", ".webp")),
        key=lambda f: f.stat().st_size, reverse=True,
    )
    if len(files) != 1:
        raise RuntimeError("YouTube download did not create exactly one video file")
    file = files[0]
    if not (0 < file.stat().st_size <= MAX_BYTES):
        raise RuntimeError("Downloaded video size outside SML limit")
    info = probe_video(file)
    if info["width"] <= 0 or info["height"] <= 0 or not info["codec"]:
        raise RuntimeError("Video Merger verification rejected audio-only or invalid video")
    return file


def download_youtube_video(url: str, workdir: Path, progress, *, use_proxy: bool,
                           timeout: int = 900):
    """Return verified VIDEO bytes; caller handles all SML persistence and Process.

    On a challenge, mirror Video Merger: at most two playback clients per route;
    retry the alternate read-only Audio route only for selected-proxy jobs.
    """
    routes = _proxy_routes(use_proxy)
    session = _cookie_file()
    attempt = 0
    last_error = ""
    for route_index, route in enumerate(routes):
        for client in _CLIENTS:
            attempt += 1
            progress(5, "Video Merger downloader: connecting to YouTube")
            directory = workdir / ("attempt-" + str(attempt))
            try:
                # Genuine yt-dlp download % is mapped into SML's existing 10-90 range.
                return _run_attempt(
                    url, directory, route, client, session,
                    lambda pct: progress(10 + int(pct * .80),
                                         "Downloading video: " + str(pct) + "%"),
                    timeout,
                )
            except Exception as exc:
                clean = _redact(str(exc), routes)
                last_error = clean
                low = clean.lower().replace("’", "'")
                fatal = any(s in low for s in (
                    "429", "too many requests", "407", "proxy authentication",
                    "403", "ytdlp_missing",
                ))
                proxy_down = any(s in low for s in (
                    "tunnel connection failed: 503", "unable to connect to proxy",
                    "proxyerror",
                ))
                # Clean partial files/cookies between attempts, not SML source assets.
                shutil.rmtree(directory, ignore_errors=True)
                if proxy_down and route_index + 1 < len(routes):
                    break
                recoverable = any(s in low for s in (
                    "not a bot", "sign in", "video unavailable",
                    "this video is unavailable",
                ))
                if fatal or not recoverable:
                    raise _failure(clean) from None
    raise _failure(last_error)

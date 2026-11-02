"""Atomic Voice-to-Song Audio worker handoff on VPS_1.

Invoked ONLY through protected-main vps1-production-deploy install <sha> voice-studio
with [vps1-song-audio-separate] intent. Owns one serialized transaction.
Never copies/moves the live media directory or edits its source SQLite database.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from production import runtime
from production import song_audio_service as service
from production import scene_candidate_release as scene

BASE = runtime.BASE
REPO = runtime.REPO
VOICE_CURRENT = BASE / "current/voice-studio"
SONG_CURRENT = BASE / "current/song-studio"
SCENE_CURRENT = BASE / "current/scene-builder"
VOICE_RELEASES = BASE / "releases/voice-studio"
SONG_RELEASES = BASE / "releases/song-studio"
SCENE_RELEASES = BASE / "releases/scene-builder"
VOICE_ENV = service.VOICE_ENV
SCENE_ENV = service.SCENE_ENV
SONG_ENV = service.SONG_ENV
SONG_UNIT_PATH = Path("/etc/systemd/system") / service.SONG_UNIT
JOURNALS = BASE / "transactions/song-audio-separation"
VOICE_FILES = ("part01/vps1_omnivoice_app.py", "part01/audio_downloader_api.py")


def _verify_pointer(pointer: Path, releases: Path) -> Path:
    if not pointer.is_symlink():
        raise RuntimeError("Existing service release pointer unavailable")
    resolved = pointer.resolve(strict=True)
    if not resolved.is_relative_to(releases):
        raise RuntimeError("Existing service release outside managed revisions")
    return resolved


def _patch_env(payload: bytes, changes: dict[str, str]) -> bytes:
    source = payload.decode("utf-8")
    rows = source.splitlines()
    for key, value in changes.items():
        if not re.fullmatch("[A-Z][A-Z0-9_]*", key) or any(
                char in value for char in ("\n", "\r", "\x00")):
            raise ValueError("Unsafe service environment setting")
        indices = [i for i, line in enumerate(rows) if line.startswith(key + "=")]
        if len(indices) > 1:
            raise RuntimeError("Ambiguous protected service configuration")
        if indices:
            rows[indices[0]] = key + "=" + value
        else:
            rows.append(key + "=" + value)
    return ("\n".join(rows) + "\n").encode()


def _atomic_env(path: Path, content: bytes) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Protected service environment unavailable")
    meta = path.stat()
    if not stat.S_ISREG(meta.st_mode) or len(content) > 65536:
        raise RuntimeError("Protected service environment invalid")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".song-audio-env-",
                                         delete=False) as fp:
            temp = Path(fp.name)
            os.fchmod(fp.fileno(), stat.S_IMODE(meta.st_mode))
            os.fchown(fp.fileno(), meta.st_uid, meta.st_gid)
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _clone_overlay(previous: Path, target: Path, changes: dict[str, bytes],
                   manifest_name: str, app_name: str, revision: str) -> None:
    if target.exists() or target.is_symlink():
        raise RuntimeError("Immutable target already exists; refusing overwrite")
    # Require every edited file to be owned by the currently active release.
    for name in changes:
        old = previous / name
        if old.is_symlink() or not old.is_file():
            raise RuntimeError("Previous release overlay target missing/linked")
    manifest_path = previous / manifest_name
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("Previous release manifest missing/linked")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("app") != app_name:
        raise RuntimeError("Previous release manifest ownership mismatch")
    for name in changes:
        if name not in manifest.get("files", {}):
            raise RuntimeError("Overlay would edit untracked source")
    shutil.copytree(previous, target, symlinks=True)
    for name, data in changes.items():
        (target / name).write_bytes(data)
        manifest["files"][name] = hashlib.sha256(data).hexdigest()
    manifest["revision"] = revision
    manifest["song_audio_separation_overlay"] = str(previous)
    runtime.atomic_json(target / manifest_name, manifest, 0o644)


def _patch_scene_source(live: bytes, reviewed: bytes) -> bytes:
    """Minimal exact allowlisted change. Never replace unrelated Scene features."""
    src = live.decode("utf-8")
    committed = reviewed.decode("utf-8")
    anchor = '_VS_UPSTREAM="http://127.0.0.1:8143"'
    head = anchor + "\n"
    next_route = '\n@app.get("/voice-studio", response_class=HTMLResponse)'
    if src.count(head) != 1 or committed.count(head) != 1:
        raise RuntimeError("Scene Voice/Song routing anchor drift")
    start = committed.index(head) + len(head)
    end = committed.index(next_route, start)
    dedicated = committed[start:end]
    if "_song_audio_upstream" not in dedicated:
        raise RuntimeError("Reviewed dedicated Song proxy implementation absent")
    if "_song_audio_upstream" not in src:
        src = src.replace(head, head + dedicated, 1)
    elif dedicated not in src:
        raise RuntimeError("Scene dedicated Song backend differs from reviewed source")
    replacements = (
        ('_vs_requests.get, "http://127.0.0.1:8143/audio-downloader/app"',
         '_vs_requests.get, _song_audio_upstream() + "/audio-downloader/app"'),
        ('url=_VS_UPSTREAM+"/"+upstream_path.lstrip("/")',
         'url=(_song_audio_upstream() if studio == "song" else _VS_UPSTREAM)+"/"+upstream_path.lstrip("/")'),
    )
    for old, new in replacements:
        if src.count(old) == 1:
            src = src.replace(old, new, 1)
        elif src.count(new) != 1:
            raise RuntimeError("Scene proxy exact route drift: refuse bulk overwrite")
    # No unreviewed URL, path or app worker mutation is permitted.
    compile(src, str(SCENE_CURRENT / "part01/app.py"), "exec")
    return src.encode("utf-8")


def _stage_song(revision: str) -> Path:
    from rebuild.fresh_vps import sources
    release = SONG_RELEASES / revision
    if release.exists() or release.is_symlink():
        raise RuntimeError("Song Audio immutable release already exists")
    files = sources("song-studio")
    required = {"part01/song_audio_app.py", "part01/song_audio_downloader_api.py",
                "part01/song_tenant.py", "part01/templates/voice_studio.html"}
    if not required <= {name for name, _ in files}:
        raise RuntimeError("Song Audio source manifest incomplete")
    release.mkdir(parents=True, mode=0o755)
    for name, content in files:
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o644)
    manifest = {"app": "song-studio", "revision": revision,
                "files": {name: hashlib.sha256(content).hexdigest()
                          for name, content in files}}
    runtime.atomic_json(release / "_production_manifest.json", manifest, 0o644)
    return release


def _prepare_runtime(revision: str) -> None:
    venv = BASE / "venvs/song-studio" / revision
    if (venv / "bin/python").is_file():
        raise RuntimeError("Song Audio venv unexpectedly pre-exists")
    venv.parent.mkdir(parents=True, exist_ok=True)
    venv.mkdir(mode=0o755)
    os.chown(venv, 1000, 1000)
    runtime.run(["runuser", "-u", "ubuntu", "--", "python3", "-m", "venv", venv], quiet=True)
    deps = runtime.SONG_AUDIO_SPEC["packages"]
    runtime.run(["runuser", "-u", "ubuntu", "--", venv / "bin/python", "-m", "pip",
                 "install", "--disable-pip-version-check", "--no-cache-dir", *deps],
                timeout=900, quiet=True)
    # Real app boot with disposable data and a fake Scene SSO only in the
    # subprocess. No production root, queue or download worker is touched.
    runtime.run(["runuser", "-u", "ubuntu", "--", venv / "bin/python", "-m",
                 "production.smoke", "--app", "song-studio"], cwd=REPO,
                timeout=180, quiet=True)


def _health_song() -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8152/healthz", timeout=6) as response:
        payload = json.loads(response.read(4096))
        if (response.status != 200 or payload.get("ok") is not True or
                payload.get("app") != "song-audio-downloader" or
                payload.get("worker_active") is not True or payload.get("smoke") is not False):
            raise RuntimeError("Independent Song worker did not become healthy")
    # Fail closed if Song exposes Voice-only synthesis APIs.
    try:
        opener.open("http://127.0.0.1:8152/tts/api/clone/jobs", timeout=3)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise RuntimeError("Song Audio anonymous SSO boundary invalid") from exc
    else:
        raise RuntimeError("Song Audio served protected Voice route")
    return payload


def _health_scene() -> None:
    scene._health()
    runtime.http_check("voice-studio")
    _health_song()


def _live_http_acceptance() -> dict:
    # Verify a real Scene-backed authenticated page plus the owner-scoped API,
    # never a forged X-Auth-User claim or a synthetic production cookie.
    from production.route_uploader_scene_https import scene_cookie, https_get
    cookie = scene_cookie()
    status, html = https_get("/audio-downloader/", cookie=cookie)
    if status != 200 or b"Audio Downloader" not in html:
        raise RuntimeError("Authenticated canonical Song Audio page not live")
    anonymous, _ = https_get("/audio-downloader/")
    if anonymous not in (401, 403, 303):
        raise RuntimeError("Anonymous Song Audio route was not denied")
    return {"canonical_page": "HTTP_200_SCENE_SSO",
            "anonymous_access": "DENIED", "protected_user_isolation": "CI_ENFORCED"}


def install(revision: str) -> None:
    runtime.check_host(revision)
    # Full-history security CI and protected main are enforced in GitHub, not
    # bypassed here. Live operator invokes existing serialized root helper.
    if "[vps1-song-audio-separate]" not in runtime.run(
            ["git", "log", "-1", "--format=%B"], cwd=REPO, quiet=True):
        raise RuntimeError("Protected main Song Audio release intent not present")
    state = service.preflight()
    root = Path(state["audio_db"]).parent.parent
    previous_voice = _verify_pointer(VOICE_CURRENT, VOICE_RELEASES)
    previous_scene = _verify_pointer(SCENE_CURRENT, SCENE_RELEASES)
    if SONG_UNIT_PATH.exists() or SONG_UNIT_PATH.is_symlink() or SONG_ENV.exists() or SONG_ENV.is_symlink():
        raise RuntimeError("Independent Song service already provisioned; do not overwrite")
    if SONG_CURRENT.exists() or SONG_CURRENT.is_symlink():
        raise RuntimeError("Song Audio pointer unexpectedly exists")
    voice_candidate = VOICE_RELEASES / (revision + "-song-audio")
    scene_candidate = SCENE_RELEASES / (revision + "-song-audio")
    voice_data = {}
    for item in VOICE_FILES:
        reviewed = REPO / "apps/voice-studio" / item
        if reviewed.is_symlink() or not reviewed.is_file():
            raise RuntimeError("Reviewed Voice split source absent/linked")
        voice_data[item] = reviewed.read_bytes()
    reviewed_scene = REPO / "apps/scene-builder/part01/app.py"
    if reviewed_scene.is_symlink() or not reviewed_scene.is_file():
        raise RuntimeError("Reviewed Scene routing source absent/linked")
    patched_scene = _patch_scene_source(
        (previous_scene / "part01/app.py").read_bytes(), reviewed_scene.read_bytes())
    _clone_overlay(previous_voice, voice_candidate, voice_data,
                   "_production_manifest.json", "voice-studio", revision)
    _clone_overlay(previous_scene, scene_candidate,
                   {"part01/app.py": patched_scene},
                   "_scene_candidate_manifest.json", "scene-builder", revision)
    _stage_song(revision)
    _prepare_runtime(revision)
    # Re-check every owner and the running service immediately before drain.
    service.preflight()
    journal = JOURNALS / (revision + "-" + str(time.time_ns()))
    journal.mkdir(parents=True, mode=0o700)
    journal.chmod(0o700)
    voice_env_original = VOICE_ENV.read_bytes()
    scene_env_original = SCENE_ENV.read_bytes()
    if not voice_env_original or not scene_env_original:
        raise RuntimeError("Protected legacy environment empty")
    # Durable rollback inputs, root-owned only, never included in Git artifacts.
    for name, content in (("voice.env", voice_env_original), ("scene.env", scene_env_original)):
        path = journal / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    sqlite_backup = journal / "audio-queue-before.sqlite3"
    sqlite_src = root / "data/jobs.sqlite3"
    with sqlite3.connect(sqlite_src) as src, sqlite3.connect(sqlite_backup) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Song Audio SQLite backup integrity failure")
    sqlite_backup.chmod(0o600)
    runtime.atomic_json(journal / "previous.json", {
        "voice": str(previous_voice), "scene": str(previous_scene),
        "song": None, "queue": str(sqlite_src), "status": "PREPARED"})
    changed_voice = False
    changed_song = False
    changed_scene = False
    try:
        # The legacy Voice process must release its worker BEFORE Song starts.
        runtime.require_voice_release_idle(runtime.DATA / "voice-studio", audio_db=sqlite_src)
        runtime.run(["systemctl", "stop", service.VOICE_UNIT], timeout=60, quiet=True)
        changed_voice = True
        runtime.atomic_link(VOICE_CURRENT, voice_candidate)
        _atomic_env(VOICE_ENV, _patch_env(voice_env_original,
                                        {"SONG_AUDIO_BACKEND_SPLIT": "1"}))
        runtime.run(["systemctl", "start", service.VOICE_UNIT], timeout=60, quiet=True)
        runtime.http_check("voice-studio")
        # Existing Voice service keeps synthesis workers; Song owns only Audio.
        runtime.atomic_link(SONG_CURRENT, SONG_RELEASES / revision)
        env_payload = service.render_env(root) + (
            "AUDIO_DOWNLOADER_YTDLP_BIN="
            + str(BASE / "venvs/song-studio" / revision / "bin/yt-dlp") + "\n"
        )
        with SONG_ENV.open("xb") as fp:
            fp.write(env_payload.encode("utf-8"))
        SONG_ENV.chmod(0o600)
        SONG_UNIT_PATH.write_text(service.render_unit(revision, root))
        SONG_UNIT_PATH.chmod(0o644)
        changed_song = True
        runtime.run(["systemctl", "daemon-reload"], quiet=True)
        runtime.run(["systemd-analyze", "verify", SONG_UNIT_PATH], quiet=True)
        runtime.run(["systemctl", "enable", service.SONG_UNIT], timeout=60, quiet=True)
        runtime.run(["systemctl", "start", service.SONG_UNIT], timeout=70, quiet=True)
        _health_song()
        # Route flip LAST: no user can claim a Song job until sole worker
        # ownership and loopback health are verified.
        runtime.atomic_link(SCENE_CURRENT, scene_candidate)
        changed_scene = True
        _atomic_env(SCENE_ENV, _patch_env(scene_env_original, {
            "SONG_AUDIO_SPLIT_UPSTREAM": "http://127.0.0.1:8152"}))
        runtime.run(["systemctl", "restart", service.SCENE_UNIT], timeout=70, quiet=True)
        _health_scene()
        acceptance = _live_http_acceptance()
        runtime.atomic_json(journal / "result.json", {
            "status": "ACTIVE", "voice": "SYNTHESIS_ONLY", "song_audio": "SOLE_WORKER",
            "scene_route": "8152", "acceptance": acceptance,
            "sqlite_backup": str(sqlite_backup)})
        runtime.event(song_audio_cutover="PASS", commit=revision,
                      worker="ONE", route="SONG_8152",
                      original_database="PRESERVED", rollback=str(journal))
    except BaseException:
        # Never restart the old Voice worker over a new pending Song queue.
        # If a job arrived, keep the single Song process and fail closed for
        # human reconciliation, rather than corrupting a download or handoff.
        if changed_song:
            runtime.require_voice_release_idle(runtime.DATA / "voice-studio",
                                               audio_db=sqlite_src)
        errors = []
        if changed_scene:
            try:
                _atomic_env(SCENE_ENV, scene_env_original)
                runtime.atomic_link(SCENE_CURRENT, previous_scene)
                runtime.run(["systemctl", "restart", service.SCENE_UNIT], timeout=70, quiet=True)
                scene._health()
            except BaseException as exc:
                errors.append("Scene rollback: " + type(exc).__name__)
        if changed_song:
            try:
                runtime.run(["systemctl", "stop", service.SONG_UNIT], timeout=70, quiet=True)
                runtime.run(["systemctl", "disable", service.SONG_UNIT], quiet=True)
                SONG_UNIT_PATH.unlink(missing_ok=True)
                SONG_ENV.unlink(missing_ok=True)
                SONG_CURRENT.unlink(missing_ok=True)
                runtime.run(["systemctl", "daemon-reload"], quiet=True)
            except BaseException as exc:
                errors.append("Song rollback: " + type(exc).__name__)
        if changed_voice:
            try:
                # Fail closed if the Song worker is still alive: do not start
                # a competing legacy worker even during rollback.
                active = subprocess.run(["systemctl", "is-active", "--quiet", service.SONG_UNIT],
                                        capture_output=True).returncode == 0
                if active:
                    raise RuntimeError("Song worker still active; Voice rollback prohibited")
                runtime.run(["systemctl", "stop", service.VOICE_UNIT], timeout=60, quiet=True)
                _atomic_env(VOICE_ENV, voice_env_original)
                runtime.atomic_link(VOICE_CURRENT, previous_voice)
                runtime.run(["systemctl", "start", service.VOICE_UNIT], timeout=60, quiet=True)
                runtime.http_check("voice-studio")
            except BaseException as exc:
                errors.append("Voice rollback: " + type(exc).__name__)
        runtime.atomic_json(journal / "result.json", {
            "status": "ROLLBACK_FAILED" if errors else "ROLLED_BACK",
            "errors": errors, "original_database": "PRESERVED"})
        if errors:
            raise RuntimeError("Song Audio rollback incomplete: " + ", ".join(errors))
        raise

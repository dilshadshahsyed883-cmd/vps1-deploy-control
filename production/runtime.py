#!/usr/bin/env python3
"""Install immutable application releases on VPS_1 through its Drive agent.

Runtime data and credentials stay on the host. A failed app is restored before
the next app runs. This module deliberately does not change GitHub SSH jobs,
the existing Scene Builder pilot, Storage Manager or Auto Script.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[1]
BASE = Path('/srv/fresh-website')
DATA = Path('/var/lib/fresh-website')
CONFIG = Path('/etc/fresh-website')
RCLONE = '/home/ubuntu/.config/masterctl/rclone.conf'
CORE = ['fastapi==0.115.12', 'uvicorn==0.34.2', 'python-multipart==0.0.20',
        'pillow==11.1.0', 'requests==2.32.3', 'jinja2==3.1.6',
        'itsdangerous==2.2.0', 'httpx==0.28.1']
CATALOG = {
    'scene-builder': dict(port=8080, module='production.scene_entry:app', health='/health',
                          ui='/', runtime='scene-builder-production',
                          packages=CORE + ['playwright==1.56.0', 'websockets==15.0.1']),
    'short-maker': dict(port=8146, module='app:app', health='/health', ui='/', packages=CORE),
    'short-stream': dict(port=8145, module='app:app', health='/health', ui='/',
                         packages=CORE + ['cryptography==44.0.3']),
    'self-media-library': dict(port=8147, module='app:app', health='/api/health', ui='/', packages=CORE + ['numpy==2.2.6', 'opencv-python-headless==4.11.0.86', 'onnxruntime==1.22.1', 'tokenizers==0.21.2', 'faster-whisper==1.1.1', 'yt-dlp[default]==2026.8.19']),
    'video-merger': dict(port=8148, script='app.py', health='/video-merger/health',
                         ui='/video-merger/', packages=['yt-dlp[default]', 'gdown==5.2.0', 'requests==2.32.3']),
    'voice-studio': dict(port=8143, module='vps1_omnivoice_app:app', health='/healthz',
                         ui='/', packages=CORE + ['edge-tts==7.3.1', 'kokoro-onnx==0.6.0', 'soundfile', 'numpy', 'yt-dlp']),
    'auto-caption-studio': dict(port=8144, module='web_backend.app:app', health='/health',
                                ui='/', packages=CORE + ['PySide6==6.8.3', 'faster-whisper==1.1.1']),
    'local-upload-manager': dict(port=8015, module='lum_engine.standalone_app:app',
                                 health='/local-upload-manager/health', ui='/local-upload-manager/app',
                                 requirements='part01/requirements.txt'),
    'copyright-uploader': dict(port=8020, module='lum_engine.standalone_app:app',
                               health='/copyright-uploader/health', ui='/copyright-uploader/app?tab=add_video',
                               requirements='part01/requirements.txt'),
}


def runtime_name(app):
    return CATALOG[app].get('runtime', app)


def event(**values):
    print(json.dumps(values, sort_keys=True), flush=True)


def run(args, *, cwd=None, timeout=300, quiet=False):
    result = subprocess.run([str(a) for a in args], cwd=cwd, timeout=timeout,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        # Package-install errors contain no environment values; never log env files.
        raise RuntimeError('command failed: ' + ' '.join(str(x) for x in args[:3]) +
                           '\n' + result.stdout[-3500:])
    if not quiet and result.stdout.strip():
        event(command=str(args[0]), output=result.stdout[-1200:])
    return result.stdout


def atomic_json(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.new')
    with tmp.open('w') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
    tmp.chmod(mode)
    os.replace(tmp, path)


def atomic_link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.new')
    if tmp.is_symlink():
        tmp.unlink()
    elif tmp.exists():
        raise RuntimeError('unexpected temporary path: ' + str(tmp))
    tmp.symlink_to(target)
    os.replace(tmp, path)


def free_port(port):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))


def check_host(revision):
    if os.geteuid() != 0 or socket.gethostname() != 'vps-01a23801':
        raise RuntimeError('installer requires approved VPS_1 root execution via Bridge')
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('an exact 40-character Git revision is required')
    if run(['git', 'rev-parse', 'HEAD'], cwd=REPO, quiet=True).strip() != revision:
        raise RuntimeError('checkout does not match approved source revision')
    if shutil.disk_usage('/').free < 5_000_000_000:
        raise RuntimeError('5 GB operational reserve is unavailable')
    if pwd.getpwnam('ubuntu').pw_uid != 1000:
        raise RuntimeError('unexpected existing runtime account')


def environment(app, state):
    env = {'PYTHONDONTWRITEBYTECODE': '1', 'FRESH_WEBSITE_INSTALL': '1',
           'RCLONE_CONFIG': RCLONE}
    if app == 'scene-builder':
        credentials = Path('/root/vps1-production-login.json')
        password = json.loads(credentials.read_text())['password'] if os.geteuid() == 0 and credentials.is_file() else secrets.token_urlsafe(32)
        env.update(WEB_DATA_DIR=str(state / 'web'), SCENE_PERMANENT_ROOT=str(state / 'permanent'),
                   SCENE_CHANNELS_FILE=str(state / 'scene_channels.json'),
                   SCRIPT_WORK_DATA_DIR=str(state / 'script-work'), SCRIPT_PROCESSOR_DATA_DIR=str(state / 'script-processor'),
                   MASTER_CONTROLLER_DB_PATH=str(state / 'master-controller.sqlite3'),
                   AUTOFLOW_FLOW_RESULT_ROOT=str(state / 'flow-results'), FRESH_AUTO_SCRIPT_READY='0',
                   WEB_USERNAME='admin', WEB_ACCESS_KEY=password, SESSION_SECRET=secrets.token_urlsafe(48),
                   PYTHONPATH=str(REPO), STORAGE_MANAGER_URL='http://127.0.0.1:8149')
        token_file = CONFIG / 'vps-storage-manager.env'
        if os.geteuid() == 0 and token_file.is_file():
            for line in token_file.read_text().splitlines():
                if line.startswith('STORAGE_MANAGER_TOKEN='):
                    env['STORAGE_MANAGER_TOKEN'] = line.split('=', 1)[1]
    elif app == 'short-maker':
        env['SHORT_MAKER_DATA'] = str(state)
    elif app == 'short-stream':
        env['SHORT_STREAM_DATA'] = str(state)
    elif app == 'self-media-library':
        env['SML_DATA_ROOT'] = str(state / 'sml')
    elif app == 'video-merger':
        env.update(VIDEO_MERGER_ROOT=str(state / 'video-merger'),
                   VIDEO_MERGER_PROXY_CONFIG_DIR=str(state / 'video-merger/config'),
                   VIDEO_MERGER_PORT='8148')
    elif app == 'voice-studio':
        for key, child in [('VOICE_CLONE_ROOT', 'omnivoice'), ('KOKORO_STUDIO_ROOT', 'kokoro'),
                           ('EDGE_STUDIO_ROOT', 'edge'), ('AUDIO_DOWNLOADER_ROOT', 'audio-downloader')]:
            env[key] = str(state / child)
    elif app == 'auto-caption-studio':
        env.update(AUTO_CAPTION_APP_ROOT=str(state / 'acs'),
                   AUTO_CAPTION_DB_PATH=str(state / 'acs/data/database/auto_caption.db'),
                   AUTO_CAPTION_CODE_ROOT=str(BASE / 'current' / app / 'part01'))
    elif app == 'copyright-uploader':
        env.update(LUM_DATA_DIR=str(state / 'lum-copyright'),
                   YOUTUBE_UPLOAD_MANAGER_DATA_DIR=str(state / 'lum-copyright/core'),
                   LUM_AUTO_ENGINE_V6_ROOT=str(state / 'lum-copyright/auto_engine_v6'),
                   LUM_GOOGLE_OAUTH_ROOT=str(state / 'lum-copyright/secure'),
                   LUM_SESSION_SECRET=secrets.token_urlsafe(48),
                   LUM_BASE_PATH='/copyright-uploader',
                   LUM_ENGINE_INSTANCE='copyright',
                   COPYRIGHT_MEDIA_ROOT=str(DATA / 'video-merger/video-merger/copyright_handoff'),
                   LUM_SUITE_AUTH='1', LUM_SUITE_LOGIN_URL='/',
                   LUM_COOKIE_SECURE='0')
    elif app == 'local-upload-manager':
        env.update(LUM_DATA_DIR=str(state / 'lum1'),
                   YOUTUBE_UPLOAD_MANAGER_DATA_DIR=str(state / 'lum1/core'),
                   LUM_AUTO_ENGINE_V6_ROOT=str(state / 'lum1/auto_engine_v6'),
                   LUM_GOOGLE_OAUTH_ROOT=str(state / 'lum1/secure'),
                   LUM_SESSION_SECRET=secrets.token_urlsafe(48),
                   LUM_BASE_PATH='/local-upload-manager', LUM_SUITE_AUTH='1',
                   LUM_SUITE_LOGIN_URL='/', LUM_COOKIE_SECURE='0')
    return env


def service_unit(app, revision):
    spec = CATALOG[app]
    python = BASE / 'venvs' / app / revision / 'bin/python'
    command = (f'{python} {spec["script"]}' if 'script' in spec else
               f'{python} -m uvicorn {spec["module"]} --host 127.0.0.1 --port {spec["port"]}')
    return f'''[Unit]
Description=VPS1 production {app}
After=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory={BASE}/current/{runtime_name(app)}/part01
EnvironmentFile={CONFIG}/production-{app}.env
Environment=PATH={BASE}/venvs/{app}/{revision}/bin:/usr/local/bin:/usr/bin:/bin
ExecStart={command}
Restart=on-failure
RestartSec=3
TimeoutStopSec=35
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={DATA}/{runtime_name(app)} -/home/ubuntu/.config/masterctl

[Install]
WantedBy=multi-user.target
'''


def http_check(app, timeout=45):
    spec = CATALOG[app]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{spec["port"]}{spec["health"]}', timeout=3) as r:
                body = json.loads(r.read(131072))
                if r.status == 200 and body.get('ok') is True:
                    return body
        except (OSError, ValueError):
            pass
        time.sleep(.4)
    raise RuntimeError('app health failed: ' + app)


def require_voice_release_idle(state: Path) -> None:
    """Fail closed on Voice synthesis and active uploads before any worker restart.

    Completed, failed and cancelled historical rows are preserved. A pending
    Drive upload may survive worker replacement; a transferring file may not.
    """
    engines = (
        (state / 'kokoro/data/kokoro_jobs.sqlite3', 'Kokoro'),
        (state / 'edge/data/edge_jobs.sqlite3', 'Edge'),
    )
    for database, label in engines:
        if not database.is_file():
            raise RuntimeError(label + ' DB unavailable for release preflight')
        with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True, timeout=4) as db:
            db.execute('PRAGMA query_only=ON')
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            if not {'status', 'delivery_status'} <= columns:
                raise RuntimeError(label + ' job schema invalid for release preflight')
            count = db.execute("""SELECT count(*) FROM jobs
                  WHERE status IN ('queued','processing','paused','pause_requested',
                    'cancel_requested','retry_wait')
                    OR delivery_status='transferring'""").fetchone()[0]
            if count:
                raise RuntimeError(label + ' has pending/in-flight synthesis or upload; release postponed')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = 'http://127.0.0.1:8143/tts/api/audio-downloader/status'
    try:
        with opener.open(url, timeout=5) as response:
            status = json.loads(response.read(32768))
            if response.status != 200 or not isinstance(status, dict) or not isinstance(status.get('queue'), dict):
                raise RuntimeError('Audio Downloader preflight malformed')
            queue = status['queue']
            if any(int(queue.get(x) or 0) for x in (
                    'queued','downloading','downloaded','processing','converting','uploading','retry_pending')):
                raise RuntimeError('Audio Downloader has active/pending jobs; release postponed')
    except (OSError, ValueError) as exc:
        raise RuntimeError('Audio Downloader preflight unreachable') from exc


def backup_databases(state, destination):
    """SQLite backup includes committed WAL transactions; media stays in place."""
    outputs = []
    for source in sorted(state.rglob('*')):
        if source.suffix not in {'.db', '.sqlite', '.sqlite3'} or not source.is_file():
            continue
        if source.is_symlink():
            raise RuntimeError('database symlink outside app state')
        relative = source.relative_to(state)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite backup integrity failed')
        target.chmod(0o600)
        outputs.append(str(relative))
    return outputs


def stage_source(app, revision):
    # Reuse the repository's tested source-only export and credential exclusion.
    from rebuild.fresh_vps import sources
    release = BASE / 'releases' / runtime_name(app) / revision
    files = sources(app)
    manifest = {'app': app, 'revision': revision,
                'files': {n: hashlib.sha256(b).hexdigest() for n, b in files}}
    if release.exists():
        if not (release / '_production_manifest.json').is_file():
            raise RuntimeError('refusing to adopt another installer release')
        if json.loads((release / '_production_manifest.json').read_text()) != manifest:
            raise RuntimeError('immutable source manifest mismatch')
        for name, digest in manifest['files'].items():
            if hashlib.sha256((release / name).read_bytes()).hexdigest() != digest:
                raise RuntimeError('installed source drift')
    else:
        release.mkdir(parents=True)
        for name, content in files:
            target = release / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o644)
        atomic_json(release / '_production_manifest.json', manifest, 0o644)
    return release


def ensure_video_merger_venv_pointer(media: Path, venv: Path, managed_root: Path | None = None) -> str:
    """Never overwrite an existing managed Video Merger interpreter symlink.

    Old runtime installs can leave a dangling but owned symlink after GC. Python
    runs from its explicit immutable release interpreter, so the compatibility
    pointer does not have to be replaced during staging. Unowned links or real
    directories fail closed rather than risking deletion of production files.
    """
    managed = (managed_root or BASE / 'venvs/video-merger').resolve(strict=False)
    new_target = venv.resolve(strict=False)
    if not new_target.is_relative_to(managed) or new_target == managed:
        raise RuntimeError('Video Merger candidate venv escaped managed releases')
    pointer = media / 'venv'
    if pointer.is_symlink():
        previous_target = pointer.resolve(strict=False)
        if (previous_target == managed or
                not previous_target.is_relative_to(managed)):
            raise RuntimeError('Unowned Video Merger venv symlink: preserve and block')
        return 'PRESERVED_EXISTING'
    if pointer.exists():
        raise RuntimeError('Unmanaged Video Merger venv path: preserve and block')
    pointer.symlink_to(venv)
    return 'CREATED'


def restore(app, previous):
    unit_name = f'vps1-production-{app}.service'
    unit = Path('/etc/systemd/system') / unit_name
    current = BASE / 'current' / runtime_name(app)
    from production import workers
    workers.stop(app)
    subprocess.run(['systemctl', 'stop', unit_name], capture_output=True)
    if previous['unit'] is None:
        subprocess.run(['systemctl', 'disable', unit_name], capture_output=True)
        unit.unlink(missing_ok=True)
    else:
        unit.write_text(previous['unit'])
    if previous['current']:
        atomic_link(current, previous['current'])
    elif current.is_symlink():
        current.unlink()
    run(['systemctl', 'daemon-reload'], quiet=True)
    if previous['active']:
        run(['systemctl', 'start', unit_name], quiet=True)
        http_check(app)
    workers.restore(previous.get('workers', {}))
    event(app=app, rollback='PASS', data='PRESERVED')


def shortstudio_rolled_back_empty(state: Path, *, unit_dir: Path = Path("/etc/systemd/system"), preserve_secure: bool = False) -> bool:
    """Prove that a failed first install left *no* Short Studio owner state.

    This permits a fresh isolated 8019 instance to be installed when the old
    installer rolled back its services but retained an empty schema. Any
    existing unit, live listener, channel, job, OAuth, media, or unfamiliar
    database state fails closed; never use this rule for another uploader.
    """
    root = state / "lum-shortstudio"
    marker = root / ".vps1-independent-engine"
    if (root.is_symlink() or not root.is_dir() or marker.is_symlink()
            or not marker.is_file() or marker.read_text().strip() != "shortstudio"):
        return False
    for suffix in ("web", "worker", "scheduler"):
        unit = unit_dir / ("vps1-production-lum-shortstudio-" + suffix + ".service")
        if unit.exists() or unit.is_symlink():
            return False
    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", 8019)) == 0:
            return False

    # The core and V6 databases are read only. Any row in a user-owned
    # channel/project/media/schedule table proves this is not an empty first
    # install, even when the worker is stopped or a job is merely queued.
    databases = (
        (root / "core/data/uploader.db",
         ("videos", "channels", "auto_engine_profiles", "accounts")),
        (root / "auto_engine_v6/auto_engine_v6.sqlite3",
         ("profile_sources", "drive_videos", "thumbnails", "job_sources",
          "upload_records", "script_history", "missed_alarms")),
    )
    for db, tables in databases:
        if db.is_symlink() or (db.exists() and not db.is_file()):
            return False
        if not db.exists():
            continue
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=3) as conn:
            conn.execute("PRAGMA query_only=ON")
            existing = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for name in tables:
                if name in existing and conn.execute(
                        "SELECT 1 FROM " + name + " LIMIT 1").fetchone():
                    return False
            if "engine_state" in existing:
                rows = conn.execute(
                    "SELECT running, scheduler_armed FROM engine_state").fetchall()
                if len(rows) != 1 or any(int(v) != 0 for v in rows[0]):
                    return False
    # ensure_dirs() creates empty oauth_clients/ and oauth_sessions/ below
    # secure/ on first startup. Those two empty *directories* are scaffolding,
    # not authenticated user state. Any file, unknown entry, symlink, or content
    # inside either one is real/unknown state and must keep the idle fence.
    secure = root / "secure"
    if secure.is_symlink() or (secure.exists() and not secure.is_dir()):
        return False
    if secure.exists():
        if preserve_secure:
            # Owner's OAuth/session artifacts are opaque; PRESERVE them.
            # Inspect only filesystem type, never names or content. Reject
            # links, special files and unbounded trees.
            count = 0
            for child in secure.rglob("*"):
                count += 1
                if (count > 512 or child.is_symlink()
                        or not (child.is_dir() or child.is_file())):
                    return False
        else:
            for child in secure.iterdir():
                if (child.name not in {"oauth_clients", "oauth_sessions"}
                        or child.is_symlink() or not child.is_dir()
                        or any(child.iterdir())):
                    return False
    if preserve_secure:
        # Existing project tasks must not be resumed/restarted implicitly.
        projects = root / "projects.sqlite3"
        if projects.is_symlink() or (projects.exists() and not projects.is_file()):
            return False
        if projects.exists():
            with sqlite3.connect(projects.as_uri() + "?mode=ro", uri=True, timeout=3) as conn:
                conn.execute("PRAGMA query_only=ON")
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone():
                    if conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
                        return False
        for relative in ("staging", "thumbnails"):
            p = root / relative
            if p.is_symlink() or (p.exists() and (not p.is_dir() or any(p.iterdir()))):
                return False
        # Previous failed installs retain diagnostic/history reports. Reports
        # are not queued source media or work claims and must be preserved.
        # Validate file types without opening, moving, rewriting, or deleting.
        reports = root / "reports"
        if reports.is_symlink() or (reports.exists() and not reports.is_dir()):
            return False
        if reports.exists():
            count = 0
            for item in reports.rglob("*"):
                count += 1
                if (count > 10000 or item.is_symlink()
                        or not (item.is_file() or item.is_dir())):
                    return False
    for relative in ("auto_engine_v6/staging", "auto_engine_v6/thumbnails",
                     "auto_engine_v6/thumbnail_trash", "auto_engine_v6/reports"):
        directory = root / relative
        if directory.is_symlink() or (directory.exists() and
                (not directory.is_dir() or any(directory.iterdir()))):
            return False
    return True


def lum_parked_network_idle(conn: sqlite3.Connection) -> int:
    """Allow release only for proven parked, pre-upload Auto Engine network waits.

    WAITING_NETWORK is not by itself evidence of active media upload. A row is
    safe only if it has no claim, remote/resumable identity, sent media bytes or
    unsafe ledger state. Fail closed on unknown/legacy schemas and any doubt.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM videos WHERE UPPER(status)='WAITING_NETWORK' LIMIT 1001"
    ).fetchall()
    if not rows:
        return 0
    if len(rows) > 1000:
        raise RuntimeError("Too many network waits to prove release idle")
    required = {
        "auto_engine_profile_id", "claim_worker_id", "recovery_state",
        "youtube_video_id", "youtube_url", "resumable_uri",
        "media_stage_state", "pipeline_stage", "upload_bytes_confirmed",
        "progress", "channel_db_id", "upload_fingerprint",
    }
    columns = {str(c[1]) for c in conn.execute("PRAGMA table_info(videos)")}
    if not required.issubset(columns):
        raise RuntimeError("Network wait schema lacks idle proof columns")
    ledger_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='upload_ledger'"
    ).fetchone()
    for row in rows:
        if not row["auto_engine_profile_id"] or str(row["claim_worker_id"] or "").strip():
            raise RuntimeError("Network wait still belongs to a worker or non-Auto Engine job")
        if str(row["recovery_state"] or "").upper() not in {"", "NONE"}:
            raise RuntimeError("Network wait has unresolved recovery evidence")
        if any(str(row[k] or "").strip() for k in ("youtube_video_id", "youtube_url", "resumable_uri")):
            raise RuntimeError("Network wait has remote/resumable upload identity")
        if int(row["upload_bytes_confirmed"] or 0) or int(row["progress"] or 0):
            raise RuntimeError("Network wait has upload progress")
        if str(row["media_stage_state"] or "PENDING").upper() != "PENDING":
            raise RuntimeError("Network wait media stage already began")
        if str(row["pipeline_stage"] or "PENDING").upper() not in {"PENDING", "PREPARE"}:
            raise RuntimeError("Network wait pipeline already advanced")
        fingerprint = str(row["upload_fingerprint"] or "").strip()
        if fingerprint:
            if not ledger_table:
                raise RuntimeError("Network wait ledger unavailable")
            ledger = conn.execute(
                "SELECT state FROM upload_ledger WHERE channel_db_id=? AND upload_fingerprint=?",
                (int(row["channel_db_id"] or 0), fingerprint),
            ).fetchone()
            if not ledger:
                raise RuntimeError("Network wait ledger entry missing")
            if str(ledger["state"] or "").upper() not in {"RESERVED", "CLAIMED", "FAILED_SAFE", "CANCELLED"}:
                raise RuntimeError("Network wait ledger has possible remote upload evidence")
    return len(rows)


def require_lum_release_idle(state: Path) -> None:
    """Fail closed before restarting any uploader web/worker process.

    The three web instances expose in-flight project IDs. Check their exact
    installed loopback routes, then read the separate SQLite video queues
    without modifying them. Queued/scheduled work remains preserved on disk.
    """
    modes = (
        ("engine1", 8015, "/local-upload-manager/health", "lum1"),
        ("engine2", 8016, "/local-upload-manager/modes/engine2/health", "lum2"),
        ("shorts", 8017, "/local-upload-manager/modes/shorts/health", "lum3"),
    )
    # Song Uploader is optional on the first rollout; once installed it must
    # pass the same idle checks before any shared uploader runtime restarts.
    if ((state / "lum-song/core/data/uploader.db").exists() or
            Path("/etc/systemd/system/vps1-production-lum-song-web.service").exists()):
        modes += (("song", 8018, "/local-upload-manager/modes/song/health", "lum-song"),)
    if ((state / "lum-shortstudio/core/data/uploader.db").exists() or
            Path("/etc/systemd/system/vps1-production-lum-shortstudio-web.service").exists()):
        # After a failed first-install rollback, service units can be absent
        # while an initialized but unused database remains on disk. Prove it
        # contains zero user/channel/job/media state before skipping 8019
        # health on this ONE unprovisioned instance.
        try:
            first_install = shortstudio_rolled_back_empty(state)
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            raise RuntimeError("LUM_RELEASE_BLOCKED: shortstudio empty-install proof failed (" +
                               type(exc).__name__ + ")") from exc
        if first_install:
            event(app="local-upload-manager", shortstudio_first_install="EMPTY_ROLLBACK_PROVEN")
        else:
            # A separate one-off recovery intent may reuse *preserved* OAuth
            # material only after stricter job/scheduler/media/port/unit
            # checks. This never becomes the default LUM release policy.
            intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
            retained_idle = False
            if '[vps1-shortstudio-preserved-recovery]' in intent:
                try:
                    retained_idle = shortstudio_rolled_back_empty(state, preserve_secure=True)
                except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
                    raise RuntimeError(
                        "LUM_RELEASE_BLOCKED: shortstudio preserved-state proof failed ("
                        + type(exc).__name__ + ")") from exc
            if retained_idle:
                event(app="local-upload-manager",
                      shortstudio_recovery="PRESERVED_SECURE_IDLE_PROVEN")
            else:
                modes += (("shortstudio", 8019, "/short-uploader/health", "lum-shortstudio"),)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    in_flight = ("CLAIMED", "UPLOADING", "THUMBNAIL_UPLOADING",
                 "PROCESSING", "IN_PROGRESS")
    for mode, port, route, leaf in modes:
        try:
            with opener.open(f"http://127.0.0.1:{port}{route}", timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError("engine did not return HTTP 200")
                health = json.loads(response.read(4096))
            if (health.get("ok") is not True or
                    health.get("engine_instance") != mode):
                raise RuntimeError("engine identity/readiness mismatch")
            if health.get("active_project_id") not in (None, "", 0):
                raise RuntimeError("in-flight web upload")
            database = state / leaf / "core" / "data" / "uploader.db"
            if database.is_file():
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                                     timeout=3) as conn:
                    conn.execute("PRAGMA query_only=ON")
                    has_videos = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='videos'"
                    ).fetchone()
                    if has_videos:
                        matches = conn.execute(
                            "SELECT 1 FROM videos WHERE UPPER(status) IN (" +
                            ",".join("?" for _ in in_flight) + ") LIMIT 1",
                            in_flight,
                        ).fetchone()
                        if matches:
                            raise RuntimeError("in-flight SQLite upload")
                        parked = lum_parked_network_idle(conn)
                        if parked:
                            event(app="local-upload-manager", mode=mode,
                                  safe_pre_media_network_waits=parked)
        except (OSError, ValueError, TypeError, sqlite3.Error, RuntimeError) as exc:
            raise RuntimeError(
                "LUM_RELEASE_BLOCKED: uploader " + mode +
                " is busy or its idle state cannot be proven (" +
                type(exc).__name__ + ")"
            ) from exc
    event(app="local-upload-manager", uploader_idle_preflight="PASS",
          checked_modes=[mode for mode, *_ in modes])


def require_sml_idle(state):
    database = Path(state) / 'sml/library.db'
    if not database.is_file():return
    with sqlite3.connect(database.as_uri() + '?mode=ro',uri=True,timeout=5) as c:
        c.execute('PRAGMA query_only=ON')
        for table,where in (('sources',"status='processing'"),('media_imports',"status='processing'"),('ai_jobs',"state='running'")):
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                if c.execute('SELECT 1 FROM '+table+' WHERE '+where+' LIMIT 1').fetchone():
                    raise RuntimeError('SML_RELEASE_BLOCKED: active '+table+' work; retry after completion')


def require_copyright_uploader_idle(state: Path) -> None:
    """Protect all Copyright manual uploads and cleanup during web release.

    Never stop a claimed upload, a queue handoff or a cleanup transaction.
    Fail closed on missing/corrupt health/SQLite while a live unit is active.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8020/copyright-uploader/health", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("COPYRIGHT_RELEASE_BLOCKED: health unavailable")
        health = json.loads(response.read(8192))
    if health.get("ok") is not True or health.get("engine_instance") != "copyright":
        raise RuntimeError("COPYRIGHT_RELEASE_BLOCKED: wrong service identity")
    if health.get("active_project_id") not in (None, "", 0):
        raise RuntimeError("COPYRIGHT_RELEASE_BLOCKED: active media upload")
    database = state / "lum-copyright/projects.sqlite3"
    if database.is_symlink() or not database.is_file():
        raise RuntimeError("COPYRIGHT_RELEASE_BLOCKED: project database missing or unsafe")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only=ON")
        outstanding = conn.execute(
            """SELECT 1 FROM projects
               WHERE status NOT IN ('COMPLETED','CANCELLED','FAILED') LIMIT 1"""
        ).fetchone()
        if outstanding:
            raise RuntimeError("COPYRIGHT_RELEASE_BLOCKED: queued/active Copyright project")
    core = state / "lum-copyright/core/data/uploader.db"
    if core.is_symlink():
        raise RuntimeError("COPYRIGHT_RELEASE_BLOCKED: unsafe core DB")
    if core.is_file():
        with sqlite3.connect(core.as_uri() + "?mode=ro", uri=True, timeout=5) as conn:
            conn.execute("PRAGMA query_only=ON")
            busy = conn.execute(
                """SELECT 1 FROM videos WHERE UPPER(status) IN
                   ('CLAIMED','UPLOADING','THUMBNAIL_UPLOADING','PROCESSING','IN_PROGRESS',
                    'WAITING_NETWORK','PAUSED','QUEUED') LIMIT 1"""
            ).fetchone()
            if busy:
                raise RuntimeError("COPYRIGHT_RELEASE_BLOCKED: core upload queue not idle")
    event(app="copyright-uploader", upload_idle_preflight="PASS")


def require_short_maker_idle(state):
    """Never interrupt an existing Short Maker render during navigation migration."""
    jobs = state / "jobs"
    if not jobs.is_dir():
        return
    for item in jobs.glob("*/state.json"):
        if item.is_symlink() or not item.is_file():
            raise RuntimeError("Short Maker state ownership drift")
        try:
            record = json.loads(item.read_text())
        except (ValueError, OSError) as exc:
            raise RuntimeError("Unreadable Short Maker job state blocks release") from exc
        status = str(record.get("status") or "").lower().strip()
        if status not in {"completed", "failed", "cancelled", "canceled"}:
            raise RuntimeError("Short Maker has an unfinished job; preserving live render")


def install_app(app, revision):
    if app not in CATALOG:
        raise ValueError('app is not owned by this installer')
    if app == 'local-upload-manager':
        intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-song-uploader-automation-summary]' in intent:
            from production.song_uploader_group_summary_release import install as install_song_group_summary
            install_song_group_summary(revision)
            return
        if '[vps1-short-uploader-group-metadata]' in intent:
            from production.short_uploader_group_release import install as install_short_groups
            install_short_groups(revision)
            return
    if app == 'video-merger':
        intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-video-merger-import-release]' in intent:
            from production.video_merger_import_release import install
            install(revision)
            return
    # The public Scene service is the original isolated candidate on port 18080,
    # not the later generic port-8080 service.  Keep its unit/env/data ownership
    # and update only its immutable source pointer through the same root helper.
    if app == 'scene-builder' and (BASE / 'current/scene-builder').is_symlink():
        intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-storage-dashboard-web-only]' in intent:
            # Web-only admin Storage overlay; queued Scene renders are
            # deliberately NOT admission-gated by renderer source refresh.
            from production.storage_dashboard_web_release import install as install_storage_web
            install_storage_web(revision)
            return
        if '[vps1-copyright-studio-release]' in intent:
            from production.copyright_scene_nav_release import install as install_copyright_scene
            install_copyright_scene(revision)
            return
        if '[vps1-song-uploader-settings-fix]' in intent:
            # Candidate POST gate is independent from the existing Song uploader
            # database and worker. Release only that policy, not all Scene code.
            from production.song_uploader_candidate_policy_release import install as install_song_policy
            install_song_policy(revision)
            return
        if '[vps1-song-studio-release]' in intent:
            from production.song_studio_release import install_scene
            install_scene(revision)
            return
        if '[vps1-scene-delete-web-only]' in intent:
            from production.scene_cancelled_delete_web_release import install as release_cancelled_delete
            release_cancelled_delete(revision)
            return
        if '[vps1-scene-web-policy-only]' in intent:
            from production.scene_manual_start_web_release import install as release_manual_start
            release_manual_start(revision)
            return
        # Explicitly UI-only: clone the running Scene web source, changing just
        # the Auto Script CSS + its scoped link. Never refresh busy renderers.
        if '[vps1-auto-script-ui-overlay]' in intent:
            from production.scene_auto_script_ui_overlay import install as release_auto_script_ui
            release_auto_script_ui(revision)
            return
        from production.scene_candidate_release import install as install_scene_candidate
        install_scene_candidate(revision)
        return
    if app in {'short-maker', 'video-merger', 'auto-caption-studio'}:
        commit_message = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-video-template-only]' in commit_message:
            from production.video_template_release import install as install_video_template
            install_video_template(app, revision)
            return
    # Explicit protected-main Voice template-only intent: never restart or
    # provision independent synthesis workers just to change sidebar markup.
    if app == 'voice-studio':
        commit_message = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-song-studio-release]' in commit_message:
            from production.song_studio_release import install_voice
            install_voice(revision)
            return
        if '[vps1-voice-edge-recovery-only]' in commit_message:
            from production.voice_edge_worker_recovery import install as repair_edge_worker
            repair_edge_worker(revision)
            return
        if '[vps1-voice-kokoro-recovery-only]' in commit_message:
            from production.voice_kokoro_worker_recovery import install as repair_kokoro_worker
            repair_kokoro_worker(revision)
            return
        if '[vps1-audio-error-label-only]' in commit_message:
            from production.audio_downloader_release import install as label_audio_download_error
            label_audio_download_error(revision, restore_proxy=False, include_thumbnail=True, diagnostic_only=True)
            return
        if '[vps1-audio-restore-only]' in commit_message:
            from production.audio_downloader_release import install as restore_audio_download
            restore_audio_download(revision, restore_proxy=False, include_thumbnail=True, restore_download=True)
            return
        if '[vps1-audio-thumbnail-only]' in commit_message:
            from production.audio_downloader_release import install as install_audio_thumbnail
            install_audio_thumbnail(revision, restore_proxy=False, include_thumbnail=True)
            return
        if '[vps1-audio-filename-only]' in commit_message:
            from production.audio_downloader_release import install as install_audio_filename
            install_audio_filename(revision, restore_proxy=False)
            return
        if '[vps1-audio-proxy-only]' in commit_message:
            from production.audio_downloader_release import install as install_audio_proxy
            install_audio_proxy(revision)
            return
        if '[vps1-voice-tts-controls-only]' in commit_message:
            from production.voice_tts_controls_release import install as install_voice_tts_controls
            install_voice_tts_controls(revision)
            return
        if '[vps1-voice-queue-api-only]' in commit_message:
            from production.voice_queue_release import install as install_voice_queue_api
            install_voice_queue_api(revision)
            return
        if '[vps1-voice-template-only]' in commit_message:
            from production.voice_template_release import install as install_voice_template
            install_voice_template(revision)
            return
    # Owner-approved website re-audit touches exact web files only. Workers stay live.
    if app in {'auto-caption-studio','voice-studio','self-media-library','short-stream','video-merger'}:
        commit_message = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-website-reaudit-release]' in commit_message:
            from production.website_reaudit_release import install as install_website_reaudit
            install_website_reaudit(app, revision)
            return
    unit_name = f'vps1-production-{app}.service'
    unit = Path('/etc/systemd/system') / unit_name
    current = BASE / 'current' / runtime_name(app)
    state = DATA / runtime_name(app)
    record = BASE / 'control/state' / (app + '.json')
    if current.exists() and not current.is_symlink():
        raise RuntimeError('existing app path is not a release pointer')
    if current.is_symlink() and not record.is_file():
        raise RuntimeError('existing app belongs to a different installer')
    old = str(current.resolve(strict=True)) if current.is_symlink() else None
    if old and not Path(old).is_relative_to(BASE / 'releases' / runtime_name(app)):
        raise RuntimeError('existing pointer escaped app releases')
    active = subprocess.run(['systemctl', 'is-active', '--quiet', unit_name]).returncode == 0
    if not active:
        free_port(CATALOG[app]['port'])
    previous = {'current': old, 'unit': unit.read_text() if unit.exists() else None, 'active': active}
    from production import workers
    previous['workers'] = workers.snapshot(app)
    if old == str(BASE / 'releases' / runtime_name(app) / revision) and active:
        stage_source(app, revision)
        http_check(app)
        event(app=app, result='PASS_ALREADY_INSTALLED', revision=revision)
        return
    release = stage_source(app, revision)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(state, 1000, 1000)
    env_file = CONFIG / f'production-{app}.env'
    CONFIG.mkdir(parents=True, exist_ok=True)
    if not env_file.exists():
        env = environment(app, state)
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(''.join(k + '=' + v + '\n' for k, v in env.items()))
    if app == 'short-stream':
        from production.auto_stream_oauth_env import ensure_key
        key_status = ensure_key(env_file, state)
        event(app=app, oauth_vault_key=key_status, secrets_logged=False)
    venv = BASE / 'venvs' / app / revision
    venv.parent.mkdir(parents=True, exist_ok=True)
    if not (venv / 'bin/python').is_file():
        venv.mkdir(exist_ok=True)
        os.chown(venv, 1000, 1000)
        run(['runuser', '-u', 'ubuntu', '--', 'python3', '-m', 'venv', venv], quiet=True)
    pip = [venv / 'bin/python', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir']
    packages = CATALOG[app].get('packages')
    args = packages if packages else ['-r', release / CATALOG[app]['requirements']]
    run(['runuser', '-u', 'ubuntu', '--', *pip, *args, 'httpx==0.28.1'], timeout=900, quiet=True)
    if app == 'self-media-library':
        # The production catalog overrides part01/requirements.txt; validate
        # the worker's actual immutable venv before switching its live pointer.
        downloader = venv / 'bin/yt-dlp'
        if not downloader.is_file() or not os.access(downloader, os.X_OK):
            raise RuntimeError('SML_YTDLP_MISSING: downloader absent from release environment')
        run(['runuser', '-u', 'ubuntu', '--', venv / 'bin/python',
             '-m', 'yt_dlp', '--version'], timeout=45, quiet=True)
        # The production package catalog overrides SML requirements.txt.  Verify
        # the exact face detector/recognizer runtime before activating a release,
        # rather than leaving 'Save person & scan images' silently disabled.
        run(['runuser', '-u', 'ubuntu', '--', venv / 'bin/python', '-c',
             'import cv2, numpy, PIL; '
             'assert callable(cv2.FaceDetectorYN.create) and '
             'callable(cv2.FaceRecognizerSF.create), "OpenCV face modules unavailable"'],
            timeout=45, quiet=True)
        event(app=app, dependency='opencv-face-engine', result='PASS_DEPENDENCY_INSTALLED',
              runtime=str(venv))
        event(app=app, dependency='yt-dlp', result='PASS_DEPENDENCY_INSTALLED',
              runtime=str(venv))
    if app == 'video-merger':
        from production.video_merger_import_release import install_js
        install_js(venv)
    freeze = run([venv / 'bin/python', '-m', 'pip', 'freeze'], quiet=True)
    (venv / 'installed-requirements.txt').write_text(freeze)
    # Exercise real startup/UI and a bounded local render before changing live pointers.
    run(['runuser', '-u', 'ubuntu', '--', venv / 'bin/python', '-m',
         'production.smoke', '--app', app], cwd=REPO, timeout=180, quiet=True)
    if app == 'self-media-library':
        intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-sml-ai-release]' in intent:
            models = state / 'sml' / 'ai_models'
            models.mkdir(parents=True, exist_ok=True)
            os.chown(models.parent, 1000, 1000)
            os.chown(models, 1000, 1000)
            run(['runuser','-u','ubuntu','--',venv / 'bin/python',
                 release / 'part01/ai_engine.py','--prepare',models],timeout=600,quiet=True)
            if not shutil.which('tesseract') or not Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf').is_file():
                run(['apt-get','update','-qq'],timeout=300,quiet=True)
                run(['apt-get','install','-y','--no-install-recommends','tesseract-ocr','fonts-dejavu-core'],timeout=300,quiet=True)
            speech_models = state / 'sml/speech_models'
            speech_models.mkdir(exist_ok=True)
            os.chown(speech_models,1000,1000)
            run(['runuser','-u','ubuntu','--',venv / 'bin/python',
                 release / 'part01/ai_text.py','--prepare',speech_models],timeout=600,quiet=True)
            run(['runuser','-u','ubuntu','--',venv / 'bin/python','-m',
                 'production.sml_ai_verify','--model-cache',models],cwd=REPO,timeout=240,quiet=True)
    if app == 'video-merger':
        media = state / 'video-merger'
        media.mkdir(exist_ok=True)
        os.chown(media, 1000, 1000)
        state_of_pointer = ensure_video_merger_venv_pointer(media, venv)
        event(app='video-merger', venv_pointer=state_of_pointer, original='PRESERVED')
    journal = BASE / 'transactions' / app / (revision + '-' + str(time.time_ns()))
    journal.mkdir(parents=True)
    atomic_json(journal / 'previous.json', previous)
    # Never interrupt an upload in any of the three independently running engines.
    if app == 'local-upload-manager' and active:
        require_lum_release_idle(state)
    if app == 'copyright-uploader' and active:
        require_copyright_uploader_idle(state)
    if app == 'short-maker' and active:
        require_short_maker_idle(state)
    if app == 'video-merger' and active:
        from production.video_template_release import idle as require_video_merger_idle
        require_video_merger_idle('video-merger')
    if app == 'voice-studio' and active:
        require_voice_release_idle(state)
    # Only the app being replaced is stopped. Other apps and all agents stay up.
    if app == 'self-media-library' and active:
        require_sml_idle(state)
    if active:
        run(['systemctl', 'stop', unit_name], quiet=True)
    workers.stop(app)
    changed = False
    short_studio_route_receipt = None
    copyright_studio_route_receipt = None
    try:
        backups = backup_databases(state, journal / 'sqlite-backup')
        changed = True
        atomic_link(current, release)
        unit.write_text(service_unit(app, revision))
        unit.chmod(0o644)
        run(['systemctl', 'daemon-reload'], quiet=True)
        run(['systemd-analyze', 'verify', unit], quiet=True)
        run(['systemctl', 'enable', unit_name], timeout=60, quiet=True)
        run(['systemctl', 'restart', unit_name], timeout=60, quiet=True)
        health = http_check(app)
        workers.install(app, revision)
        if app == 'self-media-library':
            intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
            if '[vps1-sml-local-release]' in intent:
                from production.sml_local_verify import verify as verify_local_sml
                verify_local_sml(venv)
        if app == 'scene-builder':
            from production.browser_runtime import install as install_browser
            install_browser()
        if shutil.disk_usage('/').free < 5_000_000_000:
            raise RuntimeError('operational disk reserve breached')
        if app == 'copyright-uploader':
            from production.copyright_studio_release import install_route
            copyright_studio_route_receipt = install_route(revision)
        if app == 'local-upload-manager':
            intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
            if '[vps1-short-studio-release]' in intent:
                from production.short_studio_release import install_route
                short_studio_route_receipt = install_route(revision)
        atomic_json(record, {'revision': revision, 'unit': unit_name, 'previous': previous,
                             'transaction': str(journal), 'status': 'ACTIVE'})
        event(app=app, result='PASS_SERVICE_HEALTH', revision=revision, port=CATALOG[app]['port'],
              sqlite_backups=len(backups), health=health, rollback=str(journal))
    except BaseException:
        try:
            if copyright_studio_route_receipt is not None:
                from production.copyright_studio_release import restore_route
                restore_route(copyright_studio_route_receipt)
            if short_studio_route_receipt is not None:
                from production.short_studio_release import restore_route
                restore_route(short_studio_route_receipt)
        finally:
            if changed or active:
                restore(app, previous)
        raise


@contextlib.contextmanager
def deployment_lock():
    with open('/run/lock/vps1-production.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'status', 'rollback'])
    parser.add_argument('--revision', required=True)
    parser.add_argument('--apps', nargs='+', choices=sorted(CATALOG), default=list(CATALOG))
    args = parser.parse_args()
    check_host(args.revision)
    with deployment_lock():
        intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        # Song Maker-only owner audit/claim, distinct from both parallel Maker
        # and uploader migrations; no app install or shared runtime modification.
        if '[vps1-haseeb1-songmaker-readonly]' in intent:
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError('Song Maker owner audit requires the exact approved LUM helper lane')
            from production.studio_haseeb1_songmaker_assignment import audit
            event(**audit(DATA / 'song-maker', DATA / 'scene-builder/web/web.sqlite3'))
            return
        if '[vps1-haseeb1-songmaker-assignment]' in intent:
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError('Song Maker owner claim requires the exact approved LUM helper lane')
            from production.studio_haseeb1_songmaker_assignment import install
            event(**install(args.revision, DATA / 'song-maker',
                  DATA / 'scene-builder/web/web.sqlite3',
                  BASE / 'transactions/haseeb1-songmaker-firstclaim'))
            return
        # Read-only dry run can be merged independently before any owner change.
        if '[vps1-haseeb1-song-short-readonly]' in intent:
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError('haseeb1 audit requires exact LUM lane')
            from production.studio_haseeb1_assignment import read_only_audit
            event(**read_only_audit(DATA, DATA / 'scene-builder/web/web.sqlite3'))
            return
        # Separate Short Maker data-only claim. This never deploys application
        # source, restarts a worker, or rewrites rendered media.
        if '[vps1-haseeb1-shortmaker-assignment]' in intent:
            if args.action != 'install' or args.apps != ['short-maker']:
                raise RuntimeError('haseeb1 Short Maker claim requires exact Short Maker lane')
            from production.studio_haseeb1_shortmaker_assignment import install_shortmaker_claim
            install_shortmaker_claim(args.revision)
            return
        # One narrowly authorized Song/Short legacy first-owner migration.
        # The approved local-upload-manager helper lane and host lock apply;
        # no global LUM install and no Copyright, Drive or Auto Script writes.
        if '[vps1-haseeb1-song-short-assignment]' in intent:
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError('haseeb1 owner assignment requires exact LUM lane')
            from production.studio_haseeb1_assignment import install_registry_only_online
            install_registry_only_online(args.revision)
            return
        # Strictly read-only per-user Studio inventory. The existing root
        # helper's approved local-upload-manager lane is reused; nothing is
        # installed/restarted, and no credentials/identifiers are logged.
        if '[vps1-studio-tenant-readonly-audit]' in intent:
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError('Studio tenant audit requires exact approved LUM lane')
            from production.studio_tenant_readonly_preflight import audit_all
            event(**audit_all(DATA))
            return
        # One reviewed Scene web overlay plus reversible uploader auth_request rules.
        if '[vps1-studio-permissions-release]' in intent:
            if args.apps != ['scene-builder']:
                raise RuntimeError('Three-Studio permissions require the Scene-only authorized lane')
            from production.studio_permissions_release import install, rollback
            if args.action == 'install':
                install(args.revision)
            elif args.action == 'rollback':
                rollback(args.revision)
            else:
                from production.scene_candidate_release import _health
                event(three_studio_permissions='READY', scene=_health())
            return
        # Trusted VPS root helper supports the existing local-upload-manager
        # lane, not a new app name. Dispatch only this explicitly SHA-pinned
        # Copyright intent to an entirely separate catalog/service/SQLite.
        # No code path below can touch the existing LUM mode workers.
        if '[vps1-copyright-studio-release]' in intent and args.apps == ['local-upload-manager']:
            target='copyright-uploader'
            if args.action == 'install':
                install_app(target, args.revision)
            elif args.action == 'status':
                event(app=target, health=http_check(target))
            elif args.action == 'rollback':
                record_path=BASE/'control/state'/ (target + '.json')
                if not record_path.is_file():
                    raise RuntimeError('Copyright rollback record not found')
                receipt=json.loads(record_path.read_text())
                if receipt.get('revision')!=args.revision:
                    raise RuntimeError('Copyright rollback revision changed')
                from production.copyright_studio_release import rollback_installed_route
                rollback_installed_route(args.revision)
                restore(target, receipt['previous'])
            else:
                raise RuntimeError('Unsupported Copyright operation')
            return
        # Preserve active protected-main Short Uploader network fairness release.
        if ('[vps1-shortstudio-network-fairness]' in intent or
                '[vps1-shortstudio-final-ui]' in intent):
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError('Short Uploader network fix requires isolated release lane')
            from production.short_uploader_group_release import install as install_network_fix
            install_network_fix(args.revision, network_only=True)
            return
        # Scoped, transactional Short Uploader template change. Never enter
        # the shared LUM installer or restart Song/Engine 1/Engine 2 workers.
        if '[vps1-shortstudio-intro-only]' in intent:
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError('Short Uploader UI-only release is single-instance scoped')
            from production.short_uploader_group_release import install as install_shortstudio_ui
            install_shortstudio_ui(args.revision, ui_only=True)
            return
        if '[vps1-song-worker-readonly-diagnose]' in intent:
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError("Song worker diagnosis must use only the serialized LUM lane")
            from production.song_uploader_worker_readonly_diagnostic import run as report_song_worker
            report_song_worker(DATA / 'local-upload-manager/lum-song')
            return  # No source install, queue change, service restart or rollback.
        if '[vps1-shortstudio-readonly-diagnose]' in intent:
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError("Short Studio read-only diagnosis is local-upload-manager scoped")
            from production.shortstudio_readonly_diagnostic import report
            report(DATA / 'local-upload-manager')
            return  # Deliberately no application install, service stop, or gateway refresh.
        if '[vps1-copyright-studio-release]' in intent and args.apps == ['scene-builder']:
            from production.copyright_scene_nav_release import install as scene_nav_install, rollback as scene_nav_rollback
            if args.action == 'install':
                scene_nav_install(args.revision)
            elif args.action == 'rollback':
                scene_nav_rollback(args.revision)
            elif args.action == 'status':
                event(app='scene-builder',copyright_nav='SCOPED_WEB_ONLY')
            else:
                raise RuntimeError('Unsupported Copyright Scene nav action')
            return
        # Exact one-off route-only correction for the owner's original LUM URL.
        # Never install/restart uploader services, rewrite OAuth/DB or touch queues.
        # Existing Scene-session Nginx publisher validates both identity and the
        # exact legacy Engine 1 URL and transactionally restores the site on failure.
        if '[vps1-song-folder-flow]' in intent:
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError('Song folder workflow requires the single serialized LUM release lane')
            from production.song_folder_release import install as install_song_folders
            install_song_folders(args.revision)
            return
        if '[vps1-lum-legacy-route-only]' in intent:
            if args.action != 'install' or args.apps != ['local-upload-manager']:
                raise RuntimeError("legacy LUM route release requires local-upload-manager-only install lane")
            from production.route_uploader_scene_https import deploy as activate_lum_legacy_route
            activate_lum_legacy_route(args.revision)
            event(lum_legacy_route="ENGINE1_VERIFIED", uploader_service_mutations="NONE")
            return
        # Owner-authorized one-off cleanup of the exact stalled Scene render.
        # Never enter normal Scene install or any unrelated application lane.
        if '[vps1-scene-exact-reset-3327-20261031]' in intent:
            if args.action != 'install' or args.apps != ['scene-builder']:
                raise RuntimeError('exact Scene reset requires scene-builder-only install lane')
            from production.scene_exact_reset_3327_20261031 import reset_exact_scene_job
            reset_exact_scene_job()
            return
        # One protected-main release, one specific Scene job. Never enter normal
        # install/rollback or restart a service during a cancellation request.
        intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
        if '[vps1-scene-job-terminate-3327-20261027]' in intent:
            if args.action != 'install' or args.apps != ['scene-builder']:
                raise RuntimeError('targeted Scene cancellation requires scene-builder-only install lane')
            from production.scene_exact_job_cancel_20261027 import terminate_exact_job
            terminate_exact_job()
            return
        for app in args.apps:
            if args.action == 'install':
                intent = run(['git', 'log', '-1', '--format=%B'], cwd=REPO, quiet=True)
                activate_sml_scene = (
                    app == 'self-media-library'
                    and '[vps1-sml-scene-activate]' in intent
                )
                if not activate_sml_scene:
                    install_app(app, args.revision)
                else:
                    # One narrow intent: install SML source, then link only
                    # SML + Scene web Editor credentials using a VPS-minted
                    # token. Roll back SML source if activation fails.
                    install_app(app, args.revision)
                    try:
                        from production.sml_scene_activation import activate
                        activated = activate()
                        event(sml_scene_link="EDITOR_API_READY",
                              runtime_render_worker="THREE_SCENE_RENDER_VERIFIED",
                              secrets_logged=False, **activated)
                    except BaseException:
                        record_path = BASE / 'control/state' / (app + '.json')
                        if record_path.is_file():
                            record = json.loads(record_path.read_text())
                            if record.get('revision') == args.revision:
                                restore(app, record['previous'])
                        raise
            elif args.action == 'status':
                event(app=app, health=http_check(app))
            else:
                record = json.loads((BASE / 'control/state' / (app + '.json')).read_text())
                restore(app, record['previous'])


if __name__ == '__main__':
    main()


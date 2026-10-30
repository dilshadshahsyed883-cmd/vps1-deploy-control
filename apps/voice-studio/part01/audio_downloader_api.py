"""Isolated YouTube audio extraction queue for the existing Voice Studio service.

All job state and audio belong to AUDIO_DOWNLOADER_ROOT; other voice engines are untouched.
Only explicitly authorized YouTube URLs are accepted. Optional owner-provided private cookies; no challenge bypass.
"""
from __future__ import annotations

import json
import importlib.util
import http.client
import shlex
import stat
import sys
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Literal

ROOT = Path(os.environ.get('AUDIO_DOWNLOADER_ROOT', '/home/ubuntu/apps/audio_downloader_voice_studio'))
DATA = ROOT / 'data'
DB = DATA / 'jobs.sqlite3'
JOBS = DATA / 'jobs'
DRIVE_REMOTE = os.environ.get('AUDIO_DOWNLOADER_DRIVE_REMOTE', 'gdrive')
DRIVE_BASE = 'Song Studio/Audio downloader'
RCLONE_CONFIG = '/home/ubuntu/.config/masterctl/rclone.conf'
YTDLP = os.environ.get('AUDIO_DOWNLOADER_YTDLP_BIN', str(ROOT / '.venv' / 'bin' / 'yt-dlp'))
router = APIRouter(prefix='/tts/api/audio-downloader', tags=['Voice Studio Audio Downloader'])
VIDEO_ID = re.compile(r'^[a-zA-Z0-9_-]{11}$')
JOB_ID = re.compile(r'^ADJ-[A-F0-9]{16}$')
_worker: threading.Thread | None = None
UPLOAD_GAP_SECONDS = 60
THUMBNAIL_GAP_SECONDS = 30
UPLOAD_RETRY_SECONDS = 180
SONG_MAKER_HOST = '127.0.0.1'
SONG_MAKER_PORT = 8150
SONG_MAKER_INGEST_PATH = '/song-maker/api/jobs'
SONG_MAKER_RETRY_SECONDS = 60


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def utc_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='seconds')


@contextmanager
def db():
    con = sqlite3.connect(str(DB), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=25000')
    try:
        yield con
    finally:
        con.close()


def init_storage() -> None:
    JOBS.mkdir(parents=True, exist_ok=True, mode=0o750)
    with db() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('''CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, video_id TEXT NOT NULL,
            url TEXT NOT NULL, project TEXT NOT NULL, folder TEXT NOT NULL,
            format TEXT NOT NULL, quality INTEGER NOT NULL,
            auto_upload INTEGER NOT NULL, keep_metadata INTEGER NOT NULL,
            status TEXT NOT NULL, title TEXT, local_file TEXT, drive_path TEXT,
            error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            started_at TEXT, completed_at TEXT
        )''')
        con.execute('CREATE INDEX IF NOT EXISTS audio_job_queue ON jobs(status,created_at)')
        if 'drive_url' not in {col[1] for col in con.execute('PRAGMA table_info(jobs)').fetchall()}:
            try:
                con.execute('ALTER TABLE jobs ADD COLUMN drive_url TEXT')
            except sqlite3.OperationalError as exc:
                if 'duplicate column name' not in str(exc).lower():
                    raise

        # Additive migration, preserving existing job records and queue status.
        columns = {col[1] for col in con.execute('PRAGMA table_info(jobs)').fetchall()}
        for name, sql_type in (('download_thumbnail', 'INTEGER NOT NULL DEFAULT 0'),
                               ('local_thumbnail', 'TEXT'), ('thumbnail_drive_path', 'TEXT'),
                               ('thumbnail_drive_url', 'TEXT'), ('next_retry_at', 'TEXT'),
                               ('upload_retry_count', 'INTEGER NOT NULL DEFAULT 0'),
                               ('download_percent', 'INTEGER NOT NULL DEFAULT 0'),
                               ('upload_percent', 'INTEGER NOT NULL DEFAULT 0'),
                               ('progress_stage', 'TEXT'),
                               ('thumbnail_upload_after', 'TEXT'),
                               ('auto_song_maker', 'INTEGER NOT NULL DEFAULT 0'),
                               ('song_maker_owner', 'TEXT'),
                               ('song_maker_status', "TEXT NOT NULL DEFAULT 'off'"),
                               ('song_maker_job_id', 'TEXT'),
                               ('song_maker_next_retry_at', 'TEXT')):
            if name not in columns:
                con.execute(f'ALTER TABLE jobs ADD COLUMN {name} {sql_type}')
        con.execute('''CREATE TABLE IF NOT EXISTS audio_upload_pacing (
            id INTEGER PRIMARY KEY CHECK (id=1), next_upload_at TEXT NOT NULL
        )''')
        # Owner-scoped download defaults; never infer these from queued jobs.
        con.execute('''CREATE TABLE IF NOT EXISTS audio_preferences (
            owner TEXT PRIMARY KEY, format TEXT NOT NULL,
            quality INTEGER NOT NULL, auto_upload INTEGER NOT NULL,
            keep_metadata INTEGER NOT NULL, download_thumbnail INTEGER NOT NULL,
            auto_song_maker INTEGER NOT NULL, updated_at TEXT NOT NULL
        )''')


class DriveUploadFailure(RuntimeError):
    """Known, sanitized Google Drive failure. Only confirmed API rate limits auto-retry."""
    def __init__(self, code: str):
        self.code = code
        behavior = 'automatic retry scheduled' if code == 'RATE_LIMITED' else 'manual retry required'
        super().__init__(f'Google Drive upload failed ({code}); {behavior}')


def _confirmed_rate_limit_error(error: str | None) -> bool:
    # Recognize only our sanitized persisted error marker, never arbitrary provider prose.
    return bool(error and error.startswith('Google Drive upload failed (RATE_LIMITED);'))


def recover_interrupted_jobs() -> None:
    # Startup recovery is conservative: an interruption is not proof of API rate limiting.
    # Preserve local media, verified receipts, and manual retries; never re-download automatically.
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        for row in con.execute("""SELECT job_id,error,progress_stage FROM jobs
                                  WHERE status='upload_retry'""").fetchall():
            if (row['progress_stage'] == 'manual retry requested'
                    or _confirmed_rate_limit_error(row['error'])):
                continue
            # Retire legacy 68+ retry loops from unknown/verification errors.
            con.execute("""UPDATE jobs SET status='failed',next_retry_at=NULL,
                           progress_stage='manual retry required',updated_at=?
                           WHERE job_id=? AND status='upload_retry'""", (utc(), row['job_id']))
        con.execute("""UPDATE jobs SET status='failed',next_retry_at=NULL,
                       error='Job interrupted; retry manually using saved local audio',
                       progress_stage='manual retry required',updated_at=?
                       WHERE status IN ('downloading','downloaded','uploading')""", (utc(),))
        con.execute('COMMIT')


def video_url(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or len(value) > 350:
        raise ValueError('Invalid YouTube URL length')
    try:
        p = urlsplit(value.strip())
        bad_authority = bool(p.username or p.password or p.port)
    except ValueError as exc:
        raise ValueError('Invalid YouTube URL') from exc
    if p.scheme != 'https' or bad_authority:
        raise ValueError('Only HTTPS YouTube links are supported')
    domain = (p.hostname or '').lower()
    if domain in ('youtu.be', 'www.youtu.be'):
        video = p.path.strip('/')
        if '/' in video:
            raise ValueError('Invalid short link')
    elif domain in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        if p.path == '/watch':
            ids = parse_qs(p.query, keep_blank_values=False).get('v', [])
            video = ids[0] if len(ids) == 1 else ''
        elif p.path.startswith('/shorts/') or p.path.startswith('/live/'):
            parts = p.path.strip('/').split('/')
            video = parts[1] if len(parts) == 2 else ''
        else:
            video = ''
    else:
        raise ValueError('Link must be a YouTube video')
    if not VIDEO_ID.fullmatch(video):
        raise ValueError('Invalid YouTube video ID (playlists are not supported)')
    return video, 'https://www.youtube.com/watch?v=' + video


def safe_folder(s: str, default='Untitled', maxlen=65) -> str:
    value = re.sub(r'[^a-zA-Z0-9 _-]', '-', (s or '')).strip(' .-_')[:maxlen]
    return value or default


class AudioPreferences(BaseModel):
    format: Literal['mp3', 'wav'] = 'mp3'
    quality: Literal[128, 192, 256] = 192
    auto_upload: bool = True
    keep_metadata: bool = False
    download_thumbnail: bool = False
    auto_song_maker: bool = False


class BatchRequest(BaseModel):
    links: list[str] = Field(min_length=1, max_length=30)
    format: Literal['mp3', 'wav'] = 'mp3'
    quality: Literal[128, 192, 256] = 192
    auto_upload: bool = True
    keep_metadata: bool = False
    download_thumbnail: bool = False
    auto_song_maker: bool = False
    permission_confirmed: bool = False


def sanitized_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data.pop('local_file', None)
    data.pop('local_thumbnail', None)
    data.pop('song_maker_owner', None)
    data['download_thumbnail'] = bool(data['download_thumbnail'])
    data['auto_song_maker'] = bool(data.get('auto_song_maker'))
    data['thumbnail_url'] = ('/tts/api/audio-downloader/jobs/' + data['job_id'] + '/thumbnail') if row['local_thumbnail'] and Path(row['local_thumbnail']).is_file() else None
    data['thumbnail_drive_url'] = data.get('thumbnail_drive_url') if data['status'] == 'delivered' else None
    data['drive_url'] = data.get('drive_url') if data.get('status') == 'delivered' else None
    data['audio_url'] = ('/tts/api/audio-downloader/jobs/' + data['job_id'] + '/audio') if row['local_file'] and Path(row['local_file']).is_file() else None
    data['auto_upload'] = bool(data['auto_upload'])
    data['keep_metadata'] = bool(data['keep_metadata'])
    data['download_percent'] = max(0, min(100, int(data.get('download_percent') or 0)))
    data['upload_percent'] = max(0, min(100, int(data.get('upload_percent') or 0)))
    return data


def deps() -> dict:
    isolated = ROOT / 'runtimes/yt-dlp-2026.10.26/bin/yt-dlp'
    selected = str(isolated) if isolated.is_file() and not os.environ.get('AUDIO_DOWNLOADER_YTDLP_BIN') else YTDLP
    ytdlp = selected if Path(selected).is_file() and os.access(selected, os.X_OK) else shutil.which('yt-dlp')
    # The production Voice venv already owns yt_dlp; do not require a second app venv.
    module_ready = importlib.util.find_spec('yt_dlp') is not None
    command = [ytdlp] if ytdlp else ([sys.executable, '-m', 'yt_dlp'] if module_ready else [])
    ffmpeg = shutil.which('ffmpeg')
    rclone = shutil.which('rclone')
    drive_cfg = Path(RCLONE_CONFIG).is_file()
    return {'ytdlp': ytdlp, 'ytdlp_command': command, 'ffmpeg': ffmpeg, 'rclone': rclone, 'drive_configured': drive_cfg, 'ready': bool(command and ffmpeg), 'upload_ready': bool(rclone and drive_cfg), 'progress_streaming': True}


@router.get('/status')
def status():
    init_storage()
    d = deps()
    with db() as con:
        q = con.execute('SELECT status,COUNT(*) AS n FROM jobs GROUP BY status').fetchall()
    return {'ready': d['ready'], 'reason': '' if d['ready'] else 'yt-dlp or FFmpeg unavailable',
            'upload_ready': d['upload_ready'], 'drive_root': f'{DRIVE_REMOTE}:{DRIVE_BASE}',
            'formats': ['mp3', 'wav'], 'quality': [128, 192, 256],
            'download_contract': 'explicit-proxy-private-session-v2',
            'proxy': proxy_runtime_status(), 'youtube_session': cookie_status(),
            'retry_policy': 'only confirmed Drive API rate limits retry every 180s; other failures require manual Retry; 60s between successful audio transfers',
            'upload_policy': 'one copyto per audio/thumbnail; no post-upload Drive probes; local files retained',
            'upload_gap_seconds': UPLOAD_GAP_SECONDS, 'thumbnail_gap_seconds': THUMBNAIL_GAP_SECONDS,
            'upload_retry_seconds': UPLOAD_RETRY_SECONDS,
            'queue': {v['status']: v['n'] for v in q}, 'worker': bool(_worker and _worker.is_alive())}


def _preferences_owner(request: Request) -> str:
    # The authenticated reverse proxy injects this identity, as for Song Maker.
    owner = request.headers.get('x-auth-user', '').strip()
    if not owner or len(owner) > 160 or any(char in owner for char in '\\r\\n'):
        raise HTTPException(403, 'Authenticated Audio Downloader user required to save settings')
    return owner


@router.get('/preferences')
def get_preferences(request: Request):
    owner = _preferences_owner(request)
    init_storage()
    with db() as con:
        row = con.execute('SELECT * FROM audio_preferences WHERE owner=?', (owner,)).fetchone()
    if row is None:
        return {'saved': False, 'preferences': AudioPreferences().model_dump()}
    return {'saved': True, 'preferences': {
        'format': row['format'], 'quality': row['quality'],
        'auto_upload': bool(row['auto_upload']),
        'keep_metadata': bool(row['keep_metadata']),
        'download_thumbnail': bool(row['download_thumbnail']),
        'auto_song_maker': bool(row['auto_song_maker'])}}


@router.put('/preferences')
def save_preferences(payload: AudioPreferences, request: Request):
    owner = _preferences_owner(request)
    if payload.auto_song_maker and (payload.auto_upload or not payload.download_thumbnail):
        raise HTTPException(422, 'Song Maker auto-queue requires thumbnail ON and Drive auto-upload OFF')
    init_storage()
    with db() as con:
        con.execute('''INSERT INTO audio_preferences
            (owner,format,quality,auto_upload,keep_metadata,download_thumbnail,auto_song_maker,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(owner) DO UPDATE SET
            format=excluded.format,quality=excluded.quality,
            auto_upload=excluded.auto_upload,keep_metadata=excluded.keep_metadata,
            download_thumbnail=excluded.download_thumbnail,
            auto_song_maker=excluded.auto_song_maker,updated_at=excluded.updated_at''',
            (owner, payload.format, payload.quality, int(payload.auto_upload),
             int(payload.keep_metadata), int(payload.download_thumbnail),
             int(payload.auto_song_maker), utc()))
    return {'saved': True, 'preferences': payload.model_dump()}


@router.post('/jobs', status_code=201)
def enqueue(payload: BatchRequest, request: Request = None):
    if not payload.permission_confirmed:
        raise HTTPException(422, 'Confirm you have permission to download the content')
    if not deps()['ready']:
        raise HTTPException(503, 'Downloader unavailable: yt-dlp or FFmpeg not installed')
    if payload.auto_song_maker and (payload.auto_upload or not payload.download_thumbnail):
        raise HTTPException(422, 'Song Maker auto-queue requires thumbnail and Drive auto-upload OFF')
    if payload.auto_upload and not deps()['upload_ready']:
        raise HTTPException(503, 'Google Drive upload is not configured')
    owner = request.headers.get('x-auth-user', '').strip() if request is not None else ''
    if payload.auto_song_maker and (not owner or len(owner) > 160 or '\\n' in owner or '\\r' in owner):
        raise HTTPException(403, 'Authenticated Song Maker user required for auto-queue')
    ids: list[tuple[str, str]] = []
    try:
        for link in payload.links:
            ids.append(video_url(link))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    init_storage()
    batch_id = 'ADB-' + uuid.uuid4().hex[:12].upper()
    created = utc()
    jobs = []
    existing_jobs = []
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            seen = set()
            for video, url in ids:
                if (video, payload.format) in seen:
                    continue
                seen.add((video, payload.format))
                existing = con.execute("""SELECT job_id FROM jobs WHERE video_id=? AND format=?
                    AND status IN ('queued','downloading','downloaded','uploading','upload_retry','delivered','completed')
                    ORDER BY created_at DESC LIMIT 1""", (video, payload.format)).fetchone()
                if existing:
                    existing_jobs.append(existing['job_id'])
                    continue
                jid = 'ADJ-' + uuid.uuid4().hex[:16].upper()
                con.execute('''INSERT INTO jobs(job_id,batch_id,video_id,url,project,folder,format,quality,
                    auto_upload,keep_metadata,download_thumbnail,auto_song_maker,song_maker_owner,song_maker_status,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (jid, batch_id, video, url, '', '', payload.format, payload.quality,
                     int(payload.auto_upload), int(payload.keep_metadata), int(payload.download_thumbnail),
                     int(payload.auto_song_maker), owner if payload.auto_song_maker else None,
                     'pending' if payload.auto_song_maker else 'off', 'queued', created, created))
                jobs.append(jid)
            con.execute('COMMIT')
        except BaseException:
            con.execute('ROLLBACK')
            raise
    return {'batch_id': batch_id, 'created': len(jobs), 'job_ids': jobs,
            'skipped_duplicates': len(ids) - len(jobs), 'existing_job_ids': existing_jobs}


@router.get('/jobs')
def list_jobs(limit: int = 100):
    init_storage()
    limit = max(1, min(limit, 200))
    with db() as con:
        rows = con.execute('SELECT * FROM jobs ORDER BY created_at DESC,job_id DESC LIMIT ?', (limit,)).fetchall()
    return {'jobs': [sanitized_row(row) for row in rows]}


def row_for(jid: str):
    if not JOB_ID.fullmatch(jid):
        raise HTTPException(404, 'Unknown audio job')
    with db() as con:
        row = con.execute('SELECT * FROM jobs WHERE job_id=?', (jid,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Unknown audio job')
    return row


@router.get('/jobs/{job_id}')
def get_job(job_id: str):
    init_storage()
    return sanitized_row(row_for(job_id))


@router.post('/jobs/{job_id}/{action}')
def job_action(job_id: str, action: Literal['cancel', 'retry']):
    init_storage()
    row = row_for(job_id)
    with db() as con:
        if action == 'cancel' and row['status'] in ('queued', 'upload_retry'):
            con.execute("UPDATE jobs SET status='cancelled',next_retry_at=NULL,updated_at=? WHERE job_id=? AND status IN ('queued','upload_retry')", (utc(), job_id))
        elif action == 'retry' and row['status'] in ('failed', 'cancelled'):
            local = Path(row['local_file']) if row['local_file'] else None
            expected = (JOBS / job_id / (row['video_id'] + '.' + row['format'])).resolve()
            resume = bool(row['auto_upload'] and local and local.resolve() == expected and local.is_file())
            if resume:
                con.execute("UPDATE jobs SET status='upload_retry',error=NULL,next_retry_at=?,updated_at=?,progress_stage='manual retry requested' WHERE job_id=? AND status IN ('failed','cancelled')", (utc(), utc(), job_id))
            else:
                con.execute("UPDATE jobs SET status='queued',error=NULL,next_retry_at=NULL,upload_retry_count=0,started_at=NULL,completed_at=NULL,updated_at=?,download_percent=0,upload_percent=0,progress_stage=NULL WHERE job_id=? AND status IN ('failed','cancelled')", (utc(), job_id))
        else:
            raise HTTPException(409, 'Action not available for this job status')
    return {'job_id': job_id, 'action': action, 'ok': True}


@router.get('/jobs/{job_id}/audio')
def download_audio(job_id: str):
    init_storage()
    row = row_for(job_id)
    local = row['local_file']
    if not local:
        raise HTTPException(404, 'Audio not ready')
    f = Path(local).resolve()
    expected = (JOBS / job_id).resolve()
    if expected not in f.parents or not f.is_file():
        raise HTTPException(404, 'Audio not found')
    return FileResponse(str(f), media_type='audio/mpeg' if f.suffix == '.mp3' else 'audio/wav', filename=f.name)


@router.get('/jobs/{job_id}/thumbnail')
def download_thumbnail(job_id: str):
    init_storage()
    row = row_for(job_id)
    local = row['local_thumbnail']
    if not local or not row['download_thumbnail']:
        raise HTTPException(404, 'Thumbnail not ready')
    image = Path(local).resolve()
    expected = (JOBS / job_id).resolve()
    if expected not in image.parents or image.name != row['video_id'] + '.jpg' or not image.is_file():
        raise HTTPException(404, 'Thumbnail not found')
    return FileResponse(str(image), media_type='image/jpeg', filename=image.name)


def _update(jid: str, new_state: str, **kwargs):
    columns = ['status=?', 'updated_at=?']
    values: list = [new_state, utc()]
    for key in ('title', 'local_file', 'local_thumbnail', 'drive_path', 'drive_url', 'thumbnail_drive_path', 'thumbnail_drive_url', 'error', 'started_at', 'completed_at', 'next_retry_at', 'upload_retry_count', 'download_percent', 'upload_percent', 'progress_stage', 'thumbnail_upload_after'):
        if key in kwargs:
            columns.append(key + '=?')
            values.append(kwargs[key])
    values.append(jid)
    with db() as con:
        con.execute(f'UPDATE jobs SET {", ".join(columns)} WHERE job_id=?', values)


def _claim():
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        # Due uploads get priority over fresh YouTube downloads.
        row = con.execute("""SELECT * FROM jobs
            WHERE (status='upload_retry' AND next_retry_at<=?
                   AND (progress_stage='manual retry requested'
                        OR error GLOB 'Google Drive upload failed (RATE_LIMITED);*'))
               OR status='queued'
            ORDER BY CASE WHEN status='upload_retry' THEN 0 ELSE 1 END,
                CASE WHEN status='upload_retry' THEN next_retry_at ELSE created_at END,job_id
            LIMIT 1""", (utc(),)).fetchone()
        if not row:
            con.execute('COMMIT')
            return None
        resume = row['status'] == 'upload_retry'
        new_state = 'uploading' if resume else 'downloading'
        con.execute("UPDATE jobs SET status=?,started_at=?,updated_at=? WHERE job_id=?",
                    (new_state, utc(), utc(), row['job_id']))
        con.execute('COMMIT')
    job = dict(row)
    job['_resume_upload'] = resume
    return job


def verified_drive_public_link(remote: str, local_path=None, expected_format='mp3') -> str:
    """Check stored Drive file and anonymous download bytes before allowing local deletion."""
    import hashlib
    import urllib.parse
    import urllib.request
    import urllib.error
    if not isinstance(remote, str) or not remote.startswith(DRIVE_REMOTE + ':' + DRIVE_BASE + '/'):
        raise RuntimeError('Unexpected Google Drive path')
    if any(x in remote for x in ('..', '\\', '\n', '\r')):
        raise RuntimeError('Unsafe Google Drive path')
    tool = deps()['rclone']
    if not tool:
        raise RuntimeError('Google Drive link tool unavailable')
    cmd = [tool, '--config', RCLONE_CONFIG]
    probe = subprocess.run(cmd + ['lsjson', '--stat', '--hash', remote], capture_output=True, text=True, timeout=120, check=False)
    if probe.returncode:
        if _drive_copy_failure_code(probe.stderr or probe.stdout or '') == 'RATE_LIMITED':
            raise DriveUploadFailure('RATE_LIMITED')
        raise RuntimeError('Google Drive size/hash verification unavailable')
    try:
        stat = json.loads(probe.stdout)
        size = int(stat['Size'])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('Google Drive remote stat invalid') from exc
    if size < 1024:
        raise RuntimeError('Google Drive file unexpectedly small')
    if local_path is not None:
        local = Path(local_path)
        if not local.is_file() or local.stat().st_size != size:
            raise RuntimeError('Local and Drive file sizes differ')
        remote_md5 = (stat.get('Hashes') or {}).get('MD5', '').lower()
        if not remote_md5:
            hashes = subprocess.run(cmd + ['md5sum', remote], capture_output=True, text=True, timeout=120, check=False)
            if hashes.returncode:
                if _drive_copy_failure_code(hashes.stderr or hashes.stdout or '') == 'RATE_LIMITED':
                    raise DriveUploadFailure('RATE_LIMITED')
                raise RuntimeError('Google Drive MD5 unavailable, keep local file')
            remote_md5 = hashes.stdout.strip().split()[0].lower()
        local_md5 = hashlib.md5()
        with local.open('rb') as inp:
            for piece in iter(lambda: inp.read(1<<20), b''):
                local_md5.update(piece)
        if local_md5.hexdigest() != remote_md5:
            raise RuntimeError('Google Drive content hash mismatch, keep local file')
    link = subprocess.run(cmd + ['link', remote], capture_output=True, text=True, timeout=120, check=False)
    if link.returncode:
        if _drive_copy_failure_code(link.stderr or link.stdout or '') == 'RATE_LIMITED':
            raise DriveUploadFailure('RATE_LIMITED')
        raise RuntimeError('Google Drive anyone-with-link permission failed')
    provided = (link.stdout.strip().splitlines() or [''])[-1].strip()
    parts = urllib.parse.urlsplit(provided)
    if parts.scheme != 'https' or parts.hostname != 'drive.google.com':
        raise RuntimeError('Unexpected Google Drive share link')
    match = re.fullmatch(r'/file/d/([a-zA-Z0-9_-]+)/view/?', parts.path)
    file_id = (match.group(1) if match else urllib.parse.parse_qs(parts.query).get('id', [''])[0])
    if not re.fullmatch(r'[a-zA-Z0-9_-]{12,}', file_id):
        raise RuntimeError('Google Drive share link missing file ID')
    canonical = 'https://drive.google.com/file/d/' + file_id + '/view?usp=sharing'
    # Important: no OAuth/cookies here. An anonymous response must actually be binary audio.
    test_url = 'https://drive.google.com/uc?export=download&id=' + urllib.parse.quote(file_id)
    req = urllib.request.Request(test_url, headers={'Range': 'bytes=0-1023', 'User-Agent':'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            mime = resp.headers.get('Content-Type','').split(';')[0].lower().strip()
            header = resp.read(64)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise DriveUploadFailure('RATE_LIMITED') from None
        raise RuntimeError('Google Drive anonymous audio download not verified') from None
    except Exception as exc:
        raise RuntimeError('Google Drive anonymous audio download not verified') from exc
    audio_magic = header.startswith(b'ID3') or (header[:1] == b'\xff' and len(header)>1 and (header[1] & 0xe0)==0xe0)
    wav_magic = header.startswith(b'RIFF')
    jpg_magic = header.startswith(b'\xff\xd8\xff')
    accepted = {'mp3': ('audio/mpeg', 'audio/mp3', 'application/octet-stream'),
                'wav': ('audio/wav', 'audio/x-wav', 'application/octet-stream'),
                'jpg': ('image/jpeg', 'image/jpg', 'application/octet-stream')}
    if expected_format not in accepted or mime not in accepted[expected_format]:
        raise RuntimeError('Drive anonymous access returned unexpected content type')
    if not {'mp3': audio_magic, 'wav': wav_magic, 'jpg': jpg_magic}[expected_format]:
        raise RuntimeError('Drive anonymous response did not contain expected file bytes')
    return canonical


def _run_tracked(command: list[str], *, jid: str, phase: str, cwd=None, env=None, timeout: int = 1500):
    """Stream sanitized percent only. No raw provider output or credentials enter SQLite."""
    pattern = (re.compile(r'AD_PROGRESS:\s*(\d+(?:\.\d+)?)%') if phase == 'download'
               else re.compile(r'Transferred:.*?(\d+(?:\.\d+)?)%'))
    collected: list[str] = []
    highest = -1
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, bufsize=1, cwd=cwd, env=env) as proc:
        def consume():
            nonlocal highest
            for line in proc.stdout:
                collected.append(line)
                if len(collected) > 250:
                    del collected[:len(collected) - 250]
                match = pattern.search(line)
                if not match:
                    continue
                percent = max(0, min(99, int(float(match.group(1)))))
                if percent > highest:
                    highest = percent
                    _update(jid, 'downloading' if phase == 'download' else 'uploading',
                            **{'download_percent' if phase == 'download' else 'upload_percent': percent})
        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        finally:
            reader.join(timeout=10)
        output = ''.join(collected)
        return subprocess.CompletedProcess(command, proc.returncode, output, output)


def _wait_for_audio_upload_slot() -> None:
    """Persisted across restarts; the next audio starts after the previous verified upload."""
    while True:
        with db() as con:
            row = con.execute('SELECT next_upload_at FROM audio_upload_pacing WHERE id=1').fetchone()
        if not row:
            return
        remaining = (datetime.fromisoformat(row['next_upload_at']) - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(3, remaining))


def _record_audio_upload() -> None:
    with db() as con:
        con.execute("""INSERT INTO audio_upload_pacing(id,next_upload_at) VALUES(1,?)
            ON CONFLICT(id) DO UPDATE SET next_upload_at=excluded.next_upload_at""",
            (utc_after(UPLOAD_GAP_SECONDS),))



def _wait_for_thumbnail_upload(jid: str) -> None:
    """Persisted 30s gap after this job's successful audio transfer.

    Waiting is local only: it performs no Google Drive API requests.
    Read the durable timestamp each loop so restart/retry cannot bypass it.
    """
    while True:
        with db() as con:
            row = con.execute('SELECT thumbnail_upload_after FROM jobs WHERE job_id=?',
                              (jid,)).fetchone()
        if not row or not row['thumbnail_upload_after']:
            raise RuntimeError('Thumbnail upload schedule missing; preserve local media')
        remaining = (datetime.fromisoformat(row['thumbnail_upload_after']) -
                     datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(3, remaining))


def _schedule_upload_retry(jid: str, error: Exception) -> None:
    # Auto-retry ONLY confirmed provider API rate limits; all other failures are manual.
    # In either case the downloaded MP3 and its immutable Drive receipts remain intact.
    with db() as con:
        row = con.execute('SELECT * FROM jobs WHERE job_id=?', (jid,)).fetchone()
    if row and row['status'] in ('uploading', 'downloaded'):
        local = Path(row['local_file']) if row['local_file'] else None
        expected = (JOBS / jid / (row['video_id'] + '.' + row['format'])).resolve()
        if (isinstance(error, DriveUploadFailure) and error.code == 'RATE_LIMITED'
                and local and local.resolve() == expected and local.is_file()):
            _update(jid, 'upload_retry', error=str(error)[:420],
                    next_retry_at=utc_after(UPLOAD_RETRY_SECONDS),
                    upload_retry_count=int(row['upload_retry_count']) + 1,
                    progress_stage='retry scheduled')
            return
    _update(jid, 'failed', error=_safe_proxy_error(str(error))[:420],
            next_retry_at=None, progress_stage='download failed' if isinstance(error, DownloadFailure) else 'manual retry required')


def resume_upload(job: dict) -> None:
    """Retry the saved local file and any missing thumbnail without yt-dlp."""
    jid = job['job_id']
    local = JOBS / jid / (job['video_id'] + '.' + job['format'])
    if not job.get('local_file') or Path(job['local_file']).resolve() != local.resolve() or not local.is_file():
        raise RuntimeError('Downloaded audio no longer available; manual retry required')
    thumbnail = JOBS / jid / (job['video_id'] + '.jpg') if job.get('download_thumbnail') else None
    if thumbnail and (not job.get('local_thumbnail') or
                      Path(job['local_thumbnail']).resolve() != thumbnail.resolve() or not thumbnail.is_file()):
        raise RuntimeError('Downloaded thumbnail no longer available; manual retry required')
    upload_downloaded(job, local, thumbnail, deps())


def run_job(job: dict):
    jid = job['job_id']
    video_id = job['video_id']
    if not VIDEO_ID.fullmatch(video_id):
        raise RuntimeError('Invalid YouTube video ID in queued job')
    folder = JOBS / jid
    folder.mkdir(mode=0o750, parents=True, exist_ok=True)
    state = deps()
    command = state.get('ytdlp_command') or ([state['ytdlp']] if state.get('ytdlp') else [])
    if not command or not state['ffmpeg']:
        raise RuntimeError('yt-dlp/FFmpeg unavailable')
    audio_format = job['format']
    want_thumbnail = bool(job.get('download_thumbnail', False))
    out_template = str(folder / '%(title).70s [%(id)s].%(ext)s')
    args = command + ['--no-playlist', '--no-warnings', '--no-cache-dir',
            '--no-overwrites', '--restrict-filenames', '--max-filesize', '120M',
            '--format', 'bestaudio[ext=m4a]/bestaudio/best', '--ffmpeg-location', state['ffmpeg'],
            '--extract-audio', '--audio-format', audio_format, '--audio-quality', str(job['quality']) + 'K',
            '--print', 'after_move:filepath', '-o', out_template]
    if state.get('progress_streaming'):
        args.extend(['--newline', '--progress', '--progress-template',
                     'download:AD_PROGRESS:%(progress._percent_str)s'])
    if want_thumbnail:
        args.extend(['--write-thumbnail', '--convert-thumbnails', 'jpg'])
    if job['keep_metadata']:
        args.append('--embed-metadata')
    args.append(job['url'])
    # Use the same private proxy/session snapshot for audio and thumbnail extraction.
    with youtube_execution() as (options, environment):
        try:
            full_command = command + options + args[len(command):]
            if state.get('progress_streaming'):
                p = _run_tracked(full_command, jid=jid, phase='download', timeout=1800,
                                 cwd=str(folder), env=environment)
            else:
                p = subprocess.run(full_command, capture_output=True,
                                   text=True, timeout=1800, cwd=str(folder), check=False, env=environment)
        except subprocess.TimeoutExpired:
            raise DownloadFailure('DOWNLOAD_TIMEOUT', 'YouTube download timed out; retry manually after checking the provider.') from None
        if p.returncode:
            raise download_failure(p.stderr or p.stdout or '')
    paths = [Path(x.strip()) for x in (p.stdout or '').splitlines() if x.strip()]
    paths = [x for x in paths if x.is_file() and x.suffix.lower() == '.' + audio_format and folder.resolve() in x.resolve().parents]
    if not paths:
        paths = sorted(folder.glob('*.' + audio_format))
    if len(paths) != 1:
        raise RuntimeError('Expected exactly one downloaded audio file')
    original = paths[0]
    safe = re.sub(r'[^a-zA-Z0-9._ -]', '_', original.stem).strip(' ._')[:105] or video_id
    final = folder / f'{video_id}.{audio_format}'
    if original != final:
        original.rename(final)
    if final.stat().st_size <= 1000 or final.stat().st_size > 150 * 1024 * 1024:
        raise RuntimeError('Unexpected downloaded audio size')

    thumbnail = None
    if want_thumbnail:
        images = sorted(p for p in folder.glob('*.jpg') if p.is_file())
        if len(images) != 1:
            raise RuntimeError('YouTube thumbnail unavailable; audio retained for retry')
        thumbnail = folder / f'{video_id}.jpg'
        if images[0] != thumbnail:
            images[0].rename(thumbnail)
        if not 1024 < thumbnail.stat().st_size <= 20 * 1024 * 1024:
            raise RuntimeError('Unexpected downloaded thumbnail size')
        with thumbnail.open('rb') as handle:
            if handle.read(3) != b'\xff\xd8\xff':
                raise RuntimeError('Downloaded thumbnail is not JPEG')

    _update(jid, 'downloaded', title=safe, download_percent=100, progress_stage='downloaded', local_file=str(final),
            local_thumbnail=str(thumbnail) if thumbnail else None)
    if not job['auto_upload']:
        _update(jid, 'completed', completed_at=utc())
        return
    upload_downloaded(job, final, thumbnail, state)


def _drive_copy_failure_code(output: str) -> str:
    """Return a stable, non-sensitive reason; never expose raw rclone output in job state."""
    detail = (output or '').lower()
    if any(x in detail for x in ('storagequotaexceeded', 'storage quota exceeded', 'insufficient storage')):
        return 'STORAGE_FULL'
    if any(x in detail for x in ('ratelimitexceeded', 'user rate limit', 'rate limit', 'too many requests',
                                   'dailylimitexceeded', 'daily limit', 'http 429', ' 429 ')):
        return 'RATE_LIMITED'
    if any(x in detail for x in ('invalid_grant', 'invalid credentials', 'token has been expired',
                                   'token expired', 'oauth2: cannot fetch token', 'unauthorized', 'http 401')):
        return 'AUTH_REQUIRED'
    if any(x in detail for x in ('immutable', 'already exists', 'file exists', 'destination exists',
                                   'cannot overwrite', "can't overwrite")):
        return 'DESTINATION_EXISTS'
    if any(x in detail for x in ('insufficientfilepermissions', 'permission denied', 'insufficient permissions',
                                   'access denied', 'http 403', 'forbidden')):
        return 'PERMISSION_DENIED'
    if any(x in detail for x in ('timeout', 'timed out', 'connection reset', 'connection refused',
                                   'no such host', 'temporary failure', 'network unreachable')):
        return 'NETWORK_FAILURE'
    return 'TRANSFER_FAILED'


def upload_downloaded(job: dict, final: Path, thumbnail: Path | None, state: dict) -> None:
    jid = job['job_id']
    if not state.get('upload_ready'):
        raise RuntimeError('Google Drive connection unavailable')
    _update(jid, 'uploading', progress_stage='uploading audio')

    # Existing flat destination, immutable filename, and QA nonce segregation.
    base = f'{DRIVE_REMOTE}:{DRIVE_BASE}/'
    if job.get('folder') == '__Audio_Proxy_QA__' and re.fullmatch(r'[A-F0-9]{16}', job.get('project', '')):
        base += safe_folder(job['folder']) + '/' + safe_folder(job['project']) + '/'
    remote = base + final.name

    def upload_file(source: Path, destination: str, fmt: str) -> None:
        """Only one explicit rclone transfer per file; do not probe Drive afterwards.

        rclone may perform multiple underlying Google API calls itself. A zero
        exit status means the transfer command succeeded, NOT that remote MD5,
        public permissions, or anonymous downloads have been verified.
        """
        if not source.is_file() or source.stat().st_size < 1024:
            raise DriveUploadFailure('LOCAL_FILE_UNAVAILABLE')
        cmd = [state['rclone'], '--config', RCLONE_CONFIG, 'copyto', str(source), destination,
               '--immutable', '--retries', '1', '--low-level-retries', '1', '--timeout', '5m',
               '--contimeout', '30s']
        try:
            if state.get('progress_streaming'):
                cmd.extend(['--stats', '2s', '--stats-one-line', '--stats-log-level', 'NOTICE'])
                _update(jid, 'uploading', upload_percent=0,
                        progress_stage='uploading thumbnail' if fmt == 'jpg' else 'uploading audio')
                result = _run_tracked(cmd, jid=jid, phase='upload', timeout=1500)
            else:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=1500, check=False)
        except subprocess.TimeoutExpired:
            raise DriveUploadFailure('NETWORK_TIMEOUT') from None
        if result.returncode:
            code = _drive_copy_failure_code(result.stderr or result.stdout or '')
            # Without remote probes, an immutable conflict cannot be declared
            # successful. Preserve both local assets for manual reconciliation.
            if code == 'DESTINATION_EXISTS':
                raise DriveUploadFailure('DESTINATION_CONFLICT_UNVERIFIED')
            raise DriveUploadFailure(code)
        _update(jid, 'uploading', upload_percent=100,
                progress_stage='uploaded thumbnail (not independently verified)'
                if fmt == 'jpg' else 'uploaded audio (not independently verified)')

    # Persist the successful copyto receipt before attempting the thumbnail.
    # It prevents the next retry from sending the audio a second time.
    if job.get('drive_path') == remote:
        public_url = job.get('drive_url')
    else:
        _wait_for_audio_upload_slot()
        upload_file(final, remote, job['format'])
        _record_audio_upload()
        public_url = None  # No extra Drive permission/link request.
        _update(jid, 'uploading', drive_path=remote, drive_url=None,
                thumbnail_upload_after=utc_after(THUMBNAIL_GAP_SECONDS) if thumbnail else None)
    thumb_remote = base + thumbnail.name if thumbnail else None
    if thumbnail and job.get('thumbnail_drive_path') == thumb_remote:
        thumb_public_url = job.get('thumbnail_drive_url')
    else:
        if thumbnail:
            # Prior-version partial uploads may have no due timestamp. On first
            # resume, set a conservative 30s schedule rather than send both at once.
            with db() as con:
                pending = con.execute('SELECT thumbnail_upload_after FROM jobs WHERE job_id=?',
                                      (jid,)).fetchone()
            if not pending or not pending['thumbnail_upload_after']:
                _update(jid, 'uploading', thumbnail_upload_after=utc_after(THUMBNAIL_GAP_SECONDS))
            _update(jid, 'uploading', progress_stage='waiting 30s after audio before thumbnail')
            _wait_for_thumbnail_upload(jid)
            upload_file(thumbnail, thumb_remote, 'jpg')
            # The receipt is persisted only after a successful copyto command.
            _update(jid, 'uploading', thumbnail_drive_path=thumb_remote, thumbnail_drive_url=None,
                    thumbnail_upload_after=None)
        thumb_public_url = None
    _update(jid, 'delivered', drive_path=remote, drive_url=public_url,
            thumbnail_drive_path=thumb_remote, thumbnail_drive_url=thumb_public_url,
            completed_at=utc(), next_retry_at=None, error=None,
            download_percent=100, upload_percent=100,
            progress_stage='uploaded (not independently verified)')
    # No independent Drive check has run. Keep local audio/thumbnail as the
    # recoverable originals; never delete them based only on copyto exit 0.


def _post_local_pair_to_song_maker(row: sqlite3.Row) -> str:
    """Stream the saved local audio + thumbnail into Song Maker's existing API.

    No Google Drive operations occur for the source pair. Streaming avoids
    buffering up to 150 MB of audio in the Audio Downloader worker memory.
    """
    jid, video, fmt = row['job_id'], row['video_id'], row['format']
    if not JOB_ID.fullmatch(jid) or not VIDEO_ID.fullmatch(video) or fmt not in ('mp3', 'wav'):
        raise ValueError('Unsafe Song Maker source identity')
    folder = (JOBS / jid).resolve()
    audio, image = folder / (video + '.' + fmt), folder / (video + '.jpg')
    if (not row['local_file'] or Path(row['local_file']).resolve() != audio.resolve()
            or not row['local_thumbnail'] or Path(row['local_thumbnail']).resolve() != image.resolve()
            or folder not in audio.resolve().parents or folder not in image.resolve().parents):
        raise ValueError('Unexpected local Song Maker input path')
    if (not audio.is_file() or not image.is_file()
            or not 1024 < audio.stat().st_size <= 150 * 1024 * 1024
            or not 1024 < image.stat().st_size <= 20 * 1024 * 1024):
        raise ValueError('Local Song Maker media missing or invalid')
    owner = row['song_maker_owner']
    if not owner or any(c in owner for c in '\r\n') or len(owner) > 160:
        raise ValueError('Invalid Song Maker owner')
    boundary = 'audio-song-' + uuid.uuid4().hex
    parts = []
    for key, name, mime, path in (
        ('image', image.name, 'image/jpeg', image),
        ('audio', audio.name, 'audio/mpeg' if fmt == 'mp3' else 'audio/wav', audio),
    ):
        prefix = (f'--{boundary}\r\n'
                  f'Content-Disposition: form-data; name="{key}"; filename="{name}"\r\n'
                  f'Content-Type: {mime}\r\n\r\n').encode('ascii')
        parts.append((prefix, path))
    ending = f'--{boundary}--\r\n'.encode('ascii')
    length = sum(len(prefix) + path.stat().st_size + 2 for prefix, path in parts) + len(ending)
    connection = http.client.HTTPConnection(SONG_MAKER_HOST, SONG_MAKER_PORT, timeout=180)
    try:
        connection.putrequest('POST', SONG_MAKER_INGEST_PATH)
        connection.putheader('Content-Type', 'multipart/form-data; boundary=' + boundary)
        connection.putheader('Content-Length', str(length))
        connection.putheader('X-Auth-User', owner)
        connection.putheader('X-Song-Maker', '1')
        connection.putheader('X-Audio-Downloader-Handoff', '1')
        connection.endheaders()
        for prefix, path in parts:
            connection.send(prefix)
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    connection.send(chunk)
            connection.send(b'\r\n')
        connection.send(ending)
        response = connection.getresponse()
        payload = response.read(32768)
        if response.status not in (200, 201):
            raise RuntimeError('Song Maker queue admission unavailable')
        receipt = json.loads(payload)
        song_id = receipt.get('id')
        if not isinstance(song_id, str) or not re.fullmatch(r'[0-9a-f]{32}', song_id):
            raise ValueError('Song Maker queue acknowledgement missing')
        return song_id
    finally:
        connection.close()


def _handoff_song_maker(jid: str) -> None:
    """Queue local pair exactly once; retry admission without YouTube or Drive."""
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM jobs WHERE job_id=?', (jid,)).fetchone()
        if (not row or row['status'] != 'completed' or not row['auto_song_maker']
                or row['auto_upload'] or not row['download_thumbnail']
                or row['song_maker_status'] == 'queued'
                or not row['song_maker_owner'] or not row['local_thumbnail'] or not row['local_file']):
            con.execute('COMMIT')
            return
        due = row['song_maker_next_retry_at']
        if due and datetime.fromisoformat(due) > datetime.now(timezone.utc):
            con.execute('COMMIT')
            return
        con.execute("""UPDATE jobs SET song_maker_status='sending',song_maker_next_retry_at=?
                       WHERE job_id=?""", (utc_after(SONG_MAKER_RETRY_SECONDS), jid))
        con.execute('COMMIT')
    try:
        song_id = _post_local_pair_to_song_maker(row)
    except Exception:
        with db() as con:
            con.execute("""UPDATE jobs SET song_maker_status='pending'
                           WHERE job_id=? AND song_maker_status='sending'""", (jid,))
        return
    with db() as con:
        con.execute("""UPDATE jobs SET song_maker_status='queued',song_maker_job_id=?,
                       song_maker_next_retry_at=NULL WHERE job_id=?""", (song_id, jid))


def _due_song_maker_handoff() -> str | None:
    with db() as con:
        row = con.execute("""SELECT job_id FROM jobs WHERE status='completed'
            AND auto_song_maker=1 AND auto_upload=0
            AND song_maker_status IN ('pending','sending')
            AND (song_maker_next_retry_at IS NULL OR song_maker_next_retry_at<=?)
            ORDER BY completed_at,job_id LIMIT 1""", (utc(),)).fetchone()
    return row['job_id'] if row else None


def worker_loop():
    while True:
        try:
            item = _claim()
            if item:
                try:
                    if item.get('_resume_upload'):
                        resume_upload(item)
                    else:
                        run_job(item)
                    _handoff_song_maker(item['job_id'])
                except Exception as exc:
                    _schedule_upload_retry(item['job_id'], exc)
            else:
                due = _due_song_maker_handoff()
                if due:
                    _handoff_song_maker(due)
                else:
                    time.sleep(3)
        except Exception:
            time.sleep(5)


def start_worker():
    global _worker
    init_storage()
    if _worker is None or not _worker.is_alive():
        recover_interrupted_jobs()
        _bootstrap_audio_proxy_config()
        _worker = threading.Thread(target=worker_loop, name='voice-studio-audio-downloader', daemon=True)
        _worker.start()


# AUDIO_DOWNLOADER_PROXY_V1 — private proxy profiles, copied once from Video Merger.
# The original Video Merger service, settings and DB are never written to.
import ipaddress
import socket
import tempfile
import urllib.request
import urllib.parse

_PROXY_PATH = DATA / 'proxy_settings.json'
_PROXY_LOCK = threading.RLock()
_VIDEO_PROXY_CONFIG = None  # Test override only; production discovers its path from the Video Merger source.


def _proxy_blank():
    return {'enabled': False, 'active_id': '', 'profiles': [], 'origin': 'audio-downloader'}


def _resolve_merger_proxy_path():
    # Resolve the Video Merger-owned JSON path without executing its application.
    if _VIDEO_PROXY_CONFIG is not None:
        return Path(_VIDEO_PROXY_CONFIG)
    import ast
    source = Path('/opt/video-merger/app.py')
    if not source.is_file():
        return None
    tree = ast.parse(source.read_text(encoding='utf-8'))
    # Evaluate only literal path construction, never import or execute Video Merger.
    known = {
        'BASE': Path('/opt/video-merger'),
        'DATA': Path('/opt/video-merger/data'),
        'PROXY_CONFIG_DIR': Path('/opt/video-merger/data'),
    }
    def static(expr):
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value
        if isinstance(expr, ast.Name) and expr.id in known:
            return known[expr.id]
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
            return Path(static(expr.left)) / str(static(expr.right))
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == 'Path' and len(expr.args)==1:
            return Path(str(static(expr.args[0])))
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr in ('resolve','expanduser') and not expr.args:
            p=Path(static(expr.func.value))
            return p.expanduser() if expr.func.attr=='expanduser' else p.resolve()
        raise ValueError('Unrecognized Video Merger proxy config path syntax')
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ('BASE','DATA','PROXY_CONFIG_DIR','PROXY_CONFIG_PATH'):
                    try:
                        known[target.id]=Path(static(node.value))
                    except (ValueError, TypeError, OSError):
                        continue
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in ('PROXY_CONFIG_DIR','PROXY_CONFIG_PATH'):
            try: known[node.target.id]=Path(static(node.value))
            except (ValueError, TypeError,OSError): pass
    p = known.get('PROXY_CONFIG_PATH')
    if p is None:
        return None
    p=p.resolve()
    # Never follow a source-specified path outside known Video Merger-owned configuration roots.
    authorized = (Path('/opt/video-merger').resolve(),Path('/home/ubuntu/.config/video-merger').resolve(),Path('/home/ubuntu/.config/video-merger-proxy').resolve())
    if not any(p == root or root in p.parents for root in authorized):
        return None
    return p


def _import_merger_config():
    # Read existing Video Merger JSON in memory; never write to the source.
    path=_resolve_merger_proxy_path()
    if not path or not path.is_file():
        return None
    obj=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(obj,dict):
        return None
    raw=obj.get('profiles')
    if not isinstance(raw,list):
        return None
    profiles=[]
    for item in raw[:16]:
        if not isinstance(item,dict):
            continue
        host=str(item.get('host') or '').strip()
        try:port=int(item.get('port') or 0)
        except (TypeError,ValueError):continue
        if not _safe_proxy_host(host) or not 1 <= port <= 65535:
            continue
        profiles.append({
           'id':str(item.get('id') or uuid.uuid4().hex[:12]),
           'name':str(item.get('name') or 'Imported proxy')[:70],
           'host':host,'port':port,
           'username':str(item.get('username') or ''),
           'password':str(item.get('password') or ''),
        })
    if not profiles:
        return None
    active=str(obj.get('active_id') or '')
    if active not in {p['id'] for p in profiles}:
        active=profiles[0]['id']
    enabled=obj.get('enabled',True)
    if isinstance(enabled,str):enabled=enabled.lower() in ('true','yes','on','1')
    return {'enabled':bool(enabled),'active_id':active,'profiles':profiles,
            'origin':'copied-once-from-video-merger'}

def _safe_proxy_host(host: str) -> bool:
    if not host or len(host) > 253 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
        return False
    if host.lower() in ('localhost', 'localhost.localdomain') or host.endswith('.local'):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        return not (host.endswith('.') or host.startswith('127.') or '..' in host)


def _write_proxy_config(cfg):
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.stat(DATA).st_uid == os.geteuid():
        os.chmod(DATA, 0o700)
    f = None
    try:
        # Never print payload, path-based secrets, or URLs in logs.
        fd, f = tempfile.mkstemp(prefix='.audio-proxy-', dir=str(DATA))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(cfg, out, ensure_ascii=True)
        os.replace(f, _PROXY_PATH)
        f = None
        os.chmod(_PROXY_PATH, 0o600)
    finally:
        if f:
            try: os.unlink(f)
            except OSError: pass


def _bootstrap_audio_proxy_config():
    with _PROXY_LOCK:
        if _PROXY_PATH.is_file():
            return
        try:
            cfg = _import_merger_config() or _proxy_blank()
        except (OSError, sqlite3.Error, ValueError):
            cfg = _proxy_blank()
        _write_proxy_config(cfg)


def _read_proxy_config():
    with _PROXY_LOCK:
        if not _PROXY_PATH.is_file():
            _bootstrap_audio_proxy_config()
        try:
            return json.loads(_PROXY_PATH.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise DownloadFailure('PROXY_CONFIGURATION', 'Saved proxy configuration is unreadable; refusing download until repaired.') from None


def _public_proxy_config(cfg):
    return {'enabled': bool(cfg.get('enabled')), 'active_id': cfg.get('active_id') or '',
            'origin': cfg.get('origin', 'audio-downloader'),
            'profiles': [{k:v for k,v in p.items() if k != 'password'} |
                         {'password_saved': bool(p.get('password'))} for p in cfg.get('profiles', [])]}


def _selected_proxy(cfg=None):
    cfg = cfg or _read_proxy_config()
    if not cfg.get('enabled'):
        return None
    return next((p for p in cfg.get('profiles', []) if p.get('id') == cfg.get('active_id')), None)


def _proxy_url(profile):
    try:
        if not _safe_proxy_host(profile['host']) or not 1 <= int(profile['port']) <= 65535:
            raise ValueError('invalid proxy')
        credentials = ''
        if profile.get('username'):
            credentials = urllib.parse.quote(profile['username'], safe='') + ':' + urllib.parse.quote(profile.get('password') or '', safe='') + '@'
        return 'http://' + credentials + profile['host'] + ':' + str(int(profile['port']))
    except (KeyError, ValueError, TypeError):
        raise DownloadFailure('PROXY_CONFIGURATION', 'Enabled proxy is invalid; select a valid profile.') from None


def proxy_runtime_status():
    cfg = _read_proxy_config()
    selected = _selected_proxy(cfg)
    return {'enabled': bool(cfg.get('enabled')), 'selected_id': cfg.get('active_id') or '',
            'worker_profile_id': selected.get('id') if selected else None,
            'valid_selection': not cfg.get('enabled') or selected is not None,
            'connectivity_proves_download': False}


def _youtube_proxy_env(profile=None):
    # Never inherit a system proxy or NO_PROXY that contradicts the app toggle.
    env = os.environ.copy()
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy','NO_PROXY','no_proxy'):
        env.pop(key, None)
    if profile:
        url = _proxy_url(profile)
        for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
            env[key] = url
    return env


class DownloadFailure(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(code + ': ' + message)


def download_failure(error):
    value = str(error).lower().replace('’', "'")
    if 'no supported javascript runtime' in value or 'javascript runtime was not found' in value:
        return DownloadFailure('JS_RUNTIME_MISSING', 'The Audio Downloader YouTube JavaScript runtime is missing; install the app-isolated Deno runtime.')
    if 'yt-dlp-ejs' in value and any(k in value for k in ('missing', 'not installed', 'not available', 'failed')):
        return DownloadFailure('EJS_COMPONENT_MISSING', 'YouTube EJS challenge solver components are unavailable in the Audio Downloader Python environment.')
    if any(x in value for x in ("not a bot", 'confirm your age', 'login required', 'sign in', 'cookies are no longer valid')):
        return DownloadFailure('YOUTUBE_AUTH_REQUIRED', 'YouTube requires an authorized session or challenge verification. Proxy connectivity does not prove download access. Check the selected proxy/direct route and downloader runtime; an authorized session may be needed for restricted content. No automatic retry.')
    if 'po token' in value or 'proof of origin' in value:
        return DownloadFailure('YOUTUBE_PROVIDER_CHALLENGE', 'YouTube requires additional provider verification. Cookies/proxy alone may not suffice; no automatic retry.')
    if '407' in value or 'proxy authentication' in value:
        return DownloadFailure('PROXY_AUTH_FAILED', 'Proxy rejected its credentials; update the selected profile before manual retry.')
    if any(x in value for x in ('proxyerror', 'unable to connect to proxy', 'tunnel connection failed')):
        return DownloadFailure('PROXY_CONNECTION_FAILED', 'Selected proxy connection failed; check the provider before manual retry.')
    if '429' in value or 'too many requests' in value:
        return DownloadFailure('YOUTUBE_RATE_LIMIT', 'YouTube rate limited this session/IP; wait before manual retry.')
    if '403' in value:
        return DownloadFailure('YOUTUBE_ACCESS_DENIED', 'YouTube denied media access (403); provider/session verification is required before retry.')
    if any(x in value for x in ('private video', 'video unavailable', 'not available', 'has been removed')):
        return DownloadFailure('YOUTUBE_UNAVAILABLE', 'This video is private, removed, restricted or unavailable.')
    return DownloadFailure('DOWNLOAD_FAILED', 'YouTube extraction failed; check the source and runtime support before manual retry.')


def _cookie_source():
    return Path(os.environ.get('AUDIO_DOWNLOADER_COOKIES_FILE', str(DATA / 'youtube-cookies.txt')))


def _cookie_bytes():
    path = _cookie_source()
    if not path.exists() and not path.is_symlink():
        if os.environ.get('AUDIO_DOWNLOADER_COOKIES_FILE'):
            raise DownloadFailure('SESSION_CONFIGURATION', 'Configured private YouTube cookie file is unavailable.')
        return None
    try:
        # Refuse shared/readable/symlink session files. Do not expose their path or contents.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 1024*1024:
                raise ValueError('private owner file required')
            data = stream.read(1024*1024+1)
        text = data.decode('utf-8')
        if not text.startswith(('# Netscape HTTP Cookie File', '# HTTP Cookie File')):
            raise ValueError('Netscape cookies required')
        # A header alone is not a session. Expired/malformed exports fail before network use.
        valid = False
        for line in text.splitlines():
            if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')):
                continue
            fields = line.removeprefix('#HttpOnly_').split('\t')
            if len(fields) != 7:
                raise ValueError('malformed cookies')
            domain = fields[0].lstrip('.')
            if domain in ('youtube.com', 'google.com') or domain.endswith('.youtube.com') or domain.endswith('.google.com'):
                expiry = int(fields[4])
                valid |= expiry == 0 or expiry > time.time()
        if not valid:
            raise ValueError('no unexpired YouTube session cookies')
        return data
    except (OSError, ValueError, UnicodeError):
        raise DownloadFailure('SESSION_CONFIGURATION', 'YouTube cookie file must be a private owner-readable Netscape export with unexpired YouTube cookies.') from None


def cookie_status():
    try:
        configured = _cookie_bytes() is not None
        return {'configured': configured, 'state': 'configured_not_verified' if configured else 'not_configured'}
    except DownloadFailure:
        return {'configured': False, 'state': 'invalid_private_cookie_file'}


@contextmanager
def youtube_execution():
    cfg = _read_proxy_config()
    profile = _selected_proxy(cfg)
    if cfg.get('enabled') and profile is None:
        raise DownloadFailure('PROXY_CONFIGURATION', 'Proxy is enabled but selected profile is missing; refusing direct download.')
    proxy = _proxy_url(profile) if profile else ''
    cookies = _cookie_bytes()
    # Private snapshot prevents yt-dlp from rotating/rewriting the owner's cookie export.
    with tempfile.TemporaryDirectory(prefix='audio-youtube-') as tmp:
        config = Path(tmp) / 'yt-dlp.conf'
        settings = ['--proxy ' + shlex.quote(proxy), '--retries 1', '--fragment-retries 1',
                    '--extractor-retries 0', '--file-access-retries 0', '--socket-timeout 20']
        # Mirror the formerly working Video Merger JS runtime contract.
        # The app-scoped guarded release provisions the pinned Deno binary and EJS scripts.
        deno = Path(sys.executable).parent / 'deno'
        if deno.is_file() and os.access(deno, os.X_OK):
            settings.append('--js-runtimes ' + shlex.quote('deno:' + str(deno)))
        # Unlike YouTube credentials, these are non-secret CLI options.
        if cookies is not None:
            session = Path(tmp) / 'session.txt'
            session.write_bytes(cookies)
            session.chmod(0o600)
            settings.append('--cookies ' + shlex.quote(str(session)))
        config.write_text('\n'.join(settings) + '\n', encoding='utf-8')
        config.chmod(0o600)
        yield ['--ignore-config', '--config-locations', str(config)], _youtube_proxy_env(profile)


def _safe_proxy_error(error):
    error = re.sub(r'https?://[^\s]+@[^\s]+', '[proxy-credentials-redacted]', str(error), flags=re.I)
    try:
        cfg = _read_proxy_config()
    except DownloadFailure:
        cfg = _proxy_blank()
    for profile in cfg.get('profiles', []):
        for key in ('password','username'):
            value = profile.get(key)
            if value and len(str(value)) >= 3:
                error = error.replace(str(value), '[redacted]')
                error = error.replace(urllib.parse.quote(str(value),safe=''), '[redacted]')
    return error.replace('\x00','')[:350]


class ProxyProfileRequest(BaseModel):
    id: str = ''
    name: str = Field(min_length=1, max_length=70)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    username: str = Field(default='', max_length=300)
    password: str = Field(default='', max_length=300)


class ProxyEnabledRequest(BaseModel):
    enabled: bool


@router.get('/proxy')
def get_proxy():
    return _public_proxy_config(_read_proxy_config())


@router.post('/proxy/profiles')
def save_proxy_profile(payload: ProxyProfileRequest):
    if not _safe_proxy_host(payload.host):
        raise HTTPException(422, 'Invalid proxy host')
    with _PROXY_LOCK:
        cfg = _read_proxy_config()
        existing = next((p for p in cfg['profiles'] if p['id'] == payload.id), None)
        value = payload.model_dump()
        if existing and not payload.password:
            value['password'] = existing.get('password','')
        value['id'] = existing['id'] if existing else uuid.uuid4().hex[:12]
        if existing:
            cfg['profiles'] = [value if p['id'] == existing['id'] else p for p in cfg['profiles']]
        else:
            if len(cfg['profiles']) >= 16:
                raise HTTPException(422, 'Maximum 16 proxy profiles')
            cfg['profiles'].append(value)
        if not cfg.get('active_id'):
            cfg['active_id'] = value['id']
        cfg['origin'] = 'audio-downloader'
        _write_proxy_config(cfg)
    return _public_proxy_config(cfg)


@router.post('/proxy/enable')
def enable_proxy(payload: ProxyEnabledRequest):
    with _PROXY_LOCK:
        cfg = _read_proxy_config()
        if payload.enabled and not _selected_proxy({**cfg, 'enabled': True}):
            raise HTTPException(422, 'Select a proxy profile first')
        cfg['enabled'] = payload.enabled
        _write_proxy_config(cfg)
    return _public_proxy_config(cfg)


@router.post('/proxy/profiles/{profile_id}/activate')
def activate_proxy(profile_id: str):
    with _PROXY_LOCK:
        cfg = _read_proxy_config()
        if not any(p['id'] == profile_id for p in cfg['profiles']):
            raise HTTPException(404, 'Unknown proxy profile')
        cfg['active_id'] = profile_id
        _write_proxy_config(cfg)
    return _public_proxy_config(cfg)


@router.delete('/proxy/profiles/{profile_id}')
def remove_proxy(profile_id: str):
    with _PROXY_LOCK:
        cfg = _read_proxy_config()
        cfg['profiles'] = [p for p in cfg['profiles'] if p['id'] != profile_id]
        if cfg['active_id'] == profile_id:
            cfg['active_id'] = cfg['profiles'][0]['id'] if cfg['profiles'] else ''
        if not cfg['profiles']:
            cfg['enabled'] = False
        _write_proxy_config(cfg)
    return _public_proxy_config(cfg)


@router.post('/proxy/profiles/{profile_id}/test')
def verify_proxy(profile_id: str):
    cfg = _read_proxy_config()
    p = next((x for x in cfg['profiles'] if x['id'] == profile_id), None)
    if not p:
        raise HTTPException(404, 'Unknown proxy profile')
    try:
        usr = urllib.parse.quote(p.get('username') or '', safe='')
        pw = urllib.parse.quote(p.get('password') or '', safe='')
        creds = (usr+':'+pw+'@') if usr else ''
        url = _proxy_url(p)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http':url, 'https':url}))
        with opener.open(urllib.request.Request('https://www.youtube.com/robots.txt',headers={'User-Agent':'Mozilla/5.0'}),timeout=15) as response:
            ok = response.status in (200, 206)
        return {'ok': ok, 'connectivity_ok': ok, 'download_verified': False, 'test_kind': 'connectivity_only',
                'profile_id': profile_id, 'used_by_worker': bool(cfg.get('enabled') and cfg.get('active_id') == profile_id),
                'message': 'Proxy connectivity OK. Audio download and YouTube authentication are NOT verified.' if ok else 'Proxy connection unsuccessful'}
    except Exception:
        return {'ok': False, 'connectivity_ok': False, 'download_verified': False, 'test_kind': 'connectivity_only', 'message': 'Proxy connection failed. Check profile credentials or provider status.'}

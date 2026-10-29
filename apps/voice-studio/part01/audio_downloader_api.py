"""Isolated YouTube audio extraction queue for the existing Voice Studio service.

All job state and audio belong to AUDIO_DOWNLOADER_ROOT; other voice engines are untouched.
Only explicitly authorized YouTube URLs are accepted. Optional owner-provided private cookies; no challenge bypass.
"""
from __future__ import annotations

import json
import importlib.util
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

from fastapi import APIRouter, HTTPException
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
UPLOAD_RETRY_SECONDS = 180


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
                               ('upload_retry_count', 'INTEGER NOT NULL DEFAULT 0')):
            if name not in columns:
                con.execute(f'ALTER TABLE jobs ADD COLUMN {name} {sql_type}')
        con.execute('''CREATE TABLE IF NOT EXISTS audio_upload_pacing (
            id INTEGER PRIMARY KEY CHECK (id=1), next_upload_at TEXT NOT NULL
        )''')


def recover_interrupted_jobs() -> None:
    # Only on startup: keep completed downloads and their verified Drive receipts.
    # Failed/in-flight uploads resume from local media without touching YouTube.
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        # Old terminal Drive failures with a preserved local audio also qualify.
        rows = con.execute("""SELECT job_id,local_file,video_id,format,auto_upload,status FROM jobs
            WHERE status IN ('downloading','downloaded','uploading')
               OR (status='failed' AND auto_upload=1 AND local_file IS NOT NULL)""").fetchall()
        for row in rows:
            local = Path(row['local_file']) if row['local_file'] else None
            expected = (JOBS / row['job_id'] / (row['video_id'] + '.' + row['format'])).resolve()
            if (row['auto_upload'] and row['status'] != 'downloading' and local
                    and local.resolve() == expected and local.is_file()):
                con.execute("""UPDATE jobs SET status='upload_retry',next_retry_at=?,
                    error='Upload interrupted; automatic retry scheduled',updated_at=?
                    WHERE job_id=?""", (utc_after(UPLOAD_RETRY_SECONDS), utc(), row['job_id']))
            elif row['status'] != 'failed':
                con.execute("""UPDATE jobs SET status='failed',next_retry_at=NULL,
                    error='Interrupted or local audio unavailable; retry manually',updated_at=?
                    WHERE job_id=?""", (utc(), row['job_id']))
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


class BatchRequest(BaseModel):
    links: list[str] = Field(min_length=1, max_length=30)
    format: Literal['mp3', 'wav'] = 'mp3'
    quality: Literal[128, 192, 256] = 192
    auto_upload: bool = True
    keep_metadata: bool = False
    download_thumbnail: bool = False
    permission_confirmed: bool = False


def sanitized_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data.pop('local_file', None)
    data.pop('local_thumbnail', None)
    data['download_thumbnail'] = bool(data['download_thumbnail'])
    data['thumbnail_url'] = ('/tts/api/audio-downloader/jobs/' + data['job_id'] + '/thumbnail') if row['local_thumbnail'] and Path(row['local_thumbnail']).is_file() else None
    data['thumbnail_drive_url'] = data.get('thumbnail_drive_url') if data['status'] == 'delivered' else None
    data['drive_url'] = data.get('drive_url') if data.get('status') == 'delivered' else None
    data['audio_url'] = ('/tts/api/audio-downloader/jobs/' + data['job_id'] + '/audio') if row['local_file'] and Path(row['local_file']).is_file() else None
    data['auto_upload'] = bool(data['auto_upload'])
    data['keep_metadata'] = bool(data['keep_metadata'])
    return data


def deps() -> dict:
    ytdlp = YTDLP if Path(YTDLP).is_file() and os.access(YTDLP, os.X_OK) else shutil.which('yt-dlp')
    # The production Voice venv already owns yt_dlp; do not require a second app venv.
    module_ready = importlib.util.find_spec('yt_dlp') is not None
    command = [ytdlp] if ytdlp else ([sys.executable, '-m', 'yt_dlp'] if module_ready else [])
    ffmpeg = shutil.which('ffmpeg')
    rclone = shutil.which('rclone')
    drive_cfg = Path(RCLONE_CONFIG).is_file()
    return {'ytdlp': ytdlp, 'ytdlp_command': command, 'ffmpeg': ffmpeg, 'rclone': rclone, 'drive_configured': drive_cfg, 'ready': bool(command and ffmpeg), 'upload_ready': bool(rclone and drive_cfg)}


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
            'retry_policy': 'upload-only automatic retry every 180s; 60s between verified audio uploads',
            'upload_gap_seconds': UPLOAD_GAP_SECONDS, 'upload_retry_seconds': UPLOAD_RETRY_SECONDS,
            'queue': {v['status']: v['n'] for v in q}, 'worker': bool(_worker and _worker.is_alive())}


@router.post('/jobs', status_code=201)
def enqueue(payload: BatchRequest):
    if not payload.permission_confirmed:
        raise HTTPException(422, 'Confirm you have permission to download the content')
    if not deps()['ready']:
        raise HTTPException(503, 'Downloader unavailable: yt-dlp or FFmpeg not installed')
    if payload.auto_upload and not deps()['upload_ready']:
        raise HTTPException(503, 'Google Drive upload is not configured')
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
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            for video, url in ids:
                jid = 'ADJ-' + uuid.uuid4().hex[:16].upper()
                con.execute('''INSERT INTO jobs(job_id,batch_id,video_id,url,project,folder,format,quality,
                    auto_upload,keep_metadata,download_thumbnail,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (jid, batch_id, video, url, '', '', payload.format, payload.quality,
                     int(payload.auto_upload), int(payload.keep_metadata), int(payload.download_thumbnail), 'queued', created, created))
                jobs.append(jid)
            con.execute('COMMIT')
        except BaseException:
            con.execute('ROLLBACK')
            raise
    return {'batch_id': batch_id, 'created': len(jobs), 'job_ids': jobs}


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
            con.execute("UPDATE jobs SET status='queued',error=NULL,next_retry_at=NULL,upload_retry_count=0,started_at=NULL,completed_at=NULL,updated_at=? WHERE job_id=? AND status IN ('failed','cancelled')", (utc(), job_id))
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
    for key in ('title', 'local_file', 'local_thumbnail', 'drive_path', 'drive_url', 'thumbnail_drive_path', 'thumbnail_drive_url', 'error', 'started_at', 'completed_at', 'next_retry_at', 'upload_retry_count'):
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
            WHERE (status='upload_retry' AND next_retry_at<=?) OR status='queued'
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


def _schedule_upload_retry(jid: str, error: Exception) -> None:
    # Never re-download an audio file after a Drive-only failure.
    with db() as con:
        row = con.execute('SELECT * FROM jobs WHERE job_id=?', (jid,)).fetchone()
    if row and row['status'] in ('uploading', 'downloaded'):
        local = Path(row['local_file']) if row['local_file'] else None
        expected = (JOBS / jid / (row['video_id'] + '.' + row['format'])).resolve()
        if local and local.resolve() == expected and local.is_file():
            _update(jid, 'upload_retry', error=_safe_proxy_error(str(error))[:420],
                    next_retry_at=utc_after(UPLOAD_RETRY_SECONDS),
                    upload_retry_count=int(row['upload_retry_count']) + 1)
            return
    _update(jid, 'failed', error=_safe_proxy_error(str(error))[:420], next_retry_at=None)


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
    args = command + ['--no-playlist', '--no-warnings', '--no-progress', '--no-cache-dir',
            '--no-overwrites', '--restrict-filenames', '--max-filesize', '120M',
            '--format', 'bestaudio/best', '--ffmpeg-location', state['ffmpeg'],
            '--extract-audio', '--audio-format', audio_format, '--audio-quality', str(job['quality']) + 'K',
            '--print', 'after_move:filepath', '-o', out_template]
    if want_thumbnail:
        args.extend(['--write-thumbnail', '--convert-thumbnails', 'jpg'])
    if job['keep_metadata']:
        args.append('--embed-metadata')
    args.append(job['url'])
    # Use the same private proxy/session snapshot for audio and thumbnail extraction.
    with youtube_execution() as (options, environment):
        try:
            p = subprocess.run(command + options + args[len(command):], capture_output=True,
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

    _update(jid, 'downloaded', title=safe, local_file=str(final),
            local_thumbnail=str(thumbnail) if thumbnail else None)
    if not job['auto_upload']:
        _update(jid, 'completed', completed_at=utc())
        return
    upload_downloaded(job, final, thumbnail, state)


def upload_downloaded(job: dict, final: Path, thumbnail: Path | None, state: dict) -> None:
    jid = job['job_id']
    if not state.get('upload_ready'):
        raise RuntimeError('Google Drive connection unavailable')
    _update(jid, 'uploading')

    # Existing flat destination, immutable filename, and QA nonce segregation.
    base = f'{DRIVE_REMOTE}:{DRIVE_BASE}/'
    if job.get('folder') == '__Audio_Proxy_QA__' and re.fullmatch(r'[A-F0-9]{16}', job.get('project', '')):
        base += safe_folder(job['folder']) + '/' + safe_folder(job['project']) + '/'
    remote = base + final.name

    def upload_file(source: Path, destination: str, fmt: str) -> str:
        cmd = [state['rclone'], '--config', RCLONE_CONFIG, 'copyto', str(source), destination,
               '--immutable', '--retries', '2', '--low-level-retries', '2', '--timeout', '5m',
               '--contimeout', '30s']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1500, check=False)
        if result.returncode:
            raise RuntimeError('Google Drive upload failed; automatic retry scheduled')
        # Avoid a duplicate remote metadata query: the authoritative verifier
        # checks remote size/MD5, public link and anonymous file bytes.
        return verified_drive_public_link(destination, source, fmt)

    # An audio already verified in a previous attempt is never uploaded twice.
    if job.get('drive_path') == remote and job.get('drive_url'):
        public_url = job['drive_url']
    else:
        _wait_for_audio_upload_slot()
        public_url = upload_file(final, remote, job['format'])
        _record_audio_upload()
        _update(jid, 'uploading', drive_path=remote, drive_url=public_url)
    thumb_remote = base + thumbnail.name if thumbnail else None
    if thumbnail and job.get('thumbnail_drive_path') == thumb_remote and job.get('thumbnail_drive_url'):
        thumb_public_url = job['thumbnail_drive_url']
    else:
        thumb_public_url = upload_file(thumbnail, thumb_remote, 'jpg') if thumbnail else None
        if thumbnail:
            _update(jid, 'uploading', thumbnail_drive_path=thumb_remote, thumbnail_drive_url=thumb_public_url)
    _update(jid, 'delivered', drive_path=remote, drive_url=public_url,
            thumbnail_drive_path=thumb_remote, thumbnail_drive_url=thumb_public_url,
            completed_at=utc(), next_retry_at=None, error=None)
    # Delete local files only after BOTH uploads pass size, hash and public-byte checks.
    for local in (final, thumbnail):
        if local is None:
            continue
        try:
            if local.is_file() and local.resolve().parent == (JOBS / jid).resolve():
                local.unlink()
                _update(jid, 'delivered', **{'local_thumbnail' if local == thumbnail else 'local_file': None})
        except OSError:
            pass


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
                except Exception as exc:
                    _schedule_upload_retry(item['job_id'], exc)
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
        return DownloadFailure('YOUTUBE_AUTH_REQUIRED', 'YouTube requires an authorized session or challenge verification. Proxy connectivity does not prove download access. Configure fresh private YouTube cookies; no automatic retry or bypass guarantee.')
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

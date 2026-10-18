from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def ffprobe_json(path: str | Path) -> dict[str, Any]:
    cmd = ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode != 0:
        raise ValueError((completed.stderr or 'ffprobe failed').strip())
    return json.loads(completed.stdout or '{}')


def validate_media(path: str | Path, media_type: str, scene_duration: float = 0.0, *, require_landscape: bool = True) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file() or p.stat().st_size < 1024:
        raise ValueError('Downloaded file is missing or too small')
    suffix = p.suffix.lower()
    if media_type == 'image':
        if suffix not in IMAGE_EXTENSIONS:
            raise ValueError(f'Unsupported image format: {suffix}')
        data = ffprobe_json(p)
        stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
        if not stream:
            raise ValueError('Image could not be decoded')
        width, height = int(stream.get('width') or 0), int(stream.get('height') or 0)
        if width <= 0 or height <= 0:
            raise ValueError('Image dimensions are invalid')
        aspect = width / height
        if require_landscape and aspect < 1.55:
            raise ValueError(f'Portrait or square media rejected: {width}x{height}')
        return {'valid': True, 'media_type': 'image', 'width': width, 'height': height, 'aspect_ratio': aspect, 'warning': 'low_resolution' if width < 1280 or height < 720 else ''}
    if suffix not in VIDEO_EXTENSIONS:
        raise ValueError(f'Unsupported video format: {suffix}')
    data = ffprobe_json(p)
    stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
    if not stream:
        raise ValueError('No video stream found')
    duration = float((data.get('format') or {}).get('duration') or stream.get('duration') or 0.0)
    if duration <= 0:
        raise ValueError('Video duration is invalid')
    width, height = int(stream.get('width') or 0), int(stream.get('height') or 0)
    if width <= 0 or height <= 0:
        raise ValueError('Video dimensions are invalid')
    aspect = width / height
    if require_landscape and aspect < 1.55:
        raise ValueError(f'Portrait or square media rejected: {width}x{height}')
    ratio = duration / max(scene_duration, 0.001) if scene_duration else 1.0
    handling = 'trim' if ratio >= 1.0 else ('slowdown' if ratio >= 0.85 else ('loop_or_freeze' if ratio >= 0.5 else 'reject_short'))
    return {'valid': True, 'media_type': 'video', 'duration': duration, 'width': width, 'height': height, 'aspect_ratio': aspect, 'duration_ratio': ratio, 'handling': handling, 'warning': 'low_resolution' if width < 1280 or height < 720 else ''}


def candidate_score(candidate: dict[str, Any], media_type: str, scene_duration: float, used_asset_ids: set[str], *, require_landscape: bool = True, target_width: int = 1920, target_height: int = 1080) -> float:
    provider_id = f"{candidate.get('provider')}:{candidate.get('asset_id')}"
    if provider_id in used_asset_ids:
        return -1000.0
    width, height = int(candidate.get('width') or 0), int(candidate.get('height') or 0)
    aspect = (width / height) if width > 0 and height > 0 else 0.0
    if require_landscape and aspect < 1.55:
        return -1000.0
    target_ratio = max(1, int(target_width or 1920)) / max(1, int(target_height or 1080))
    ratio_score = max(0.0, 30.0 - abs(aspect - target_ratio) * 60.0)
    resolution = min(20.0, (min(width, 1920) / 1920.0) * 12.0 + (min(height, 1080) / 1080.0) * 8.0)
    relevance = max(5.0, 35.0 - max(0, int(candidate.get('rank') or 1) - 1) * 3.0)
    duration_score = 20.0
    if media_type == 'video' and scene_duration > 0:
        duration = float(candidate.get('duration') or 0.0)
        ratio = duration / scene_duration if duration else 0.0
        if 1.0 <= ratio <= 3.0: duration_score = 20.0
        elif 0.85 <= ratio < 1.0: duration_score = 17.0
        elif 3.0 < ratio <= 8.0: duration_score = 13.0
        elif 0.5 <= ratio < 0.85: duration_score = 7.0
        else: duration_score = -25.0
    return round(relevance + resolution + ratio_score + duration_score + 10.0, 2)


class ReliabilityStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._connect() as conn:
            conn.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS search_attempts(
              id INTEGER PRIMARY KEY AUTOINCREMENT, scene_id TEXT, keyword TEXT, media_type TEXT,
              provider TEXT, asset_id TEXT, candidate_score REAL, status TEXT, error TEXT,
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS assignments(
              scene_id TEXT PRIMARY KEY, requested_media_type TEXT, assigned_media_type TEXT,
              keyword_used TEXT, provider TEXT, asset_id TEXT, file_path TEXT, file_hash TEXT,
              retry_count INTEGER, status TEXT, fallback_type TEXT, fallback_source_scene_id TEXT,
              fallback_reason TEXT, handling TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS used_assets(
              provider_asset_key TEXT PRIMARY KEY, file_hash TEXT, scene_id TEXT, used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs(
              scene_id TEXT PRIMARY KEY, state TEXT, attempts INTEGER, error TEXT, updated_at TEXT
            );
            ''')

    def recover_interrupted(self):
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET state='retry_pending', updated_at=? WHERE state IN ('searching','downloading','validating','assigning')", (now_iso(),))

    def set_job(self, scene_id: str, state: str, attempts: int = 0, error: str = ''):
        with self._connect() as conn:
            conn.execute('''INSERT INTO jobs(scene_id,state,attempts,error,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(scene_id) DO UPDATE SET state=excluded.state, attempts=excluded.attempts, error=excluded.error, updated_at=excluded.updated_at''',
            (scene_id, state, attempts, error, now_iso()))

    def log_attempt(self, scene_id: str, keyword: str, media_type: str, candidate: dict[str, Any] | None, score: float, status: str, error: str = ''):
        with self._connect() as conn:
            conn.execute('''INSERT INTO search_attempts(scene_id,keyword,media_type,provider,asset_id,candidate_score,status,error,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)''', (scene_id, keyword, media_type, str((candidate or {}).get('provider','')), str((candidate or {}).get('asset_id','')), score, status, error, now_iso()))

    def used_asset_ids(self) -> set[str]:
        with self._connect() as conn:
            return {str(r['provider_asset_key']) for r in conn.execute('SELECT provider_asset_key FROM used_assets')}

    def used_hashes(self) -> set[str]:
        with self._connect() as conn:
            return {str(r['file_hash']) for r in conn.execute('SELECT file_hash FROM used_assets WHERE file_hash != ""')}

    def save_assignment(self, row: dict[str, Any]):
        with self._connect() as conn:
            conn.execute('''INSERT INTO assignments(scene_id,requested_media_type,assigned_media_type,keyword_used,provider,asset_id,file_path,file_hash,retry_count,status,fallback_type,fallback_source_scene_id,fallback_reason,handling,updated_at)
            VALUES(:scene_id,:requested_media_type,:assigned_media_type,:keyword_used,:provider,:asset_id,:file_path,:file_hash,:retry_count,:status,:fallback_type,:fallback_source_scene_id,:fallback_reason,:handling,:updated_at)
            ON CONFLICT(scene_id) DO UPDATE SET requested_media_type=excluded.requested_media_type,assigned_media_type=excluded.assigned_media_type,keyword_used=excluded.keyword_used,provider=excluded.provider,asset_id=excluded.asset_id,file_path=excluded.file_path,file_hash=excluded.file_hash,retry_count=excluded.retry_count,status=excluded.status,fallback_type=excluded.fallback_type,fallback_source_scene_id=excluded.fallback_source_scene_id,fallback_reason=excluded.fallback_reason,handling=excluded.handling,updated_at=excluded.updated_at''', row)
            key = f"{row.get('provider')}:{row.get('asset_id')}" if row.get('provider') and row.get('asset_id') else ''
            if key:
                conn.execute('INSERT OR REPLACE INTO used_assets(provider_asset_key,file_hash,scene_id,used_at) VALUES(?,?,?,?)', (key,row.get('file_hash',''),row.get('scene_id',''),now_iso()))

    def export_report(self, target: str | Path, summary: dict[str, Any]):
        target = Path(target)
        with self._connect() as conn:
            assignments = [dict(r) for r in conn.execute('SELECT * FROM assignments ORDER BY scene_id')]
            attempts = [dict(r) for r in conn.execute('SELECT * FROM search_attempts ORDER BY id')]
        payload = {'generated_at': now_iso(), 'summary': summary, 'assignments': assignments, 'search_attempts': attempts}
        temp = target.with_suffix(target.suffix + '.tmp')
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, target)

# ---------------------------------------------------------------------------
# Scene-media efficiency policy
# ---------------------------------------------------------------------------
TINY_SCENE_REUSE_SECONDS = max(0.10, min(2.0, float(os.getenv("TINY_SCENE_REUSE_SECONDS", "0.75"))))
WORKING_CLIP_TINY_SECONDS = max(0.5, float(os.getenv("WORKING_CLIP_TINY_SECONDS", "2.0")))
WORKING_CLIP_SHORT_SECONDS = max(1.0, float(os.getenv("WORKING_CLIP_SHORT_SECONDS", "3.0")))
WORKING_CLIP_HANDLE_SECONDS = max(0.25, float(os.getenv("WORKING_CLIP_HANDLE_SECONDS", "1.0")))


def should_continue_previous_visual(scene_duration: float) -> bool:
    """Tiny narration fragments should normally inherit the prior visual.

    This avoids a provider request + full stock-video download for a few frames.
    Timing is not changed; only the visual assignment is reused.
    """
    try:
        return 0.0 < float(scene_duration) < TINY_SCENE_REUSE_SECONDS
    except Exception:
        return False


def working_clip_target_duration(scene_duration: float) -> float:
    """Return the short on-disk working clip length including edit handles."""
    duration = max(0.05, float(scene_duration or 0.0))
    if duration <= 1.0:
        return WORKING_CLIP_TINY_SECONDS
    if duration <= 2.0:
        return WORKING_CLIP_SHORT_SECONDS
    if duration <= 4.0:
        return duration + WORKING_CLIP_HANDLE_SECONDS
    # Longer scenes still benefit from a small handle without retaining a very long
    # provider file inside every per-project resolved_media directory.
    return duration + WORKING_CLIP_HANDLE_SECONDS


def build_working_video_clip(
    source: str | Path,
    destination: str | Path,
    scene_duration: float,
    *,
    source_duration: float = 0.0,
    require_landscape: bool = True,
) -> dict[str, Any]:
    """Create a compact scene-working MP4 from a provider source video.

    The provider/library source is left untouched. The per-project assignment is a
    short clip sized for the scene plus a small editing handle. If trimming is not
    useful or FFmpeg fails, a normal copy is used so media acquisition never fails
    solely because of this optimization.
    """
    src = Path(source)
    dst = Path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.is_file():
        raise ValueError(f'Video source is missing: {src}')

    try:
        if source_duration <= 0:
            source_duration = float(validate_media(src, 'video', max(0.05, scene_duration), require_landscape=require_landscape).get('duration') or 0.0)
    except Exception:
        source_duration = max(0.0, float(source_duration or 0.0))

    target = working_clip_target_duration(scene_duration)
    trim_useful = source_duration > target + 0.35
    if not trim_useful:
        shutil.copy2(src, dst)
        return {
            'trimmed': False,
            'source_duration': source_duration,
            'working_duration': source_duration,
            'target_duration': target,
            'start_offset': 0.0,
        }

    # Avoid always using the very first frame of stock clips. A deterministic 25%
    # offset into the available trim window gives motion handles while remaining
    # reproducible across retries/restarts.
    start = max(0.0, (source_duration - target) * 0.25)
    temporary = dst.with_name(f'.{dst.stem}.working{dst.suffix or ".mp4"}')
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-ss', f'{start:.3f}', '-i', str(src), '-t', f'{target:.3f}',
        '-map', '0:v:0', '-an', '-c:v', 'libx264', '-preset', 'veryfast',
        '-crf', '21', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(temporary),
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size < 1024:
            raise RuntimeError((completed.stderr or 'FFmpeg working-clip trim failed').strip())
        temporary.replace(dst)
        try:
            actual = float(validate_media(dst, 'video', max(0.05, scene_duration), require_landscape=require_landscape).get('duration') or target)
        except Exception:
            actual = target
        return {
            'trimmed': True,
            'source_duration': source_duration,
            'working_duration': actual,
            'target_duration': target,
            'start_offset': start,
        }
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass
        shutil.copy2(src, dst)
        return {
            'trimmed': False,
            'source_duration': source_duration,
            'working_duration': source_duration,
            'target_duration': target,
            'start_offset': 0.0,
            'fallback_copy': True,
        }

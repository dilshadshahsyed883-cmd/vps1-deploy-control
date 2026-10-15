from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import FileResponse

from autoflow_control_backend import _db, _now, _require_api

LEASE_MINUTES = 12
WORKER_ONLINE_SECONDS = 45
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGES = 4
RESULT_ROOT = Path('/home/ubuntu/.local/share/autoflow-flow/results')
WS_CONNECTIONS: dict[str, WebSocket] = {}


def _hash(value: str) -> str:
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def _dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def _iso_after(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _safe(value: str, fallback: str = 'item') -> str:
    s = re.sub(r'[^A-Za-z0-9._-]+', '-', str(value or '').strip()).strip('-._')
    return (s or fallback)[:120]


def _worker_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out.pop('token_hash', None)
    out['enabled'] = bool(out.get('enabled'))
    seen = _dt(str(out.get('last_seen') or ''))
    out['online'] = bool(seen and (datetime.now(timezone.utc) - seen).total_seconds() <= WORKER_ONLINE_SECONDS)
    return out


def _worker_auth_values(worker_id: str, token: str) -> dict[str, Any]:
    if not worker_id or not token:
        raise HTTPException(401, 'Flow worker credentials required')
    with _db() as conn:
        row = conn.execute('SELECT * FROM autoflow_flow_workers WHERE id=? AND enabled=1', (worker_id,)).fetchone()
    if not row or not hmac.compare_digest(str(row['token_hash']), _hash(token)):
        raise HTTPException(401, 'Invalid Flow worker credentials')
    return dict(row)


def _worker_auth(request: Request) -> dict[str, Any]:
    return _worker_auth_values(
        str(request.headers.get('X-AutoFlow-Worker-Id') or '').strip(),
        str(request.headers.get('X-AutoFlow-Worker-Token') or '').strip(),
    )


def _audit(conn: sqlite3.Connection, tjob: sqlite3.Row, event: str, old: str, new: str, detail: dict[str, Any] | None = None) -> None:
    conn.execute(
        'INSERT INTO autoflow_thumbnail_audit(project_id,permanent_id,job_id,event,from_status,to_status,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
        (int(tjob['project_id']), str(tjob['permanent_id']), int(tjob['id']), event, old, new, json.dumps(detail or {}, ensure_ascii=False), _now()),
    )


def init_flow_bridge_schema() -> None:
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS autoflow_flow_pairings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              code_hash TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS autoflow_flow_workers (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              account_label TEXT NOT NULL DEFAULT '',
              chrome_profile TEXT NOT NULL DEFAULT '',
              token_hash TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              last_seen TEXT NOT NULL DEFAULT '',
              browser_state TEXT NOT NULL DEFAULT 'UNKNOWN',
              active_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS autoflow_flow_jobs (
              id TEXT PRIMARY KEY,
              thumbnail_job_id INTEGER,
              project_id INTEGER,
              permanent_id TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL DEFAULT '',
              prompt TEXT NOT NULL DEFAULT '',
              reference_image TEXT NOT NULL DEFAULT '',
              aspect_ratio TEXT NOT NULL DEFAULT '16:9',
              variations INTEGER NOT NULL DEFAULT 2,
              creativity INTEGER NOT NULL DEFAULT 5,
              status TEXT NOT NULL DEFAULT 'QUEUED',
              worker_id TEXT NOT NULL DEFAULT '',
              lease_hash TEXT NOT NULL DEFAULT '',
              lease_expires_at TEXT NOT NULL DEFAULT '',
              attempt_count INTEGER NOT NULL DEFAULT 0,
              result_count INTEGER NOT NULL DEFAULT 0,
              error_code TEXT NOT NULL DEFAULT '',
              error_text TEXT NOT NULL DEFAULT '',
              test_only INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT '',
              completed_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_af_flow_jobs_queue ON autoflow_flow_jobs(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_af_flow_jobs_worker ON autoflow_flow_jobs(worker_id, status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_af_flow_jobs_thumb_unique ON autoflow_flow_jobs(thumbnail_job_id) WHERE thumbnail_job_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS autoflow_flow_results (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              flow_job_id TEXT NOT NULL,
              image_index INTEGER NOT NULL,
              mime_type TEXT NOT NULL,
              file_path TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(flow_job_id, image_index)
            );
            '''
        )
        cols = {r[1] for r in conn.execute('PRAGMA table_info(autoflow_thumbnail_jobs)').fetchall()}
        if 'result_url' not in cols:
            conn.execute("ALTER TABLE autoflow_thumbnail_jobs ADD COLUMN result_url TEXT NOT NULL DEFAULT ''")


def _clean_expired(conn: sqlite3.Connection) -> None:
    now_dt = datetime.now(timezone.utc)
    rows = conn.execute("SELECT * FROM autoflow_flow_jobs WHERE status='RUNNING' AND lease_expires_at!=''").fetchall()
    for row in rows:
        exp = _dt(str(row['lease_expires_at'] or ''))
        if exp and exp <= now_dt:
            now = _now()
            conn.execute("UPDATE autoflow_flow_jobs SET status='QUEUED',worker_id='',lease_hash='',lease_expires_at='',error_code='LEASE_EXPIRED',error_text='Worker lease expired',updated_at=? WHERE id=?", (now, row['id']))
            if row['thumbnail_job_id'] is not None:
                t = conn.execute('SELECT * FROM autoflow_thumbnail_jobs WHERE id=?', (row['thumbnail_job_id'],)).fetchone()
                if t and str(t['status']).upper() == 'GENERATING':
                    conn.execute("UPDATE autoflow_thumbnail_jobs SET status='RETRYING',error_text='Flow worker lease expired',updated_at=? WHERE id=?", (now, t['id']))
                    _audit(conn, t, 'FLOW_LEASE_EXPIRED', 'GENERATING', 'RETRYING', {'flow_job_id': row['id']})


def _ensure_production_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    t = conn.execute(
        "SELECT t.*,v.title FROM autoflow_thumbnail_jobs t LEFT JOIN autoflow_videos v ON v.project_id=t.project_id AND v.permanent_id=t.permanent_id WHERE t.status IN ('PENDING','RETRYING') AND TRIM(t.prompt)!='' ORDER BY t.updated_at DESC,t.id DESC LIMIT 1"
    ).fetchone()
    if not t:
        return None
    row = conn.execute('SELECT * FROM autoflow_flow_jobs WHERE thumbnail_job_id=?', (t['id'],)).fetchone()
    now = _now()
    if row is None:
        jid = str(uuid.uuid4())
        conn.execute(
            '''INSERT INTO autoflow_flow_jobs(id,thumbnail_job_id,project_id,permanent_id,title,prompt,reference_image,aspect_ratio,variations,creativity,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'QUEUED',?,?)''',
            (jid, int(t['id']), int(t['project_id']), str(t['permanent_id']), str(t['title'] or t['permanent_id']), str(t['prompt'] or ''), str(t['reference_image'] or ''), '16:9', 2, 5, now, now),
        )
        row = conn.execute('SELECT * FROM autoflow_flow_jobs WHERE id=?', (jid,)).fetchone()
    elif str(row['status']).upper() in {'ERROR','CANCELLED'}:
        conn.execute("UPDATE autoflow_flow_jobs SET status='QUEUED',prompt=?,reference_image=?,title=?,worker_id='',lease_hash='',lease_expires_at='',error_code='',error_text='',updated_at=? WHERE id=?", (str(t['prompt'] or ''), str(t['reference_image'] or ''), str(t['title'] or t['permanent_id']), now, row['id']))
        row = conn.execute('SELECT * FROM autoflow_flow_jobs WHERE id=?', (row['id'],)).fetchone()
    return row if str(row['status']).upper() == 'QUEUED' else None


def _verify_lease(conn: sqlite3.Connection, worker: dict[str, Any], job_id: str, lease_token: str, allow_done: bool = False) -> sqlite3.Row:
    row = conn.execute('SELECT * FROM autoflow_flow_jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Flow job not found')
    if allow_done and str(row['status']) == 'DONE':
        return row
    if str(row['status']) != 'RUNNING' or str(row['worker_id']) != str(worker['id']):
        raise HTTPException(409, 'Flow job is not leased to this worker')
    if not lease_token or not hmac.compare_digest(str(row['lease_hash']), _hash(lease_token)):
        raise HTTPException(401, 'Invalid Flow lease token')
    return row


async def _notify(payload: dict[str, Any], only: str | None = None) -> None:
    dead = []
    for wid, ws in list(WS_CONNECTIONS.items()):
        if only and wid != only:
            continue
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(wid)
    for wid in dead:
        WS_CONNECTIONS.pop(wid, None)


def _decode_image(item: dict[str, Any]) -> tuple[bytes, str]:
    data = str(item.get('data_url') or item.get('base64') or '').strip()
    mime = str(item.get('mime_type') or '').lower().strip()
    if data.startswith('data:'):
        head, _, body = data.partition(',')
        m = re.match(r'data:([^;]+);base64$', head, re.I)
        if not m:
            raise HTTPException(400, 'Image data URL must be base64 encoded')
        mime = m.group(1).lower()
        data = body
    if mime not in {'image/jpeg','image/png','image/webp'}:
        raise HTTPException(400, f'Unsupported image type: {mime or "unknown"}')
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception as exc:
        raise HTTPException(400, 'Invalid base64 image data') from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(400, 'Returned thumbnail image size is invalid')
    if mime == 'image/png' and not raw.startswith(b'\x89PNG'):
        raise HTTPException(400, 'Invalid PNG payload')
    if mime == 'image/jpeg' and not raw.startswith(b'\xff\xd8'):
        raise HTTPException(400, 'Invalid JPEG payload')
    if mime == 'image/webp' and not (raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'):
        raise HTTPException(400, 'Invalid WebP payload')
    return raw, mime


def register_autoflow_flow_bridge(app) -> None:
    init_flow_bridge_schema()

    @app.get('/api/master-controller/flow/pairings')
    def flow_pair_status(request: Request):
        _require_api(request)
        with _db() as conn:
            row = conn.execute('SELECT id,created_at FROM autoflow_flow_pairings ORDER BY id DESC LIMIT 1').fetchone()
        return {'ok': True, 'active': bool(row), 'permanent': True, 'created_at': str(row['created_at'] if row else '')}

    @app.post('/api/master-controller/flow/pairings')
    def flow_pair_create(request: Request):
        _require_api(request)
        code = secrets.token_urlsafe(18)
        with _db() as conn:
            conn.execute('DELETE FROM autoflow_flow_pairings')
            conn.execute('INSERT INTO autoflow_flow_pairings(code_hash,created_at) VALUES(?,?)', (_hash(code), _now()))
        return {'ok': True, 'code': code, 'permanent': True}

    @app.delete('/api/master-controller/flow/pairings')
    def flow_pair_delete(request: Request):
        _require_api(request)
        with _db() as conn:
            cur = conn.execute('DELETE FROM autoflow_flow_pairings')
        return {'ok': True, 'deleted': int(cur.rowcount or 0), 'workers_untouched': True}

    @app.post('/api/master-controller/flow/pairings/exchange')
    async def flow_pair_exchange(request: Request):
        body = await request.json()
        code = str(body.get('code') or '').strip()
        if not code:
            raise HTTPException(400, 'Pairing code required')
        with _db() as conn:
            row = conn.execute('SELECT * FROM autoflow_flow_pairings WHERE code_hash=?', (_hash(code),)).fetchone()
            if not row:
                raise HTTPException(401, 'Pairing code invalid')
            wid = str(uuid.uuid4())
            token = secrets.token_urlsafe(36)
            now = _now()
            name = str(body.get('worker_name') or 'Flow Worker').strip()[:100]
            account = str(body.get('account_label') or '').strip()[:160]
            profile = str(body.get('chrome_profile') or '').strip()[:100]
            conn.execute('INSERT INTO autoflow_flow_workers(id,name,account_label,chrome_profile,token_hash,last_seen,browser_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)', (wid,name,account,profile,_hash(token),now,'PAIRED',now,now))
        return {'ok': True, 'worker_id': wid, 'worker_token': token, 'max_parallel': 1}

    @app.get('/api/master-controller/flow/workers')
    def flow_workers(request: Request):
        _require_api(request)
        with _db() as conn:
            rows = conn.execute('SELECT * FROM autoflow_flow_workers ORDER BY created_at DESC').fetchall()
        return {'ok': True, 'workers': [_worker_public(r) for r in rows]}

    @app.delete('/api/master-controller/flow/workers/{worker_id}')
    def flow_worker_revoke(worker_id: str, request: Request):
        _require_api(request)
        with _db() as conn:
            cur = conn.execute("UPDATE autoflow_flow_workers SET enabled=0,token_hash=?,updated_at=? WHERE id=?", (_hash(secrets.token_urlsafe(32)), _now(), worker_id))
            if not cur.rowcount:
                raise HTTPException(404, 'Flow worker not found')
            conn.execute("UPDATE autoflow_flow_jobs SET status='QUEUED',worker_id='',lease_hash='',lease_expires_at='',updated_at=? WHERE worker_id=? AND status='RUNNING'", (_now(), worker_id))
        WS_CONNECTIONS.pop(worker_id, None)
        return {'ok': True}

    @app.get('/api/master-controller/flow/jobs')
    def flow_jobs(request: Request, limit: int = 20):
        _require_api(request)
        with _db() as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM autoflow_flow_jobs ORDER BY created_at DESC LIMIT ?', (max(1,min(int(limit),100)),)).fetchall()]
        for r in rows:
            r.pop('lease_hash', None)
        return {'ok': True, 'jobs': rows}

    @app.post('/api/master-controller/flow/test-jobs')
    async def flow_test_job(request: Request):
        _require_api(request)
        body = await request.json()
        now = _now(); jid = str(uuid.uuid4()); pid = f"FLOW-TEST-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        with _db() as conn:
            conn.execute('''INSERT INTO autoflow_flow_jobs(id,thumbnail_job_id,project_id,permanent_id,title,prompt,aspect_ratio,variations,creativity,status,test_only,created_at,updated_at) VALUES(?,NULL,NULL,?,?,?,?,?,?, 'QUEUED',1,?,?)''',
                (jid,pid,str(body.get('title') or 'Auto Flow Flow Worker Test'),str(body.get('prompt') or 'Photorealistic professional YouTube thumbnail, a bright green check mark made of light on a clean dark studio background, cinematic lighting, no text, no logo, no watermark'),'16:9',1,5,now,now))
        await _notify({'type':'job_available'})
        return {'ok': True, 'job': {'id':jid,'permanent_id':pid,'status':'QUEUED'}}

    @app.post('/api/master-controller/flow/worker/heartbeat')
    async def flow_heartbeat(request: Request):
        worker = _worker_auth(request); body = await request.json(); now = _now()
        state = str(body.get('browser_state') or 'READY')[:80]; err = str(body.get('last_error') or '')[:1000]
        with _db() as conn:
            _clean_expired(conn)
            conn.execute('UPDATE autoflow_flow_workers SET last_seen=?,active_count=?,browser_state=?,last_error=?,updated_at=? WHERE id=?', (now,max(0,min(1,int(body.get('active_count') or 0))),state,err,now,worker['id']))
        return {'ok': True, 'server_time': now}

    @app.post('/api/master-controller/flow/worker/claim')
    async def flow_claim(request: Request):
        worker = _worker_auth(request); now = _now(); lease = secrets.token_urlsafe(30)
        with _db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            _clean_expired(conn)
            if str(worker.get('browser_state') or '').upper() == 'ATTENTION':
                conn.commit(); return {'ok':True,'job':None,'reason':'WORKER_ATTENTION'}
            running = conn.execute("SELECT COUNT(*) FROM autoflow_flow_jobs WHERE status='RUNNING'").fetchone()[0]
            if running:
                conn.commit(); return {'ok':True,'job':None,'reason':'GLOBAL_LIMIT'}
            row = conn.execute("SELECT * FROM autoflow_flow_jobs WHERE status='QUEUED' ORDER BY test_only DESC,created_at DESC LIMIT 1").fetchone()
            if not row:
                row = _ensure_production_job(conn)
            if not row:
                conn.commit(); return {'ok':True,'job':None,'reason':'EMPTY'}
            exp = _iso_after(LEASE_MINUTES)
            conn.execute("UPDATE autoflow_flow_jobs SET status='RUNNING',worker_id=?,lease_hash=?,lease_expires_at=?,attempt_count=attempt_count+1,started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,error_code='',error_text='',updated_at=? WHERE id=? AND status='QUEUED'", (worker['id'],_hash(lease),exp,now,now,row['id']))
            row = conn.execute('SELECT * FROM autoflow_flow_jobs WHERE id=?', (row['id'],)).fetchone()
            if row['thumbnail_job_id'] is not None:
                t = conn.execute('SELECT * FROM autoflow_thumbnail_jobs WHERE id=?', (row['thumbnail_job_id'],)).fetchone()
                if t:
                    old = str(t['status'] or '')
                    conn.execute("UPDATE autoflow_thumbnail_jobs SET status='GENERATING',attempt_count=attempt_count+1,error_text='',updated_at=? WHERE id=?", (now,t['id']))
                    _audit(conn,t,'FLOW_CLAIMED',old,'GENERATING',{'flow_job_id':row['id'],'worker_id':worker['id']})
            conn.execute("UPDATE autoflow_flow_workers SET last_seen=?,active_count=1,browser_state='WORKING',updated_at=? WHERE id=?", (now,now,worker['id']))
            conn.commit()
        return {'ok':True,'job':{'id':row['id'],'thumbnail_job_id':row['thumbnail_job_id'],'project_id':row['project_id'],'permanent_id':row['permanent_id'],'title':row['title'],'prompt':row['prompt'],'reference_image':row['reference_image'],'aspect_ratio':row['aspect_ratio'],'variations':row['variations'],'creativity':row['creativity'],'lease_token':lease,'lease_expires_at':exp,'test_only':bool(row['test_only'])}}

    @app.post('/api/master-controller/flow/worker/jobs/{job_id}/lease')
    async def flow_lease(job_id: str, request: Request):
        worker=_worker_auth(request); body=await request.json(); token=str(body.get('lease_token') or ''); exp=_iso_after(LEASE_MINUTES); now=_now()
        with _db() as conn:
            _verify_lease(conn,worker,job_id,token)
            conn.execute('UPDATE autoflow_flow_jobs SET lease_expires_at=?,updated_at=? WHERE id=?',(exp,now,job_id))
            conn.execute("UPDATE autoflow_flow_workers SET last_seen=?,browser_state='WORKING',updated_at=? WHERE id=?",(now,now,worker['id']))
        return {'ok':True,'lease_expires_at':exp}

    @app.post('/api/master-controller/flow/worker/jobs/{job_id}/result')
    async def flow_result(job_id: str, request: Request):
        worker=_worker_auth(request); body=await request.json(); token=str(body.get('lease_token') or ''); images=body.get('images')
        if not isinstance(images,list) or not images:
            raise HTTPException(400,'At least one returned image is required')
        images=images[:MAX_IMAGES]
        with _db() as conn:
            row=conn.execute('SELECT * FROM autoflow_flow_jobs WHERE id=?',(job_id,)).fetchone()
            if not row: raise HTTPException(404,'Flow job not found')
            if str(row['status'])=='DONE': return {'ok':True,'duplicate':True,'result_count':int(row['result_count'] or 0)}
            row=_verify_lease(conn,worker,job_id,token)
            out_dir=RESULT_ROOT/_safe(str(row['project_id'] or 'test'))/_safe(str(row['permanent_id'] or 'item'))/_safe(job_id)
            out_dir.mkdir(parents=True,exist_ok=True)
            decoded=[]
            for idx,item in enumerate(images,1):
                if not isinstance(item,dict): continue
                raw,mime=_decode_image(item); ext={'image/jpeg':'jpg','image/png':'png','image/webp':'webp'}[mime]
                path=out_dir/f'{_safe(str(row["permanent_id"]))}-v{idx}.{ext}'
                path.write_bytes(raw); decoded.append((idx,mime,path))
            if not decoded: raise HTTPException(400,'No valid images were returned')
            conn.execute('DELETE FROM autoflow_flow_results WHERE flow_job_id=?',(job_id,))
            now=_now()
            for idx,mime,path in decoded:
                conn.execute('INSERT INTO autoflow_flow_results(flow_job_id,image_index,mime_type,file_path,created_at) VALUES(?,?,?,?,?)',(job_id,idx,mime,str(path),now))
            conn.execute("UPDATE autoflow_flow_jobs SET status='DONE',result_count=?,lease_hash='',lease_expires_at='',completed_at=?,updated_at=? WHERE id=?",(len(decoded),now,now,job_id))
            result_url=f'/api/master-controller/flow/jobs/{job_id}/images/1'
            if row['thumbnail_job_id'] is not None:
                t=conn.execute('SELECT * FROM autoflow_thumbnail_jobs WHERE id=?',(row['thumbnail_job_id'],)).fetchone()
                if t:
                    old=str(t['status'] or 'GENERATING'); final='WAITING_APPROVAL' if int(t['approval_required'] or 0) else 'AUTO_APPROVED'
                    conn.execute('UPDATE autoflow_thumbnail_jobs SET status=?,local_file=?,result_url=?,flow_profile=?,final_prompt=?,error_text=?,completed_at=?,updated_at=? WHERE id=?',(final,str(decoded[0][2]),result_url,str(worker.get('account_label') or worker.get('name') or 'Flow Worker'),str(row['prompt'] or ''),'',now if final=='AUTO_APPROVED' else str(t['completed_at'] or ''),now,t['id']))
                    _audit(conn,t,'FLOW_RESULT_RECEIVED',old,final,{'flow_job_id':job_id,'worker_id':worker['id'],'images':len(decoded)})
                    if final=='AUTO_APPROVED':
                        asset=conn.execute("SELECT * FROM autoflow_assets WHERE project_id=? AND permanent_id=? AND asset_type='thumbnail'",(t['project_id'],t['permanent_id'])).fetchone()
                        if asset:
                            conn.execute("UPDATE autoflow_assets SET file_url=?,version=?,outdated=0,updated_at=? WHERE id=?",(result_url,int(t['version'] or 1),now,asset['id']))
                        else:
                            conn.execute("INSERT INTO autoflow_assets(project_id,permanent_id,asset_type,text_content,source_url,file_url,mode,locked,outdated,version,created_at,updated_at) VALUES(?,?,'thumbnail',?,?,?,?,0,0,?,?,?)",(t['project_id'],t['permanent_id'],str(t['prompt'] or ''),str(t['reference_image'] or ''),result_url,'AUTO',int(t['version'] or 1),now,now))
            conn.execute("UPDATE autoflow_flow_workers SET last_seen=?,active_count=0,browser_state='READY',last_error='',updated_at=? WHERE id=?",(now,now,worker['id']))
        await _notify({'type':'claim_more'},worker['id'])
        return {'ok':True,'result_count':len(decoded),'result_url':result_url}

    @app.post('/api/master-controller/flow/worker/jobs/{job_id}/fail')
    async def flow_fail(job_id: str, request: Request):
        worker=_worker_auth(request); body=await request.json(); token=str(body.get('lease_token') or ''); code=str(body.get('error_code') or 'FLOW_WORKER_ERROR').upper()[:80]; text=str(body.get('error_text') or '')[:4000]; now=_now()
        blocking=code in {'LOGIN_REQUIRED','CAPTCHA','RATE_LIMIT','ACCOUNT_RESTRICTION','UNEXPECTED_PAGE','FLOW_TAB_NOT_OPEN'}
        with _db() as conn:
            row=_verify_lease(conn,worker,job_id,token)
            conn.execute("UPDATE autoflow_flow_jobs SET status='ERROR',error_code=?,error_text=?,lease_hash='',lease_expires_at='',completed_at=?,updated_at=? WHERE id=?",(code,text,now,now,job_id))
            if row['thumbnail_job_id'] is not None:
                t=conn.execute('SELECT * FROM autoflow_thumbnail_jobs WHERE id=?',(row['thumbnail_job_id'],)).fetchone()
                if t:
                    attempts=int(t['attempt_count'] or 0); final='FAILED' if attempts>=3 else 'RETRYING'
                    conn.execute('UPDATE autoflow_thumbnail_jobs SET status=?,error_text=?,updated_at=? WHERE id=?',(final,f'{code}: {text}'[:2000],now,t['id']))
                    _audit(conn,t,'FLOW_FAILED',str(t['status'] or 'GENERATING'),final,{'flow_job_id':job_id,'worker_id':worker['id'],'error_code':code})
            conn.execute('UPDATE autoflow_flow_workers SET last_seen=?,active_count=0,browser_state=?,last_error=?,updated_at=? WHERE id=?',(now,'ATTENTION' if blocking else 'READY',f'{code}: {text}'[:1000],now,worker['id']))
        if not blocking: await _notify({'type':'claim_more'},worker['id'])
        return {'ok':True,'worker_paused':blocking}

    @app.get('/api/master-controller/flow/jobs/{job_id}/images/{image_index}')
    def flow_image(job_id: str, image_index: int, request: Request):
        _require_api(request)
        with _db() as conn:
            row=conn.execute('SELECT * FROM autoflow_flow_results WHERE flow_job_id=? AND image_index=?',(job_id,int(image_index))).fetchone()
        if not row: raise HTTPException(404,'Flow thumbnail image not found')
        path=Path(str(row['file_path']));
        if not path.is_file(): raise HTTPException(404,'Flow thumbnail file missing')
        return FileResponse(path, media_type=str(row['mime_type']), headers={'Cache-Control':'private, max-age=300'})

    @app.websocket('/api/master-controller/flow/ws')
    async def flow_ws(websocket: WebSocket):
        wid=str(websocket.query_params.get('worker_id') or '').strip(); token=str(websocket.query_params.get('token') or '').strip()
        try: _worker_auth_values(wid,token)
        except HTTPException:
            await websocket.close(code=4401); return
        await websocket.accept(); WS_CONNECTIONS[wid]=websocket
        try:
            await websocket.send_json({'type':'connected','worker_id':wid,'max_parallel':1})
            while True:
                msg=await websocket.receive_json()
                if str(msg.get('type') or '')=='heartbeat':
                    now=_now()
                    with _db() as conn:
                        conn.execute('UPDATE autoflow_flow_workers SET last_seen=?,active_count=?,browser_state=?,last_error=?,updated_at=? WHERE id=?',(now,max(0,min(1,int(msg.get('active_count') or 0))),str(msg.get('browser_state') or 'READY')[:80],str(msg.get('last_error') or '')[:1000],now,wid))
                    await websocket.send_json({'type':'heartbeat_ack'})
        except Exception:
            pass
        finally:
            WS_CONNECTIONS.pop(wid,None)

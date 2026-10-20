"""App-owned scheduler, guarded cleanup receipts and streaming settings."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from fastapi import APIRouter,Body,Request

router=APIRouter(tags=['Auto Stream operations'])
FLAGS=('AUTO_STREAM_PUBLISH_ENABLED','AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED','AUTO_STREAM_LIVE_TRANSITIONS_ENABLED','AUTO_STREAM_GO_LIVE_ENABLED','AUTO_STREAM_SCHEDULER_ENABLED')
_stop=threading.Event()
_thread=None
_lease_fd=None
RCLONE_CONFIG='/home/ubuntu/.config/masterctl/rclone.conf'


def initialize(engine):
    path=engine.root/'operator-settings.json'
    if path.exists() and not path.is_symlink():
        try:
            doc=json.loads(path.read_text())
            for flag in FLAGS:
                if flag in doc:os.environ[flag]='1' if doc[flag] else '0'
        except (ValueError,OSError):pass
    with engine._connect() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS cleanup_receipts(job_id TEXT NOT NULL,relative_path TEXT NOT NULL,
            drive_path TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,verified_at TEXT NOT NULL,
            PRIMARY KEY(job_id,relative_path))''')
        con.execute('''CREATE TABLE IF NOT EXISTS cleanup_runs(id INTEGER PRIMARY KEY,job_id TEXT,state TEXT NOT NULL,
            message TEXT NOT NULL,created_at TEXT NOT NULL)''')


def settings():
    from auto_stream_engine import service
    engine=service()
    return {'flags':{flag:os.environ.get(flag)=='1' for flag in FLAGS},'cleanup_after_hours':12,
            'cleanup_owner':'auto-stream','rclone_ready':bool(shutil.which('rclone') and Path(RCLONE_CONFIG).is_file())}


def rclone(args):
    if not shutil.which('rclone') or not Path(RCLONE_CONFIG).is_file():raise ValueError('Canonical Drive connection is unavailable for this application')
    result=subprocess.run(['rclone','--config',RCLONE_CONFIG,*args],capture_output=True,timeout=3600)
    if result.returncode:raise ValueError('Drive transfer/verification failed; local files preserved')
    return result.stdout


def preserve(engine,jid,remote):
    """Copy only app-created render artifacts, then verify by remote SHA-256 readback."""
    from auto_stream_engine import utc_now
    job=engine.get(jid)
    if job['status'] not in ('COMPLETED','STOPPED'):raise ValueError('Job must complete before preservation/cleanup')
    if not isinstance(remote,str) or not remote.startswith('gdrive:') or any(x in remote for x in ('..','\n','\r')):
        raise ValueError('Choose a canonical gdrive: destination folder')
    source=engine._source(job['media_path'])
    # Never make shared/legacy sources eligible just because one stream finished.
    if not source.is_relative_to((engine.media/'auto-stream'/'renders').resolve()):raise ValueError('Only app-owned rendered compositions are eligible; shared or uploaded originals stay preserved')
    destination=remote.rstrip('/')+'/'+jid+'/'+source.name
    digest=hashlib.sha256()
    with source.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    rclone(['copyto',str(source),destination,'--immutable'])
    binary=shutil.which('rclone')
    with tempfile_file(engine.root) as temporary:
        rclone(['copyto',destination,str(temporary),'--immutable'])
        verify=hashlib.sha256()
        with temporary.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):verify.update(chunk)
        if verify.hexdigest()!=digest.hexdigest() or temporary.stat().st_size!=source.stat().st_size:raise ValueError('Drive readback checksum mismatch; local files preserved')
    with engine._connect() as con:con.execute('INSERT OR REPLACE INTO cleanup_receipts VALUES(?,?,?,?,?,?)',(jid,job['media_path'],destination,digest.hexdigest(),source.stat().st_size,utc_now()))
    return {'verified':True,'job_id':jid,'drive_path':destination,'sha256':digest.hexdigest(),'local_preserved_until_eligible':True}


@contextlib.contextmanager
def tempfile_file(root):
    import tempfile
    fd,name=tempfile.mkstemp(prefix='drive-verify-',dir=root);os.close(fd);path=Path(name);path.unlink()
    try:yield path
    finally:path.unlink(missing_ok=True)


def cleanup(engine,dry_run=True):
    from auto_stream_engine import utc_now
    cutoff=time.time()-12*3600;eligible=[];blocked=[]
    with engine._connect() as con:
        receipts=[dict(r) for r in con.execute('SELECT * FROM cleanup_receipts')]
        jobs=[dict(r) for r in con.execute('SELECT id,media_path,status FROM jobs')]
        config=con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='studio_config'").fetchone()
        row=con.execute('SELECT document FROM studio_config WHERE id=1').fetchone() if config else None
        document=row[0] if row else ''
    for receipt in receipts:
        relative=receipt['relative_path'];path=engine.media/relative
        folder=path.parent
        if not path.exists():continue
        if path.is_symlink() or folder.is_symlink() or not path.resolve().is_relative_to((engine.media/'auto-stream'/'renders').resolve()):continue
        if path.stat().st_mtime>cutoff:continue
        dependent=[j for j in jobs if j['media_path']==relative and (j['id']!=receipt['job_id'] or j['status'] not in ('COMPLETED','STOPPED'))]
        if dependent or relative in document:
            blocked.append({'media_path':relative,'reason':'Referenced by another job or saved scene'});continue
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        if digest.hexdigest()!=receipt['sha256'] or path.stat().st_size!=receipt['bytes']:
            blocked.append({'media_path':relative,'reason':'Local file changed since Drive verification'});continue
        eligible.append(relative)
        if not dry_run:
            # Only this renderer-created directory, never general project roots.
            shutil.rmtree(folder)
            with engine._connect() as con:con.execute("INSERT INTO cleanup_runs(job_id,state,message,created_at) VALUES(?,'CLEANED',?,?)",(receipt['job_id'],'Verified render artifacts removed',utc_now()))
    # Thumbnails are reproducible and never inputs to running encoders.
    previews=engine.root/'previews'
    if previews.exists():
        for folder in previews.iterdir():
            if folder.is_dir() and not folder.is_symlink() and folder.stat().st_mtime<cutoff:
                if not dry_run:shutil.rmtree(folder)
    return {'dry_run':dry_run,'eligible':eligible,'blocked':blocked,'owner':'auto-stream','shared_assets_preserved':True}


def worker():
    from auto_stream_engine import service
    engine=service();last_cleanup=0
    while not _stop.wait(5):
        try:engine.tick()
        except Exception:pass  # individual job history holds failures; never log credentials
        if time.monotonic()-last_cleanup>3600:
            try:
                preserve_completed(engine)
                cleanup(engine,dry_run=False)
            except Exception:pass
            last_cleanup=time.monotonic()


def startup():
    global _thread,_lease_fd
    if _thread and _thread.is_alive():return
    import fcntl
    from auto_stream_engine import ENGINE_ROOT
    ENGINE_ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    _lease_fd=os.open(ENGINE_ROOT/'worker.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(_lease_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        os.close(_lease_fd);_lease_fd=None
        raise RuntimeError('Auto Stream requires one supervisor worker per data directory') from None
    _stop.clear();_thread=threading.Thread(target=worker,daemon=True,name='auto-stream-scheduler');_thread.start()


def shutdown():
    global _lease_fd
    from auto_stream_engine import service
    from auto_stream_compositor import _processes,_process_lock
    import signal
    _stop.set();engine=service()
    for jid in list(engine.processes):
        with contextlib.suppress(Exception):engine.stop(jid)
    with _process_lock:
        for proc in _processes.values():
            with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGTERM)
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):os.killpg(proc.pid,signal.SIGKILL)
    if _thread:_thread.join(timeout=6)
    for jid,proc in list(engine.processes.items()):engine._terminate(jid,proc)
    if _lease_fd is not None:
        os.close(_lease_fd);_lease_fd=None


@router.get('/settings')
def get_settings(request:Request):
    from auto_stream_client_config import require_admin
    require_admin(request);return settings()


@router.put('/settings')
def set_settings(request:Request,payload:dict=Body(...)):
    from auto_stream_engine import service,translate
    from auto_stream_client_config import require_admin
    require_admin(request,write=True)
    try:
        if set(payload)-set(FLAGS) or any(not isinstance(v,bool) for v in payload.values()):raise ValueError('Only boolean streaming settings are supported')
        current=settings()['flags'];current.update(payload)
        if current['AUTO_STREAM_SCHEDULER_ENABLED'] and not current['AUTO_STREAM_PUBLISH_ENABLED']:raise ValueError('Enable encoder publishing before enabling scheduling')
        engine=service();path=engine.root/'operator-settings.json';temp=path.with_suffix('.tmp')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:json.dump(current,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        for flag,value in current.items():os.environ[flag]='1' if value else '0'
        return settings()
    except Exception as exc:translate(exc)


@router.get('/storage')
def storage_status():
    from auto_stream_engine import service
    engine=service()
    with engine._connect() as con:
        receipts=[dict(r) for r in con.execute('SELECT job_id,relative_path,drive_path,bytes,verified_at FROM cleanup_receipts')]
        runs=[dict(r) for r in con.execute('SELECT * FROM cleanup_runs ORDER BY id DESC LIMIT 30')]
    return {'policy':'12-hour, verified Drive readback, no active/shared dependencies','receipts':receipts,'recent_cleanup':runs,'settings':settings(),'policy':storage_policy(engine)}


@router.post('/jobs/{job_id}/preserve')
def preserve_job(job_id:str,request:Request,payload:dict=Body(...)):
    from auto_stream_client_config import require_admin
    from auto_stream_engine import service,translate
    require_admin(request,write=True)
    try:return preserve(service(),job_id,payload.get('drive_folder'))
    except Exception as exc:translate(exc)


@router.post('/storage/cleanup')
def run_cleanup(request:Request,payload:dict=Body(...)):
    from auto_stream_client_config import require_admin
    from auto_stream_engine import service,translate
    require_admin(request,write=True)
    try:return cleanup(service(),dry_run=payload.get('dry_run',True) is not False)
    except Exception as exc:translate(exc)


@router.put('/storage/policy')
def set_storage_policy(request:Request,payload:dict=Body(...)):
    from auto_stream_client_config import require_admin
    from auto_stream_engine import service,translate
    require_admin(request,write=True)
    try:
        folder=str(payload.get('drive_folder','')).strip()
        if folder and (not folder.startswith('gdrive:') or any(v in folder for v in ('..','\n','\r'))):raise ValueError('Use a canonical gdrive: folder or leave blank to preserve local media')
        path=service().root/'storage-policy.json';temporary=path.with_suffix('.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:json.dump({'drive_folder':folder},stream);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        return {'drive_folder':folder,'auto_preserve_completed':bool(folder),'cleanup_after_hours':12}
    except Exception as exc:translate(exc)


def storage_policy(engine):
    path=engine.root/'storage-policy.json'
    try:return json.loads(path.read_text()) if not path.is_symlink() else {}
    except (OSError,ValueError):return {}


def preserve_completed(engine):
    policy=storage_policy(engine);folder=policy.get('drive_folder','')
    if not folder:return
    with engine._connect() as con:
        jobs=[dict(r) for r in con.execute("SELECT id,media_path FROM jobs WHERE status IN ('COMPLETED','STOPPED') AND id NOT IN (SELECT job_id FROM cleanup_receipts) ORDER BY updated_at LIMIT 5")]
    for job in jobs:
        if not job['media_path'].startswith('auto-stream/renders/'):continue
        try:preserve(engine,job['id'],folder)
        except Exception:
            from auto_stream_engine import utc_now
            with engine._connect() as con:con.execute("INSERT INTO cleanup_runs(job_id,state,message,created_at) VALUES(?,'PRESERVED_LOCAL','Drive preservation unavailable; local source retained',?)",(job['id'],utc_now()))

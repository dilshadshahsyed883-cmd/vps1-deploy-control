"""Loopback-only backend; trusted Nginx overwrites identity headers from Scene SSO."""
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid
from fastapi import FastAPI, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from .store import Store, ACTIVE, TERMINAL
from .media import IMAGES, AUDIO, FORMATS, audio_duration, output_name, run
from .drive import CONFIG, Drive, folder_id, enqueue_pair
from .worker import capacity


def create_app(store=None):
    store = store or Store()
    app = FastAPI(title='Song Maker', version='1.0.0')
    assets = Path(__file__).parent / 'static'
    app.mount('/song-maker/assets', StaticFiles(directory=assets), name='assets')

    def principal(request: Request):
        user = request.headers.get('x-auth-user', '').strip()
        if not user:
            raise HTTPException(401, 'Authentication required')
        if request.method not in {'GET', 'HEAD'}:
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                raise HTTPException(403, 'Cross-origin request denied')
            if request.headers.get('x-song-maker') != '1':
                raise HTTPException(403, 'Same-origin application request required')
        return {'owner': user, 'admin': request.headers.get('x-is-admin') == '1'}

    def owned(job_id, user):
        job = store.get(job_id)
        if not job or job['owner'] != user['owner'] or job['deleted']:
            raise HTTPException(404, 'Job not found')
        return job

    def public(job):
        return {k: v for k, v in job.items() if k not in {'source', 'destination', 'source_key'}}

    @app.middleware('http')
    async def private_responses(request, call_next):
        length = request.headers.get('content-length', '0')
        try:
            if int(length) > 1100*1024*1024:
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail': 'Request exceeds upload limit'}, status_code=413)
        except ValueError:
            raise HTTPException(400, 'Invalid content length')
        response = await call_next(request)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/health')
    @app.get('/song-maker/health')
    def health():
        with store.db() as db:
            workers = {r['lane']: time.time()-r['heartbeat'] < 30 for r in db.execute('SELECT * FROM workers')}
        return {'ok': True, 'app': 'song-maker', 'workers': workers, 'auto_script': 'OFFLINE'}

    @app.get('/song-maker/', response_class=HTMLResponse)
    def index(user=Depends(principal)):
        return (assets / 'index.html').read_text()

    @app.get('/song-maker/api/state')
    def state(user=Depends(principal)):
        settings = store.record('settings', user['owner'])
        return {'jobs': [public(j) for j in store.jobs(user['owner'])], 'settings': settings,
                'sync': store.record('sync', user['owner']), 'admin': user['admin']}

    @app.get('/song-maker/api/profiles')
    def profiles(user=Depends(principal)):
        if not user['admin']:
            raise HTTPException(403, 'Administrator required')
        # Names only; never return or parse credential values.
        try:
            names = run(['rclone', 'listremotes', '--config', CONFIG], timeout=30)
            return [n.rstrip(':') for n in names.splitlines() if n.endswith(':')]
        except Exception:
            raise HTTPException(503, 'Existing Drive profiles unavailable') from None

    @app.post('/song-maker/api/settings')
    async def settings(request: Request, user=Depends(principal)):
        if not user['admin']:
            raise HTTPException(403, 'Administrator required')
        data = await request.json()
        try:
            clean = {key: folder_id(str(data[key])) for key in ('image_folder', 'audio_folder', 'output_folder')}
            if len(set(clean.values())) != 3:
                raise ValueError('Input and output folders must be distinct')
            clean.update(remote=str(data['remote']), auto_sync=bool(data.get('auto_sync')),
                         resolution=data.get('resolution', '1080p'), fit=data.get('fit', 'fit'))
            if clean['resolution'] not in FORMATS or clean['fit'] not in {'fit', 'blur'}:
                raise ValueError('Unsupported render options')
            Drive(clean)
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc))
        # Each Drive profile settings row is owned by the authenticated administrator.
        # Shared server credentials never grant another user access to that owner's files/jobs.
        store.record('settings', user['owner'], clean)
        store.record('settings', '__default__', {**clean, 'auto_sync': False})
        return {'saved': True}

    @app.post('/song-maker/api/sync')
    def sync(user=Depends(principal)):
        if not user['admin']:
            raise HTTPException(403, 'Administrator required')
        settings = store.record('settings', user['owner'])
        if not settings:
            raise HTTPException(409, 'Select Drive folders first')
        settings['sync_requested'] = True
        store.record('settings', user['owner'], settings)
        return {'queued': True}

    @app.post('/song-maker/api/pairs/select')
    async def select_pair(request: Request, user=Depends(principal)):
        if not user['admin']:
            raise HTTPException(403, 'Administrator required')
        data = await request.json()
        snapshot = store.record('sync', user['owner'])
        matches = [p for p in snapshot.get('pairs', []) if p['key'] == data.get('key')]
        if len(matches) != 1:
            raise HTTPException(409, 'Pair changed; sync again')
        group = matches[0]
        images = [x for x in group['images'] if x.get('ID') == data.get('image_id')]
        audio = [x for x in group['audio'] if x.get('ID') == data.get('audio_id')]
        if len(images) != 1 or len(audio) != 1:
            raise HTTPException(409, 'Select exactly one image and one audio')
        job = enqueue_pair(store, user['owner'], store.record('settings', user['owner']), images[0], audio[0])
        return public(job)

    @app.post('/song-maker/api/jobs')
    async def submit(request: Request, image: UploadFile = File(...), audio: UploadFile = File(...),
                     resolution: str = Form('1080p'), fit: str = Form('fit'), user=Depends(principal)):
        form = await request.form()
        if len(form.getlist('image')) != 1 or len(form.getlist('audio')) != 1:
            raise HTTPException(422, 'Exactly one image and one audio required')
        image_ext, audio_ext = Path(image.filename or '').suffix.lower(), Path(audio.filename or '').suffix.lower()
        if image_ext not in IMAGES or audio_ext not in AUDIO or resolution not in FORMATS or fit not in {'fit', 'blur'}:
            raise HTTPException(422, 'Select one supported image, one audio file and valid render options')
        capacity(store.root, 512*1024*1024)
        token = uuid.uuid4().hex
        staging = store.root / 'staging' / token
        staging.mkdir()
        hashes = {}
        try:
            for key, upload, extension, maximum in [('image', image, image_ext, 50*1024*1024),
                                                    ('audio', audio, audio_ext, 1024*1024*1024)]:
                size, hasher = 0, hashlib.sha256()
                with (staging / (key+extension)).open('wb') as stream:
                    while chunk := await upload.read(1024*1024):
                        size += len(chunk)
                        if size > maximum:
                            raise HTTPException(413, key+' exceeds upload limit')
                        capacity(store.root, len(chunk))
                        hasher.update(chunk)
                        stream.write(chunk)
                hashes[key] = hasher.hexdigest()
            from PIL import Image
            with Image.open(staging / ('image'+image_ext)) as check:
                check.verify()
            duration = audio_duration(staging / ('audio'+audio_ext))
            source = dict(hashes, image_name=image.filename, audio_name=audio.filename)
            # Lock publishing inputs with API requests so workers can never claim a half-published job.
            with (store.root / 'state/submission.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                job = store.enqueue(user['owner'], source, {
                    'project': Path(audio.filename).stem, 'image_name': image.filename, 'audio_name': audio.filename,
                    'image_ext': image_ext, 'audio_ext': audio_ext, 'duration': duration,
                    'local_name': output_name(audio.filename), 'origin': 'manual',
                    'resolution': resolution, 'fit': fit, 'drive_status': 'Pending'})
                destination = store.root / 'inputs' / job['id']
                if not destination.exists():
                    staging.rename(destination)
            return public(job)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422, 'Invalid media input') from None
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    @app.post('/song-maker/api/jobs/{job_id}/{action}')
    def action(job_id: str, action: str, user=Depends(principal)):
        job = owned(job_id, user)
        if action == 'cancel':
            if job['stage'] == 'Completed':
                raise HTTPException(409, 'Completed job cannot be cancelled')
            store.update(job_id, cancel=1, **({'stage': 'Cancelled'} if not job['lane'] else {}))
        elif action == 'retry':
            if job['stage'] not in TERMINAL or job['stage'] == 'Completed' or job['lane']:
                raise HTTPException(409, 'Job is not retryable')
            store.update(job_id, stage='Ready to Upload' if job.get('valid') else 'Queued',
                         cancel=0, error='', drive_status='Pending')
        elif action == 'delete':
            if job['lane'] or job['stage'] not in TERMINAL:
                raise HTTPException(409, 'Cancel the job and wait for the worker before deleting')
            # Tombstone preserves pair idempotency, removes owned local files only.
            store.update(job_id, deleted=1)
            for category in ('inputs', 'temp', 'outputs', 'staging', 'logs'):
                path = store.root / category / job_id
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
        else:
            raise HTTPException(404, 'Unknown action')
        return {'ok': True}

    @app.get('/song-maker/api/jobs/{job_id}/video')
    def video(job_id: str, download: bool = False, user=Depends(principal)):
        job = owned(job_id, user)
        if not job.get('valid'):
            raise HTTPException(409, 'Video is not ready')
        path = store.root / 'outputs' / job_id / job['local_name']
        if not path.is_file():
            raise HTTPException(404, 'Local video unavailable')
        return FileResponse(path, media_type='video/mp4', filename=job.get('remote_name') or job['local_name'],
                            content_disposition_type='attachment' if download else 'inline')

    return app


app = create_app()

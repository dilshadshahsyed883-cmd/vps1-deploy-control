"""Loopback-only backend; trusted Nginx overwrites identity headers from Scene SSO."""
import fcntl
import asyncio
import hashlib
import json
import re
from pathlib import Path
import shutil
import time
import uuid
from fastapi import FastAPI, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from .store import Store, ACTIVE, TERMINAL
from .media import IMAGES, AUDIO, FORMATS, QUALITIES, audio_duration, output_name, run, validate_overlay
from .drive import CONFIG, Drive, folder_ref, default_settings, enqueue_pair, archive_eligible, DEFAULT_INPUT, DEFAULT_OUTPUT
from .worker import capacity
from .folders import Folders


def create_app(store=None):
    store = store or Store()
    folders = Folders(store)
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

    def owned(job_id, user, allow_reserved_deleted=False):
        job = store.get(job_id)
        if not job or job['owner'] != user['owner']:
            raise HTTPException(404, 'Job not found')
        if job['deleted']:
            if not allow_reserved_deleted or not job.get('song_folder_id'):
                raise HTTPException(404, 'Job not found')
            # An importer may already have reserved the video before deletion.
            # Let that same account finish fetching the original pair; never
            # restore the deleted project to queue or folder discovery.
            with store.db() as db:
                pending = db.execute(
                    """SELECT 1 FROM song_folder_media m
                       JOIN song_folder_refs r ON r.job_id=m.job_id
                       WHERE m.job_id=? AND m.folder_id=? AND m.consumed=0
                         AND r.youtube_id='' LIMIT 1""",
                    (job_id, job['song_folder_id'])).fetchone()
            if not pending:
                raise HTTPException(404, 'Job not found')
        return job

    def public(job):
        return {k: v for k, v in job.items() if k not in {'source', 'destination', 'source_key'}}

    upload_lock = asyncio.Lock()

    @app.middleware('http')
    async def private_responses(request, call_next):
        length = request.headers.get('content-length', '0')
        try:
            if int(length) > 1100*1024*1024:
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail': 'Request exceeds upload limit'}, status_code=413)
        except ValueError:
            raise HTTPException(400, 'Invalid content length')
        if request.method == 'POST' and request.url.path == '/song-maker/api/jobs':
            # Admit only one multipart parser at a time, before temporary upload
            # spooling. Reserve the complete declared request above the disk floor.
            async with upload_lock:
                try:
                    capacity(store.root, int(length) or 1100*1024*1024)
                except RuntimeError as exc:
                    from fastapi.responses import JSONResponse
                    return JSONResponse({'detail': str(exc)}, status_code=507)
                response = await call_next(request)
        else:
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
        settings = store.record('settings', user['owner']) or default_settings()
        exports = {'resolution': settings.get('resolution', '1080p'),
                   'fit': settings.get('fit', 'fit'),
                   'quality': settings.get('quality', 'balanced'),
                   'overlay_enabled': settings.get('overlay_enabled', False),
                   'overlay_preset': settings.get('overlay_preset', 'light_leaks'),
                   'overlay_strength': settings.get('overlay_strength', 35),
                   'overlay_speed': settings.get('overlay_speed', 'normal'),
                   'auto_input_from_audio_downloader': False,
                   'send_to_song_uploader': False,
                   **store.record('exports', user['owner'])}
        return {'jobs': [public(j) for j in store.jobs(user['owner'])], 'settings': settings,
                'export': exports, 'sync_access': True,
                'sync': store.record('sync', user['owner']), 'admin': user['admin'],
                'queue_paused': store.paused(user['owner']), 'drive_retry_after': store.drive_after()}

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

    def owner_drive_root(owner):
        # Private namespace on the admin-managed shared remote. No user-provided path segment.
        return 'Song Studio/users/' + hashlib.sha256(owner.encode('utf-8')).hexdigest()[:24]

    def drive_choices(owner):
        with store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS song_drive_folders
                (owner TEXT NOT NULL, path TEXT NOT NULL,
                 PRIMARY KEY(owner, path))''')
            rows = db.execute('SELECT path FROM song_drive_folders WHERE owner=? ORDER BY path',
                              (owner,)).fetchall()
        return [DEFAULT_INPUT, DEFAULT_OUTPUT] + [row['path'] for row in rows]

    @app.get('/song-maker/api/drive/folders')
    def drive_folders(user=Depends(principal)):
        return {'folders': drive_choices(user['owner']),
                'shared_config': True, 'owner_root': owner_drive_root(user['owner'])}

    @app.post('/song-maker/api/drive/folders')
    async def drive_folder_create(request: Request, user=Depends(principal)):
        body = await request.json()
        name = body.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _-]{0,63}', name):
            raise HTTPException(422, 'Folder name must be 1-64 safe characters')
        path = folder_ref(owner_drive_root(user['owner']) + '/' + name.strip())
        if path in drive_choices(user['owner']):
            return {'path': path, 'created': False}
        # Namespaced mkdir on a pinned admin-managed remote: credentials stay server-side.
        shared = store.record('settings', '__default__') or default_settings()
        remote = shared.get('remote') or default_settings()['remote']
        try:
            Drive({'remote': remote}).call(['mkdir', remote + ':' + path])
        except RuntimeError:
            raise HTTPException(503, 'Unable to create folder in the approved Drive root') from None
        with store.db() as db:
            db.execute('INSERT OR IGNORE INTO song_drive_folders(owner,path) VALUES(?,?)',
                       (user['owner'], path))
        return {'path': path, 'created': True}

    @app.post('/song-maker/api/settings')
    async def settings(request: Request, user=Depends(principal)):
        data = await request.json()
        try:
            clean = {key: folder_ref(str(data[key])) for key in ('image_folder', 'audio_folder', 'output_folder')}
            if not user['admin']:
                allowed = set(drive_choices(user['owner']))
                if any(value not in allowed for value in clean.values()):
                    raise ValueError('Select an approved or personally created Drive folder')

            if clean['output_folder'] in {clean['image_folder'], clean['audio_folder']}:
                raise ValueError('Output folder must differ from the input folder')
            shared = store.record('settings', '__default__') or default_settings()
            remote = str(data['remote']) if user['admin'] else shared.get('remote', default_settings()['remote'])
            clean.update(remote=remote, auto_sync=bool(data.get('auto_sync')),
                         resolution=data.get('resolution', '1080p'), fit=data.get('fit', 'fit'))
            if clean['resolution'] not in FORMATS or clean['fit'] not in {'fit', 'blur'}:
                raise ValueError('Unsupported render options')
            interval = int(data.get('sync_interval', 300))
            if not 60 <= interval <= 3600:
                raise ValueError('Sync interval must be between 60 and 3600 seconds')
            clean['sync_interval'] = interval
            Drive(clean)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, str(exc))
        # Never reset an in-flight sync request or any existing per-user preference.
        previous = store.record('settings', user['owner'])
        store.record('settings', user['owner'], {**previous, **clean})
        if user['admin']:
            store.record('settings', '__default__', {**clean, 'auto_sync': False})
        return {'saved': True}

    @app.post('/song-maker/api/export')
    async def save_export(request: Request, user=Depends(principal)):
        # Personal output presets never change credentials or other users' jobs.
        data = await request.json()
        resolution, fit, quality = data.get('resolution'), data.get('fit'), data.get('quality', 'balanced')
        if resolution not in FORMATS or fit not in {'fit', 'blur'} or quality not in QUALITIES:
            raise HTTPException(422, 'Unsupported export settings')
        try:
            overlays = validate_overlay(data.get('overlay_enabled', False),
                                        data.get('overlay_preset', 'light_leaks'),
                                        data.get('overlay_strength', 35),
                                        data.get('overlay_speed', 'normal'))
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None
        for option in ('auto_input_from_audio_downloader', 'send_to_song_uploader'):
            if type(data.get(option, False)) is not bool:
                raise HTTPException(422, option + ' must be a checkbox value')
        store.record('exports', user['owner'], {
            'resolution': resolution, 'fit': fit, 'quality': quality, **overlays,
            'auto_input_from_audio_downloader': data.get('auto_input_from_audio_downloader', False),
            'send_to_song_uploader': data.get('send_to_song_uploader', False)})
        return {'saved': True}

    @app.post('/song-maker/api/sync')
    def sync(user=Depends(principal)):
        # Sync Now sets only the request bit. In particular it cannot replace a
        # non-admin user's saved folders, Auto Sync, or exported video settings.
        settings = store.record('settings', user['owner']) or default_settings()
        store.record('settings', user['owner'], {**settings, 'sync_requested': True})
        return {'queued': True}

    @app.post('/song-maker/api/pairs/select')
    async def select_pair(request: Request, user=Depends(principal)):
        # Resolve conflicts strictly from this user's own saved sync snapshot.
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
        settings = store.record('settings', user['owner']) or default_settings()
        export = store.record('exports', user['owner'])
        settings.update({key: export[key] for key in ('resolution', 'fit', 'quality', 'overlay_enabled', 'overlay_preset', 'overlay_strength', 'overlay_speed', 'send_to_song_uploader') if key in export})
        job = enqueue_pair(store, user['owner'], settings, images[0], audio[0])
        return public(job)

    @app.get('/song-maker/api/folders')
    def folder_list(user=Depends(principal)):
        return {'folders':folders.list(user['owner'])}

    @app.get('/song-maker/api/manual-folder')
    def manual_folder(user=Depends(principal)):
        return {'song_folder_id':store.record('intake',user['owner']).get('song_folder_id','')}

    @app.put('/song-maker/api/manual-folder')
    async def save_manual_folder(request:Request,user=Depends(principal)):
        data=await request.json()
        folder_id=str(data.get('song_folder_id') or '')
        if folder_id:
            try: folders.get(user['owner'],folder_id)
            except ValueError as exc: raise HTTPException(422,str(exc))
        store.record('intake',user['owner'],{'song_folder_id':folder_id})
        return {'song_folder_id':folder_id}

    @app.get('/song-maker/api/folders/{folder_id}')
    def folder_get(folder_id:str,user=Depends(principal)):
        try: return folders.get(user['owner'],folder_id)
        except ValueError as exc: raise HTTPException(404,str(exc))

    @app.post('/song-maker/api/folders')
    async def folder_create(request:Request,user=Depends(principal)):
        try: return folders.create(user['owner'],(await request.json()).get('name'))
        except ValueError as exc: raise HTTPException(422,str(exc))

    @app.get('/song-maker/api/folders/{folder_id}/outputs')
    def folder_outputs(folder_id:str,user=Depends(principal)):
        try: return {'outputs':folders.outputs(user['owner'],folder_id)}
        except ValueError as exc: raise HTTPException(404,str(exc))

    def internal_user(request:Request):
        # Outside the public /song-maker/ Nginx prefix. Never accept proxied browser requests.
        if (not request.client or request.client.host not in {'127.0.0.1','::1'}
                or request.headers.get('origin') or request.headers.get('x-forwarded-for')):
            raise HTTPException(403,'Internal Song Studio transport required')
        return principal(request)

    @app.post('/internal/song-folders/{folder_id}/{job_id}/{action}')
    async def folder_transfer(folder_id:str,job_id:str,action:str,request:Request,user=Depends(internal_user)):
        body=await request.json()
        try:
            if action=='reserve': return folders.reserve(user['owner'],folder_id,job_id,str(body.get('consumer') or ''))
            if action=='consume': return folders.finish(user['owner'],folder_id,job_id,
                str(body.get('consumer') or ''),str(body.get('youtube_id') or ''))
            raise HTTPException(404,'Unknown transfer action')
        except ValueError as exc: raise HTTPException(409,str(exc))

    @app.post('/song-maker/api/jobs')
    async def submit(request: Request, image: UploadFile = File(...), audio: UploadFile = File(...),
                     resolution: str = Form(''), fit: str = Form(''), quality: str = Form(''), song_folder_id: str = Form(''), user=Depends(principal)):
        exports = store.record('exports', user['owner'])
        downloader_handoff = request.headers.get('x-audio-downloader-handoff') == '1'
        if song_folder_id:
            try: folders.get(user['owner'],song_folder_id)
            except ValueError as exc: raise HTTPException(422,str(exc))
        if (downloader_handoff and not song_folder_id
                and not exports.get('auto_input_from_audio_downloader', False)):
            raise HTTPException(409, 'Enable Auto Input from Audio Downloader in Song Maker Settings')
        settings = store.record('settings', user['owner']) or default_settings()
        resolution = resolution or exports.get('resolution', settings.get('resolution', '1080p'))
        fit = fit or exports.get('fit', settings.get('fit', 'fit'))
        quality = quality or exports.get('quality', settings.get('quality', 'balanced'))
        overlays = validate_overlay(exports.get('overlay_enabled', False),
                                    exports.get('overlay_preset', 'light_leaks'),
                                    exports.get('overlay_strength', 35),
                                    exports.get('overlay_speed', 'normal'))
        form = await request.form()
        if len(form.getlist('image')) != 1 or len(form.getlist('audio')) != 1:
            raise HTTPException(422, 'Exactly one image and one audio required')
        image_ext, audio_ext = Path(image.filename or '').suffix.lower(), Path(audio.filename or '').suffix.lower()
        if image_ext not in IMAGES or audio_ext not in AUDIO or resolution not in FORMATS or fit not in {'fit', 'blur'} or quality not in QUALITIES:
            raise HTTPException(422, 'Select one supported image, one audio file and valid render options')
        if downloader_handoff and (image_ext != '.jpg'
                or Path(image.filename).stem != Path(audio.filename).stem):
            raise HTTPException(422, 'Audio Downloader handoff requires the original matching Video ID JPEG')
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
            # Provenance is part of idempotency: a manual upload must not claim a downloader job.
            source = dict(hashes, image_name=image.filename, audio_name=audio.filename,
                          downloader_handoff=downloader_handoff, song_folder_id=song_folder_id)
            # Lock publishing inputs with API requests so workers can never claim a half-published job.
            with (store.root / 'state/submission.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                job = store.enqueue(user['owner'], source, {
                    'project': Path(audio.filename).stem, 'image_name': image.filename, 'audio_name': audio.filename,
                    'image_ext': image_ext, 'audio_ext': audio_ext, 'duration': duration,
                    'local_name': output_name(audio.filename), 'origin': 'manual',
                    'audio_downloader_handoff': downloader_handoff,
                    'song_folder_id': song_folder_id,
                    'resolution': resolution, 'fit': fit, 'quality': quality, **overlays,
                     'send_to_song_uploader': bool(song_folder_id) or bool(exports.get('send_to_song_uploader', False)),
                     'drive_status': 'Pending'})
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

    @app.post('/song-maker/api/queue/{command}')
    def queue_control(command: str, user=Depends(principal)):
        if command not in {'pause', 'resume'}:
            raise HTTPException(404, 'Unknown queue control')
        return {'paused': store.paused(user['owner'], command == 'pause')}

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
            # A rendered Drive job that has not archived its input must resume
            # the render lane to complete the safe /used move (without rerender).
            archived = not archive_eligible(job) or job.get('archive_status') == 'Archived'
            store.update(job_id, stage='Ready to Upload' if job.get('valid') and archived and not job.get('send_to_song_uploader') else 'Queued',
                         cancel=0, error='', drive_status='Pending', next_retry_at=None, retry_lane='')
        elif action == 'delete':
            if job['lane'] or job['stage'] not in TERMINAL:
                raise HTTPException(409, 'Cancel the job and wait for the worker before deleting')
            # Queue deletion is immediate. Keep original files only while an
            # already-reserved Song Uploader import/upload needs them.
            retained = False
            if job.get('song_folder_id'):
                try:
                    retained = folders.dismiss(job['owner'], job['song_folder_id'], job_id)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from None
            else:
                store.update(job_id, deleted=1)
            for category in ('inputs', 'temp', 'outputs', 'staging', 'logs'):
                if retained and category in {'inputs', 'outputs'}:
                    continue
                path = store.root / category / job_id
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
            return {'ok': True, 'media_retained': retained}
        else:
            raise HTTPException(404, 'Unknown action')
        return {'ok': True}

    @app.get('/song-maker/api/uploader/outputs')
    def uploader_outputs(user=Depends(principal)):
        # Owner-scoped SSO library; return no absolute filesystem paths.
        ready = []
        for job in store.jobs(user['owner']):
            if not (job.get('send_to_song_uploader') and job['stage'] == 'Completed'
                    and job.get('valid')):
                continue
            folder = store.root / 'outputs' / job['id']
            video = folder / job['local_name']
            thumb_name = Path(job['local_name']).stem + '.jpg'
            if not video.is_file() or not (folder / thumb_name).is_file():
                continue
            ready.append({'id': job['id'], 'folder_id':job.get('song_folder_id',''), 'name': job['local_name'],
                          'size': video.stat().st_size, 'created': job['created'],
                          'video_url': '/song-maker/api/jobs/' + job['id'] + '/video',
                          'thumbnail_url': '/song-maker/api/uploader/' + job['id'] + '/thumbnail'})
        return {'outputs': ready}

    @app.get('/song-maker/api/uploader/{job_id}/thumbnail')
    def uploader_thumbnail(job_id: str, user=Depends(principal)):
        job = owned(job_id, user, allow_reserved_deleted=True)
        if not job.get('send_to_song_uploader') or job['stage'] != 'Completed':
            raise HTTPException(404, 'Local uploader thumbnail unavailable')
        name = Path(job['local_name']).stem + '.jpg'
        path = store.root / 'outputs' / job_id / name
        if not path.is_file():
            raise HTTPException(404, 'Local uploader thumbnail unavailable')
        return FileResponse(path, media_type='image/jpeg', filename=name)

    @app.get('/song-maker/api/jobs/{job_id}/video')
    def video(job_id: str, download: bool = False, user=Depends(principal)):
        job = owned(job_id, user, allow_reserved_deleted=True)
        if not job.get('valid'):
            raise HTTPException(409, 'Video is not ready')
        path = store.root / 'outputs' / job_id / job['local_name']
        if not path.is_file():
            raise HTTPException(404, 'Local video unavailable')
        return FileResponse(path, media_type='video/mp4', filename=job.get('remote_name') or job['local_name'],
                            content_disposition_type='attachment' if download else 'inline')

    return app


app = create_app()

import argparse
import fcntl
import json
import logging
from pathlib import Path
import shutil
import signal
import threading
import time
from .store import Store
from .media import Cancelled, render, validate, audio_duration, digest
from .drive import Drive, sync_owner, default_settings, archive_eligible


def capacity(root, required):
    if shutil.disk_usage(root).free < 5_000_000_000 + required:
        raise RuntimeError('Insufficient free space above the 5 GB operational reserve')


def cleanup(store, job):
    # Inputs imported for Drive jobs are disposable copies. Manual originals retained.
    # Final MP4 always retained for preview/download until explicit user deletion.
    if job['stage'] != 'Completed' or not job.get('verified_at') or not job.get('remote_id'):
        return False
    for category in ('temp', 'staging') + (('inputs',) if job.get('origin') == 'drive' else ()):
        folder = store.root / category / job['id']
        if folder.is_symlink():
            raise ValueError('Unsafe cleanup path')
        if folder.exists():
            shutil.rmtree(folder)
    store.update(job['id'], cleaned=True)
    return True


def process(store, job, lane, shutdown=lambda: False, drive_factory=Drive):
    job_id = job['id']
    def cancelled():
        return shutdown() or bool(store.get(job_id)['cancel'])
    try:
        if cancelled():
            raise Cancelled('Cancelled')
        if lane == 'render':
            inputs = store.root / 'inputs' / job_id
            temp = store.root / 'temp' / job_id
            outputs = store.root / 'outputs' / job_id
            for path in (inputs, temp, outputs):
                path.mkdir(exist_ok=True)
            image, audio = inputs / ('image'+job.get('image_ext', Path(job['image_name']).suffix.lower())), inputs / ('audio'+job.get('audio_ext', Path(job['audio_name']).suffix.lower()))
            if job.get('origin') == 'drive':
                source = job['source']
                image = inputs / ('image'+Path(job['image_name']).suffix.lower())
                audio = inputs / ('audio'+Path(job['audio_name']).suffix.lower())
                drive = drive_factory(job['destination'], cancelled)
                for kind, target in [('image', image), ('audio', audio)]:
                    item = source[kind]
                    capacity(store.root, max(0, item['Size'])*2)
                    if not target.exists() or target.stat().st_size != item['Size'] or (
                        (item.get('Hashes') or {}).get('MD5') and digest(target, 'md5') != item['Hashes']['MD5']):
                        drive.download(source[kind+'_folder'], item, target)
            duration = audio_duration(audio)
            capacity(store.root, int(duration*2_000_000)+image.stat().st_size*4)
            output = outputs / job['local_name']
            # A crash after rename but before DB commit must validate/reuse the MP4.
            if not output.exists():
                store.update(job_id, stage='Rendering', duration=duration, progress=5, error='')
                render(image, audio, output, temp, job.get('resolution', '1080p'), job.get('fit', 'fit'),
                       cancelled, lambda seconds: store.update(job_id, progress=min(84, 5+int(seconds/duration*79))))
            store.update(job_id, stage='Validating MP4', progress=86)
            result = validate(output, duration, cancelled)
            if cancelled():
                raise Cancelled('Cancelled')
            # Persist the fully validated MP4 before any remote input mutation.
            # Drive inputs move immediately after rendering; MP4 uploads later.
            if archive_eligible(job):
                store.update(job_id, stage='Archiving Inputs', progress=88,
                             archive_status='Moving', valid=True, **result)
                drive_factory(job['destination'], cancelled).archive_inputs(job, store)
            store.update(job_id, stage='Ready to Upload', progress=90, lane='',
                         valid=True, **result)
        else:
            settings = (job.get('destination') or store.record('settings', job['owner'])
                        or store.record('settings', '__default__') or default_settings())
            if not settings.get('output_folder'):
                store.update(job_id, stage='Waiting for Drive', lane='', error='Administrator must select Drive output folder')
                return
            # Pin destination before first external write; later settings edits cannot redirect a retry.
            if not job.get('destination'):
                store.update(job_id, destination=settings)
                job = store.get(job_id)
            if archive_eligible(job) and job.get('archive_status') != 'Archived':
                raise ValueError('Move verified inputs to used before uploading MP4')
            local = store.root / 'outputs' / job_id / job['local_name']
            if not job.get('valid') or digest(local, 'md5') != job.get('md5'):
                raise ValueError('Validated local MP4 missing or changed; no upload attempted')
            receipt = drive_factory(settings, cancelled).upload(job, store)
            # If cancel arrived after upload, persist receipt for idempotent retry/reconciliation.
            store.update(job_id, stage='Completed', progress=100, lane='', error='', **receipt)
            try:
                cleanup(store, store.get(job_id))
            except Exception as exc:
                store.update(job_id, cleanup_error=str(exc)[-1000:])
    except Cancelled:
        if shutdown() and not store.get(job_id)['cancel']:
            store.update(job_id, stage='Queued' if lane == 'render' else 'Ready to Upload', lane='')
        else:
            store.update(job_id, stage='Cancelled', lane='')
    except Exception as exc:
        archiving = (lane == 'render' and
                     store.get(job_id).get('stage') == 'Archiving Inputs')
        stage = ('Needs Attention' if archiving or isinstance(exc, ValueError)
                 else 'Waiting for Drive' if lane == 'upload' else 'Failed')
        update = {'stage': stage, 'lane': '', 'error': str(exc)[-2000:],
                  'drive_status': 'Upload failed' if lane == 'upload' else 'Pending'}
        if archiving:
            update['archive_status'] = 'Needs Attention'
        store.update(job_id, **update)
        logging.exception('Song Maker %s failed for %s', lane, job_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('lane', choices=['render', 'upload', 'sync'])
    args = parser.parse_args()
    store, stop = Store(), threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with (store.root / 'state' / (args.lane+'.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.recover(args.lane)
        while not stop.is_set():
            store.pulse(args.lane)
            if args.lane == 'sync':
                with store.db() as db:
                    owners = [(r['owner'], json.loads(r['data'])) for r in db.execute('SELECT * FROM settings')]
                for owner, settings in owners:
                    if owner == '__default__':
                        continue
                    previous = store.record('sync', owner)
                    due = settings.get('sync_requested') or (settings.get('auto_sync') and time.time()-previous.get('last_sync', 0) >= 60)
                    if due:
                        settings.pop('sync_requested', None)
                        store.record('settings', owner, settings)
                        try:
                            # Coordinate scanner admissions with guarded app updates,
                            # manual submissions and render worker claims.
                            with (store.root / 'state/submission.lock').open('a') as admission:
                                fcntl.flock(admission, fcntl.LOCK_EX)
                                sync_owner(store, owner, settings, Drive(settings, stop.is_set))
                        except Exception:
                            previous.update(last_sync=time.time(), error='Drive scan failed; check profile and folder access')
                            store.record('sync', owner, previous)
            else:
                with (store.root / 'state/submission.lock').open('a') as submission_lock:
                    fcntl.flock(submission_lock, fcntl.LOCK_EX)
                    job = store.claim(args.lane)
                if job:
                    # Periodic worker pulse stays alive during long commands.
                    pulse_stop = threading.Event()
                    def pulse():
                        while not pulse_stop.wait(5):
                            store.pulse(args.lane)
                    thread = threading.Thread(target=pulse, daemon=True)
                    thread.start()
                    try:
                        # Metadata is stored without redundant extension fields for Drive jobs.
                        from pathlib import Path
                        job.setdefault('image_ext', Path(job['image_name']).suffix.lower())
                        job.setdefault('audio_ext', Path(job['audio_name']).suffix.lower())
                        process(store, job, args.lane, stop.is_set)
                    finally:
                        pulse_stop.set()
                        thread.join()
                    continue
            stop.wait(1)


if __name__ == '__main__':
    main()

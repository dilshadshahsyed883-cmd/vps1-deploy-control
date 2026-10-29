"""Existing rclone credentials, ID-rooted folders, no input mutation."""
import json
import os
from pathlib import Path
import re
import time
from .media import fingerprint, pairs, run, digest, output_name

CONFIG = os.environ.get('RCLONE_CONFIG', '/home/ubuntu/.config/masterctl/rclone.conf')


def folder_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,200}', value or ''):
        raise ValueError('Choose a Google Drive folder ID')
    return value


class Drive:
    def __init__(self, settings, cancelled=lambda: False):
        self.settings, self.cancelled = settings, cancelled
        self.remote = settings.get('remote', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', self.remote):
            raise ValueError('Select an existing rclone Drive profile')

    def call(self, args, folder=None, timeout=300):
        command = ['rclone', *args, '--config', CONFIG, '--contimeout', '20s', '--timeout', '60s',
                   '--retries', '2', '--low-level-retries', '2']
        if folder:
            command += ['--drive-root-folder-id', folder_id(folder)]
        try:
            return run(command, self.cancelled, timeout=timeout)
        except RuntimeError:
            # rclone diagnostics may contain private paths/URLs. Never persist raw stderr.
            raise RuntimeError('Drive operation failed; check the selected profile and folder access') from None

    def listing(self, folder):
        return json.loads(self.call(['lsjson', self.remote+':', '--files-only', '--hash'], folder))

    def download(self, folder, item, target):
        if '/' in item['Name'] or '\\' in item['Name'] or item['Name'] in {'.', '..'}:
            raise ValueError('Drive source name contains path separators; rename the input safely')
        # Filename transport is permitted only while unique metadata/ID still matches.
        def current():
            candidates = [x for x in self.listing(folder) if x['Name'] == item['Name']]
            if len(candidates) != 1 or fingerprint(candidates[0]) != fingerprint(item):
                raise ValueError('Drive input changed or became ambiguous; sync and select the new pair')
        current()
        self.call(['copyto', self.remote+':'+item['Name'], str(target)], folder)
        current()
        if target.stat().st_size != item['Size']:
            raise ValueError('Drive download size mismatch')
        md5 = (item.get('Hashes') or {}).get('MD5')
        if md5 and digest(target, 'md5') != md5:
            raise ValueError('Drive download checksum mismatch')

    def allocate(self, name, folder):
        existing = {x['Name'].casefold() for x in self.listing(folder)}
        stem = Path(name).stem
        for version in range(1, 10000):
            candidate = name if version == 1 else f'{stem} ({version}).mp4'
            if candidate.casefold() not in existing:
                return candidate
        raise RuntimeError('Too many output name collisions')

    def upload(self, job, store):
        local = store.root / 'outputs' / job['id'] / job['local_name']
        destination = job['destination']
        folder = destination['output_folder']
        name = job.get('remote_name')
        if not name:
            name = self.allocate(job['local_name'], folder)
            store.update(job['id'], remote_name=name, drive_status='Uploading')
        # Reservation persisted before transfer: crash recovery reuses this path.
        # ignore-existing NEVER overwrites. A race becomes Needs Attention at checksum verification.
        self.call(['copyto', str(local), self.remote+':'+name, '--ignore-existing',
                   '--transfers', '1'], folder, timeout=max(300, local.stat().st_size//10000))
        store.update(job['id'], stage='Verifying Drive Upload', progress=97, drive_status='Verifying')
        items = [x for x in self.listing(folder) if x['Name'] == name]
        if len(items) != 1:
            raise ValueError('Remote output identity ambiguous or missing')
        item = items[0]
        if not item.get('ID') or item['Size'] != local.stat().st_size:
            raise ValueError('Remote output identity/size verification failed')
        if not (item.get('Hashes') or {}).get('MD5') or item['Hashes']['MD5'] != digest(local, 'md5'):
            raise ValueError('Remote output checksum verification failed; local MP4 retained')
        return {'remote_id': item['ID'], 'drive_link': 'https://drive.google.com/file/d/'+item['ID']+'/view',
                'remote_md5': item['Hashes']['MD5'], 'remote_name': name,
                'verified_at': time.time(), 'drive_status': 'Verified'}


def sync_owner(store, owner, settings, drive=None):
    drive = drive or Drive(settings)
    images = drive.listing(settings['image_folder'])
    audio = drive.listing(settings['audio_folder'])
    results = pairs(images, audio)
    count = 0
    for pair in results:
        if pair['stage'] != 'Matched':
            continue
        image, sound = pair['images'][0], pair['audio'][0]
        job = enqueue_pair(store, owner, settings, image, sound)
        pair['job_id'], pair['job_stage'] = job['id'], job['stage']
        count += 1
    summary = {'last_sync': time.time(), 'pairs': results, 'images': sum(len(p['images']) for p in results),
               'audio': sum(len(p['audio']) for p in results), 'matched': count, 'error': ''}
    store.record('sync', owner, summary)
    return summary


def enqueue_pair(store, owner, settings, image, sound):
    # Stable IDs + provider hashes/modtime/size version snapshots, independent of render settings.
    source = {'remote': settings['remote'], 'image_folder': settings['image_folder'],
              'audio_folder': settings['audio_folder'], 'image': fingerprint(image), 'audio': fingerprint(sound)}
    job = store.enqueue(owner, source, {
        'project': Path(sound['Name']).stem, 'image_name': image['Name'], 'audio_name': sound['Name'],
        'local_name': output_name(sound['Name']), 'source': source, 'destination': settings,
        'resolution': settings.get('resolution', '1080p'), 'fit': settings.get('fit', 'fit'),
        'drive_status': 'Pending', 'origin': 'drive'})
    return job

"""Existing rclone credentials, safe Drive folder IDs or relative paths, no input mutation."""
import json
import fcntl
import os
from pathlib import Path
import re
import time
from .media import fingerprint, pairs, run, digest, output_name, Cancelled

CONFIG = os.environ.get('RCLONE_CONFIG', '/home/ubuntu/.config/masterctl/rclone.conf')
DEFAULT_INPUT = 'Song Studio/Audio downloader'
DEFAULT_OUTPUT = 'Song Studio/song maker'


def default_settings():
    """New installations only: preserve explicit saved settings and old ID-based jobs."""
    return {'remote': os.environ.get('SONG_MAKER_DRIVE_REMOTE',
                                    os.environ.get('AUDIO_DOWNLOADER_DRIVE_REMOTE', 'gdrive')),
            'image_folder': DEFAULT_INPUT, 'audio_folder': DEFAULT_INPUT,
            'output_folder': DEFAULT_OUTPUT, 'auto_sync': False,
            'resolution': '1080p', 'fit': 'fit', 'quality': 'balanced', 'sync_interval': 300}


def folder_ref(value):
    """Support existing Drive folder IDs and scoped, relative named folders."""
    if '/' not in value:
        return folder_id(value)
    if len(value) > 220 or value.startswith('/') or value.endswith('/'):
        raise ValueError('Choose a safe relative Google Drive folder path')
    parts = value.split('/')
    if any(not re.fullmatch(r'[A-Za-z0-9 _.-]+', part) or part in {'.', '..'}
           for part in parts):
        raise ValueError('Choose a safe relative Google Drive folder path')
    return value


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
                   '--retries', '2', '--low-level-retries', '2',
                   '--tpslimit', '1', '--tpslimit-burst', '1']
        if folder:
            folder = folder_ref(folder)
            if '/' in folder:
                # Only default/new named paths use rclone's relative-path addressing.
                # Legacy ID-backed jobs still use --drive-root-folder-id unchanged.
                prefix = self.remote + ':'
                command = [prefix + folder + '/' + value[len(prefix):]
                           if value.startswith(prefix) else value for value in command]
            else:
                command += ['--drive-root-folder-id', folder]
        for attempt in range(3):
            try:
                return run(command, self.cancelled, timeout=timeout)
            except RuntimeError as exc:
                # Persist only a provider reason from an explicit allowlist,
                # never paths, tokens, URLs or raw rclone stderr.
                error = str(exc).lower()
                reasons = ('storageQuotaExceeded', 'dailyLimitExceeded', 'downloadQuotaExceeded',
                           'userRateLimitExceeded', 'rateLimitExceeded', 'quotaExceeded',
                           'invalid_grant', 'permission denied', 'read-only file system',
                           'directory not found', 'object not found', 'unknown flag', '429', 'rate limit')
                reason = next((r for r in reasons if r.lower() in error), 'unclassified')
                temporary = reason in {'userRateLimitExceeded', 'rateLimitExceeded',
                                       'quotaExceeded', '429', 'rate limit'}
                # Re-read metadata after a quiet window. Mutations are never
                # replayed here; their persisted reservations/receipts own recovery.
                if args[0] == 'lsjson' and temporary and attempt < 2:
                    for _ in range((10, 30)[attempt]):
                        if self.cancelled():
                            raise Cancelled('Cancelled during Drive rate-limit backoff')
                        time.sleep(1)
                    continue
                raise RuntimeError('Drive operation failed (' + reason +
                                   '); check profile, folder access or provider quota') from None

    def listing(self, folder):
        return json.loads(self.call(['lsjson', self.remote+':', '--files-only', '--hash'], folder))

    def stat(self, folder, name):
        """Query one output file after transfer instead of rescanning the entire folder.

        This is a fresh, non-cached ID/size/hash receipt. An absent or ambiguous
        provider response fails closed and never permits local cleanup.
        """
        if not name or name in {'.', '..'} or '/' in name or '\\' in name:
            raise ValueError('Unsafe Drive output filename')
        value = json.loads(self.call(
            ['lsjson', self.remote + ':' + name, '--stat', '--hash'], folder))
        if not isinstance(value, dict) or (value.get('Name') and value['Name'] != name):
            raise ValueError('Remote output identity ambiguous or missing')
        return value

    def lookup(self, folder, name):
        # Literal rclone filter: provider still supplies identity/hash metadata,
        # but unrelated folder entries are not returned or hashed.
        escaped = ''.join('\\' + c if c in '\\*?[]{}' else c for c in name)
        return json.loads(self.call(['lsjson', self.remote+':', '--files-only',
                                     '--hash', '--include', '/' + escaped], folder))

    def download(self, folder, item, target):
        if '/' in item['Name'] or '\\' in item['Name'] or item['Name'] in {'.', '..'}:
            raise ValueError('Drive source name contains path separators; rename the input safely')
        # Filename transport is permitted only while unique metadata/ID still matches.
        def current():
            candidates = [x for x in self.lookup(folder, item['Name']) if x['Name'] == item['Name']]
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
        if '/' in folder_ref(folder):
            # New output folder may not exist yet; never mutate the input folder.
            self.call(['mkdir', self.remote+':'], folder)
        name = job.get('remote_name')
        if not name:
            name = self.allocate(job['local_name'], folder)
            store.update(job['id'], remote_name=name, drive_status='Uploading')
        # Reservation persisted before transfer: crash recovery reuses this path.
        # ignore-existing NEVER overwrites. A race becomes Needs Attention at checksum verification.
        self.call(['copyto', str(local), self.remote+':'+name, '--ignore-existing',
                   '--transfers', '1'], folder, timeout=max(300, local.stat().st_size//10000))
        store.update(job['id'], stage='Verifying Drive Upload', progress=97, drive_status='Verifying')
        item = self.stat(folder, name)
        if not item.get('ID') or item['Size'] != local.stat().st_size:
            raise ValueError('Remote output identity/size verification failed')
        if not (item.get('Hashes') or {}).get('MD5') or item['Hashes']['MD5'] != digest(local, 'md5'):
            raise ValueError('Remote output checksum verification failed; local MP4 retained')
        return {'remote_id': item['ID'], 'drive_link': 'https://drive.google.com/file/d/'+item['ID']+'/view',
                'remote_md5': item['Hashes']['MD5'], 'remote_name': name,
                'verified_at': time.time(), 'drive_status': 'Verified'}


    def archive_inputs(self, job, store):
        """Archive rendered Drive inputs using per-step fresh folder snapshots.

        Read the source and /used folders once for each decision point. A move
        invalidates both snapshots; refresh both immediately afterwards. This
        replaces repeated full-folder scans without weakening identity checks or
        overwriting files, and remains resumable after an interrupted move.
        """
        if not archive_eligible(job):
            return {}
        source = job['source']
        folder = source['image_folder']
        used = folder + '/used'
        self.call(['mkdir', self.remote + ':' + used])
        receipts = dict(job.get('archive_moves') or {})
        reservations = dict(job.get('archive_names') or {})
        originals_snapshot = self.listing(folder)
        used_snapshot = self.listing(used)

        def matches(snapshot, name):
            return [x for x in snapshot if x['Name'] == name]

        for kind in ('image', 'audio'):
            item = source[kind]
            name = item.get('Name', '')
            ident = item.get('ID')
            if (not ident or not isinstance(name, str) or not name or
                    name in {'.', '..'} or '/' in name or '\\' in name):
                raise ValueError('Unsafe or unidentified Drive source; archive blocked')
            if kind in receipts:
                archived = matches(used_snapshot, receipts[kind]['name'])
                if len(archived) != 1 or not _same_identity(archived[0], item):
                    raise ValueError('Previously archived Drive source changed; archive blocked')
                if matches(originals_snapshot, name):
                    raise ValueError('Previously archived source unexpectedly still present')
                continue

            chosen = reservations.get(kind)
            if not chosen:
                already = [x for x in used_snapshot if x.get('ID') == ident]
                if already:
                    if len(already) != 1 or not _same_identity(already[0], item):
                        raise ValueError('Archived Drive identity collision')
                    chosen = already[0]['Name']
                else:
                    occupied = {x['Name'].casefold() for x in used_snapshot}
                    stem, ext = os.path.splitext(name)
                    for version in range(1, 10001):
                        candidate = name if version == 1 else f'{stem} ({version}){ext}'
                        if candidate.casefold() not in occupied:
                            chosen = candidate
                            break
                    if not chosen:
                        raise RuntimeError('No collision-free used filename available')
                reservations[kind] = chosen
                store.update(job['id'], archive_names=reservations, archive_status='Moving')

            target = matches(used_snapshot, chosen)
            if len(target) > 1 or (target and not _same_identity(target[0], item)):
                raise ValueError('Used destination occupied by a different Drive file')
            originals = matches(originals_snapshot, name)
            if len(originals) > 1 or (originals and fingerprint(originals[0]) != fingerprint(item)):
                raise ValueError('Source changed since sync; refusing Drive move')
            if not target:
                if len(originals) != 1:
                    raise ValueError('Original Drive source missing; cannot verify archive')
                # Atomic provider-side move; ignore-existing is an overwrite guard.
                self.call(['moveto', self.remote + ':' + folder + '/' + name,
                           self.remote + ':' + used + '/' + chosen,
                           '--ignore-existing'])
                # No stale cache survives a write; verify both folder states.
                originals_snapshot = self.listing(folder)
                used_snapshot = self.listing(used)

            verified = matches(used_snapshot, chosen)
            if len(verified) != 1 or not _same_identity(verified[0], item):
                raise ValueError('Drive move receipt missing or mismatched')
            if matches(originals_snapshot, name):
                raise ValueError('Original Drive source still present after archive')
            receipts[kind] = {'name': chosen, 'id': ident, 'verified_at': time.time()}
            store.update(job['id'], archive_moves=receipts, archive_status='Moving')
        store.update(job['id'], archive_moves=receipts, archive_status='Archived')
        return receipts

    def _archive_match(self, item, name, used):
        candidates = [x for x in self.listing(used) if x['Name'] == name]
        return len(candidates) == 1 and _same_identity(candidates[0], item)


def _same_identity(actual, expected):
    """Drive file ID and bytes must match; names can change on a safe collision."""
    if not expected.get('ID') or actual.get('ID') != expected['ID']:
        return False
    if actual.get('Size') != expected.get('Size'):
        return False
    checksum = (expected.get('Hashes') or {}).get('MD5')
    return not checksum or (actual.get('Hashes') or {}).get('MD5') == checksum


def archive_eligible(job):
    """Only default shared Drive input pairs may be moved, never manual/legacy jobs."""
    source = job.get('source') or {}
    dest = job.get('destination') or {}
    return (job.get('origin') == 'drive'
            and source.get('image_folder') == DEFAULT_INPUT
            and source.get('audio_folder') == DEFAULT_INPUT
            and source.get('remote') == dest.get('remote')
            and bool(source.get('remote')))


def sync_owner(store, owner, settings, drive=None, admission_lock=None):
    drive = drive or Drive(settings)
    # Slow Drive listing never monopolizes manual queue admission.
    images = drive.listing(settings['image_folder'])
    # Shared Song Studio folder: one provider listing supplies both media types.
    audio = (images if settings['audio_folder'] == settings['image_folder']
             else drive.listing(settings['audio_folder']))
    results = pairs(images, audio)
    count = 0

    def admit_pairs():
        nonlocal count
        export = store.record('exports', owner)
        job_settings = {**settings, **{key: export[key] for key in ('resolution', 'fit', 'quality') if key in export}}
        for pair in results:
            if pair['stage'] != 'Matched':
                continue
            image, sound = pair['images'][0], pair['audio'][0]
            job = enqueue_pair(store, owner, job_settings, image, sound)
            pair['job_id'], pair['job_stage'] = job['id'], job['stage']
            count += 1

    if admission_lock is not None:
        with open(admission_lock, 'a') as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            admit_pairs()
    else:
        admit_pairs()
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
        'quality': settings.get('quality', 'balanced'), 'drive_status': 'Pending', 'origin': 'drive'})
    return job

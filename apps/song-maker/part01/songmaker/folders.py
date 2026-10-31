"""Canonical account-scoped Song Studio folders and verified-consumption receipts."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from .store import TERMINAL

ID=re.compile(r'^[0-9a-f]{32}$')


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


class Folders:
    def __init__(self, store):
        self.store=store
        with store.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS song_folders(id TEXT PRIMARY KEY,owner TEXT NOT NULL,
                name TEXT NOT NULL,name_key TEXT NOT NULL,UNIQUE(owner,name_key));
            CREATE TABLE IF NOT EXISTS song_folder_media(job_id TEXT PRIMARY KEY,folder_id TEXT NOT NULL,
                manifest TEXT NOT NULL,closed INTEGER NOT NULL DEFAULT 0,consumed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS song_folder_refs(job_id TEXT NOT NULL,consumer TEXT NOT NULL,
                youtube_id TEXT NOT NULL DEFAULT '',PRIMARY KEY(job_id,consumer));
            -- Migration keeps the physically existing source-owner hash; access
            -- is still gated by the new authenticated logical owner.
            CREATE TABLE IF NOT EXISTS song_folder_path_owners(
                folder_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                original_owner_hash TEXT NOT NULL);
            ''')

    def get(self, owner, fid):
        if not ID.fullmatch(str(fid)): raise ValueError('Invalid Song Studio folder')
        with self.store.db() as db:
            row=db.execute('SELECT id,name FROM song_folders WHERE owner=? AND id=?',(owner,fid)).fetchone()
        if not row: raise ValueError('Song Studio folder is unavailable for this account')
        self.path(owner,fid)
        return dict(row)

    def path(self, owner, fid):
        if not owner or len(owner)>160 or any(ord(c)<32 for c in owner) or not ID.fullmatch(str(fid)):
            raise ValueError('Invalid folder identity')
        parent=self.store.root/'folders'
        with self.store.db() as db:
            row=db.execute('SELECT owner,original_owner_hash FROM song_folder_path_owners '
                           'WHERE folder_id=?',(fid,)).fetchone()
        if row:
            if row['owner'] != owner or not re.fullmatch(r'[0-9a-f]{64}',row['original_owner_hash']):
                raise ValueError('Folder path ownership mismatch')
            digest=row['original_owner_hash']
        else:
            digest=hashlib.sha256(owner.encode()).hexdigest()
        path=parent/digest/fid
        for part in (self.store.root,parent,path.parent,path):
            if part.is_symlink(): raise ValueError('Folder symlinks are prohibited')
        return path

    def create(self, owner, name):
        name=str(name or '').strip()
        if not 1<=len(name)<=80 or '..' in name or any(not(c.isalnum() or c in ' _-') for c in name):
            raise ValueError('Use 1–80 letters, numbers, spaces, underscores or hyphens')
        fid=uuid.uuid4().hex
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT id FROM song_folders WHERE owner=? AND name_key=?',(owner,name.casefold())).fetchone()
            if row: return self.get(owner,row['id'])
            path=self.path(owner,fid);path.mkdir(parents=True,mode=0o700)
            db.execute('INSERT INTO song_folders VALUES(?,?,?,?)',(fid,owner,name,name.casefold()))
        return {'id':fid,'name':name}

    def list(self,owner):
        with self.store.db() as db:
            rows=db.execute('SELECT id,name FROM song_folders WHERE owner=? ORDER BY name_key,id',(owner,)).fetchall()
        return [self.get(owner,r['id']) for r in rows]

    def media_path(self, owner, fid, jid):
        if not ID.fullmatch(str(jid)): raise ValueError('Invalid Song Maker project')
        path=self.path(owner,fid)/jid
        if path.is_symlink(): raise ValueError('Unsafe media directory')
        return path

    def publish(self, job, video, thumbnail):
        fid=job['song_folder_id'];owner=job['owner'];jid=job['id']
        self.get(owner,fid)
        target=self.media_path(owner,fid,jid)
        manifest={'id':jid,'folder_id':fid,'name':video.name,'size':video.stat().st_size,
                  'video_sha256':sha(video),'thumbnail_sha256':sha(thumbnail),'created':job['created']}
        if not target.exists():
            pending=target.with_name('.'+jid+'-'+uuid.uuid4().hex)
            pending.mkdir(mode=0o700)
            try:
                for source in (video,thumbnail):
                    if source.is_symlink() or not source.is_file(): raise ValueError('Unsafe rendered media')
                    dest=pending/source.name
                    with source.open('rb') as inp,dest.open('xb') as out:
                        shutil.copyfileobj(inp,out);out.flush();os.fsync(out.fileno())
                    if sha(dest)!=sha(source): raise ValueError('Folder publication hash mismatch')
                os.replace(pending,target)
            finally:
                if pending.exists(): shutil.rmtree(pending)
        else:
            for name,key in ((video.name,'video_sha256'),(thumbnail.name,'thumbnail_sha256')):
                path=target/name
                if path.is_symlink() or not path.is_file() or sha(path)!=manifest[key]:
                    raise ValueError('Existing folder output differs from original pair')
        with self.store.db() as db:
            db.execute('INSERT INTO song_folder_media(job_id,folder_id,manifest) VALUES(?,?,?) '
                       'ON CONFLICT(job_id) DO NOTHING',(jid,fid,json.dumps(manifest)))
        return manifest

    def outputs(self,owner,fid):
        self.get(owner,fid)
        with self.store.db() as db:
            rows=db.execute('''SELECT m.manifest FROM song_folder_media m JOIN jobs j ON j.id=m.job_id
                WHERE j.owner=? AND m.folder_id=? AND m.closed=0 AND m.consumed=0
                  AND j.stage='Completed' AND j.deleted=0''',(owner,fid)).fetchall()
        ready=[]
        for row in rows:
            item=json.loads(row[0]);folder=self.media_path(owner,fid,item['id'])
            video=folder/item['name'];thumb=folder/(Path(item['name']).stem+'.jpg')
            if all(p.is_file() and not p.is_symlink() for p in (video,thumb)):
                ready.append(item)
        return ready

    def reserve(self,owner,fid,jid,consumer):
        self.get(owner,fid)
        if not re.fullmatch(r'song-uploader:profile:[1-9][0-9]{0,10}',consumer):
            raise ValueError('Invalid Song Uploader consumer')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM song_folder_media WHERE job_id=? AND folder_id=?',(jid,fid)).fetchone()
            existing=db.execute('SELECT * FROM song_folder_refs WHERE job_id=? AND consumer=?',(jid,consumer)).fetchone()
            if not row or row['consumed'] or (row['closed'] and not existing):
                raise ValueError('Folder media is already consumed or closing')
            if existing and existing['youtube_id']: raise ValueError('This consumer already completed this media')
            db.execute('INSERT OR IGNORE INTO song_folder_refs(job_id,consumer) VALUES(?,?)',(jid,consumer))
        return {'reserved':True}

    def finish(self,owner,fid,jid,consumer,youtube_id):
        self.get(owner,fid)
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',youtube_id): raise ValueError('Verified YouTube ID required')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            media=db.execute('SELECT * FROM song_folder_media WHERE job_id=? AND folder_id=?',(jid,fid)).fetchone()
            ref=db.execute('SELECT * FROM song_folder_refs WHERE job_id=? AND consumer=?',(jid,consumer)).fetchone()
            if not media or not ref: raise ValueError('Reserved folder consumer required')
            if ref['youtube_id'] and ref['youtube_id']!=youtube_id: raise ValueError('Verified receipt conflict')
            db.execute('UPDATE song_folder_refs SET youtube_id=? WHERE job_id=? AND consumer=?',(youtube_id,jid,consumer))
            db.execute('UPDATE song_folder_media SET closed=1 WHERE job_id=?',(jid,))
        return self.cleanup(owner,fid,jid)

    def dismiss(self, owner, fid, jid):
        """Remove an idle project from the queue without deleting uploader reservations.

        The tombstone and closing flag share one SQLite transaction, so a
        concurrent Song Uploader reserve cannot slip between the two changes.
        Original media survives until every reserved consumer has a receipt.
        """
        self.get(owner, fid)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job=db.execute('SELECT stage,lane,deleted FROM jobs WHERE id=? AND owner=?',
                           (jid,owner)).fetchone()
            if not job or job['deleted']:
                raise ValueError('Job not found')
            if job['lane'] or job['stage'] not in TERMINAL:
                raise ValueError('Cancel the job and wait for the worker before deleting')
            media=db.execute('SELECT consumed FROM song_folder_media WHERE job_id=? AND folder_id=?',
                             (jid,fid)).fetchone()
            pending=bool(media and not media['consumed'] and db.execute(
                "SELECT 1 FROM song_folder_refs WHERE job_id=? AND youtube_id=''",
                (jid,)).fetchone())
            if media and not media['consumed']:
                db.execute('UPDATE song_folder_media SET closed=1 WHERE job_id=?',(jid,))
            db.execute('UPDATE jobs SET deleted=1,updated=? WHERE id=? AND owner=?',
                       (time.time(),jid,owner))
        if media and not media['consumed'] and not pending:
            self.cleanup(owner,fid,jid)
        return pending

    def cleanup(self,owner,fid,jid):
        # Keep receipts committed even if cleanup is interrupted; retries are idempotent.
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            media=db.execute('SELECT * FROM song_folder_media WHERE job_id=? AND folder_id=?',(jid,fid)).fetchone()
            pending=db.execute("SELECT 1 FROM song_folder_refs WHERE job_id=? AND youtube_id=''",(jid,)).fetchone()
            if not media or not media['closed'] or pending: return {'cleaned':False,'pending_dependents':bool(pending)}
            if not media['consumed']:
                for path in (self.media_path(owner,fid,jid),self.store.root/'outputs'/jid,self.store.root/'inputs'/jid):
                    if path.parent.is_symlink() or path.is_symlink() or any(p.is_symlink() for p in path.rglob('*')):
                        raise ValueError('Unsafe media cleanup path')
                    if path.exists(): shutil.rmtree(path)
                db.execute('UPDATE song_folder_media SET consumed=1 WHERE job_id=?',(jid,))
            return {'cleaned':True,'pending_dependents':False}

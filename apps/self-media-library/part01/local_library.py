"""Local SML lifecycle. All destructive operations serialize with editor reservations.

Historical Drive receipts remain in SQLite for provenance; no remote IO is performed.
"""
from pathlib import Path
import json
import shutil
import subprocess
import time
import uuid
from fastapi import HTTPException, Query
from pydantic import BaseModel, Field
from media_integrity import sha256_file
from media_catalog import probe_video


class Choice(BaseModel):
    keep_id: str | None = None  # None is allowed only for an odd singleton.


class MediaBatch(BaseModel):
    media_type: str
    ids: list[str] = Field(min_length=1, max_length=500)


def initialize(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS local_reviews (
      source_id TEXT PRIMARY KEY, state TEXT NOT NULL, error TEXT,
      created_at TEXT NOT NULL, verified_at TEXT, cleaned_at TEXT);
    CREATE TABLE IF NOT EXISTS local_pairs (
      source_id TEXT NOT NULL, pair_index INTEGER NOT NULL, ids_json TEXT NOT NULL,
      decided INTEGER NOT NULL DEFAULT 0, keep_id TEXT,
      PRIMARY KEY(source_id,pair_index));
    CREATE TABLE IF NOT EXISTS library_replacements (
      id TEXT PRIMARY KEY, old_json TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
    ''')
    if 'local_selection' not in {r['name'] for r in conn.execute('PRAGMA table_info(clips)')}:
        conn.execute("ALTER TABLE clips ADD COLUMN local_selection TEXT NOT NULL DEFAULT 'legacy'")
    conn.execute("UPDATE settings SET value='false' WHERE key='drive_enabled'")
    conn.execute("UPDATE settings SET value='after_local_selection' WHERE key='source_cleanup'")


def busy(conn):
    # Fail closed while intake/processing/AI jobs could still be reading media.
    for sql in ("SELECT 1 FROM sources WHERE status='processing' LIMIT 1",
                "SELECT 1 FROM media_imports WHERE status IN ('queued','processing') LIMIT 1",
                "SELECT 1 FROM ai_jobs WHERE state IN ('queued','running') LIMIT 1"):
        if conn.execute(sql).fetchone():
            raise HTTPException(409, 'Media jobs are active; retry after they finish')


def protect(conn, kind, ids, *, pending=True):
    for ident in ids:
        if conn.execute("SELECT 1 FROM media_reservations WHERE media_type=? AND media_id=? AND (state='committed' OR (state='reserved' AND expires_at>?)) LIMIT 1", (kind, ident, int(time.time()))).fetchone():
            raise HTTPException(409, 'Media is required by an editor project: ' + ident)
        if kind == 'video':
            source=conn.execute('SELECT s.status FROM sources s JOIN clips c ON c.source_id=s.id WHERE c.id=?',(ident,)).fetchone()
            if source and source['status'] in ('queued','processing','failed'):
                raise HTTPException(409, 'Pending, active or failed source media is protected')
        if pending and kind == 'video':
            row = conn.execute('SELECT local_selection FROM clips WHERE id=?', (ident,)).fetchone()
            if row and row['local_selection'] == 'pending':
                raise HTTPException(409, 'Complete pair review before changing this clip')


def safe_file(root, folder, name):
    path = root / folder / name
    if not name or Path(name).name != name or path.is_symlink():
        raise HTTPException(409, 'Unsafe media filename; files retained')
    return path


def verify(root, row, kind):
    path = safe_file(root, 'clips' if kind == 'video' else 'images', row['filename'] if kind == 'video' else row['stored_name'])
    if not path.is_file() or path.stat().st_size == 0 or not row['sha256'] or sha256_file(path) != row['sha256']:
        raise HTTPException(409, 'Local media verification failed: ' + row['id'])
    if kind == 'video':
        if not probe_video(path)['width']:
            raise HTTPException(409, 'Clip cannot be decoded: ' + row['id'])
        try:
            subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(path),'-f','null','-'],check=True,capture_output=True,timeout=120)
        except (OSError,subprocess.SubprocessError):
            raise HTTPException(409, 'Clip decode verification failed: ' + row['id'])
    with path.open('rb') as stream:
        import os
        os.fsync(stream.fileno())
    return path


def create_review(db, now, source_id, ids):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO local_reviews(source_id,state,created_at) VALUES(?,?,?)', (source_id,'pending',now()))
        for index in range(0, len(ids), 2):
            conn.execute('INSERT INTO local_pairs(source_id,pair_index,ids_json) VALUES(?,?,?)', (source_id,index//2,json.dumps(ids[index:index+2])))
        conn.executemany("UPDATE clips SET local_selection='pending' WHERE id=?", [(i,) for i in ids])


def finalize(root, db, now, source_id):
    root = Path(root)
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        src = conn.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
        review = conn.execute('SELECT * FROM local_reviews WHERE source_id=?', (source_id,)).fetchone()
        if not src or not review:
            raise HTTPException(404, 'Review not found')
        if review['state'] == 'cleaned':
            return {'ok':True,'state':'cleaned','idempotent':True}
        if src['status'] != 'completed':
            raise HTTPException(409, 'Failed or incomplete processing: original retained')
        pairs = conn.execute('SELECT * FROM local_pairs WHERE source_id=? ORDER BY pair_index', (source_id,)).fetchall()
        if not pairs or any(not p['decided'] for p in pairs):
            raise HTTPException(409, 'Complete every pair and the odd last clip first')
        busy(conn)
        kept = [p['keep_id'] for p in pairs if p['keep_id']]
        if not kept:
            raise HTTPException(409, 'Keep at least one verified clip before deleting the original')
        ids = [i for p in pairs for i in json.loads(p['ids_json'])]
        protect(conn, 'video', ids, pending=False)
        rows = {r['id']:r for r in conn.execute('SELECT * FROM clips WHERE source_id=?', (source_id,))}
        # Verification precedes ALL removal. Retry may resume after any filesystem error.
        for ident in kept:
            row = rows.get(ident)
            if not row or row['deleted_at']:
                raise HTTPException(409, 'Kept clip is missing; files retained')
            verify(root, row, 'video')
        conn.execute('UPDATE local_reviews SET verified_at=?,error=NULL WHERE source_id=?', (now(),source_id))
        for ident in ids:
            if ident in kept:
                conn.execute("UPDATE clips SET local_selection='kept',status='approved',duplicate_state='clear',updated_at=? WHERE id=?", (now(),ident))
                continue
            row = rows[ident]
            for folder, name in [('clips',row['filename']),('thumbs',row['thumb_filename']),('metadata',ident+'.json')]:
                if name:safe_file(root,folder,name).unlink(missing_ok=True)
            conn.execute("UPDATE clips SET local_selection='discarded',status='rejected',deleted_at=?,updated_at=? WHERE id=?", (now(),now(),ident))
        safe_file(root,'sources',src['stored_name']).unlink(missing_ok=True)
        conn.execute("UPDATE sources SET source_deleted_at=?,source_cleanup_mode='local_pair_verified',stage='selection_saved',updated_at=? WHERE id=?", (now(),now(),source_id))
        conn.execute("UPDATE local_reviews SET state='cleaned',cleaned_at=?,error=NULL WHERE source_id=?", (now(),source_id))
    return {'ok':True,'state':'cleaned','kept':len(kept),'discarded':len(ids)-len(kept)}


def erase(root, conn, kind, ids, now):
    busy(conn)
    protect(conn,kind,ids)
    table = 'clips' if kind == 'video' else 'image_assets'
    for ident in ids:
        row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
        if not row:raise HTTPException(404,'Media not found: '+ident)
        if kind == 'video':
            files=[('clips',row['filename']),('thumbs',row['thumb_filename']),('metadata',ident+'.json')]
        else:
            files=[('images',row['stored_name']),('image_previews',row['thumb_name'])]
        for folder,name in files:
            if name:safe_file(root,folder,name).unlink(missing_ok=True)
        conn.execute(f'UPDATE {table} SET deleted_at=?,updated_at=? WHERE id=?',(now(),now(),ident))
        if kind == 'video':
            conn.execute("UPDATE clips SET local_selection='discarded',status='rejected' WHERE id=?",(ident,))


def register(app, root, db, now):
    root = Path(root)
    @app.on_event('startup')
    def start_local():
        with db() as conn:initialize(conn)

    @app.get('/api/local/storage')
    def storage():
        usage=shutil.disk_usage(root)
        used=sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink())
        with db() as conn:
            counts={name:conn.execute(f'SELECT COUNT(*) FROM {table} WHERE deleted_at IS NULL').fetchone()[0] for name,table in [('clips','clips'),('images','image_assets')]}
            pending=conn.execute("SELECT COUNT(*) FROM local_reviews WHERE state!='cleaned'").fetchone()[0]
            cleaned=conn.execute("SELECT COUNT(*) FROM local_reviews WHERE state='cleaned'").fetchone()[0]
        return {**counts,'storage_mode':'vps_only','library_bytes':used,'vps_used_bytes':usage.used,'free_bytes':usage.free,'total_bytes':usage.total,'pending_reviews':pending,'sources_cleaned':cleaned}

    @app.get('/api/local/reviews')
    def reviews():
        with db() as conn:
            rows=conn.execute('SELECT r.*,s.filename FROM local_reviews r JOIN sources s ON s.id=r.source_id ORDER BY r.created_at DESC').fetchall()
        return {'items':[dict(r) for r in rows]}

    @app.get('/api/local/reviews/{source_id}')
    def review(source_id:str):
        with db() as conn:
            source=conn.execute('SELECT * FROM local_reviews WHERE source_id=?',(source_id,)).fetchone()
            if not source:raise HTTPException(404,'Review not found')
            pairs=[]
            for pair in conn.execute('SELECT * FROM local_pairs WHERE source_id=? ORDER BY pair_index',(source_id,)):
                items=[]
                for ident in json.loads(pair['ids_json']):
                    row=conn.execute('SELECT * FROM clips WHERE id=?',(ident,)).fetchone()
                    items.append({**dict(row),'clip_url':'/media/clips/'+row['filename']})
                pairs.append({**dict(pair),'items':items})
        return {**dict(source),'pairs':pairs}

    @app.put('/api/local/reviews/{source_id}/pairs/{index}')
    def choose(source_id:str,index:int,body:Choice):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            r=conn.execute('SELECT * FROM local_reviews WHERE source_id=?',(source_id,)).fetchone()
            pair=conn.execute('SELECT * FROM local_pairs WHERE source_id=? AND pair_index=?',(source_id,index)).fetchone()
            if not r or not pair:raise HTTPException(404,'Review pair not found')
            if r['state']=='cleaned':raise HTTPException(409,'Selection already saved')
            ids=json.loads(pair['ids_json'])
            if body.keep_id not in ids and not (len(ids)==1 and body.keep_id is None):
                raise HTTPException(400,'Keep one clip from this pair; odd last clip can be discarded')
            conn.execute('UPDATE local_pairs SET decided=1,keep_id=? WHERE source_id=? AND pair_index=?',(body.keep_id,source_id,index))
        try:
            result=finalize(root,db,now,source_id)
        except HTTPException as exc:
            # Decision is durable, even if another pair, job or integrity check blocks cleanup.
            return {'ok':True,'saved':True,'cleanup_pending':True,'detail':exc.detail}
        return result

    @app.post('/api/local/reviews/{source_id}/finalize')
    def finish(source_id:str):return finalize(root,db,now,source_id)

    @app.get('/api/local/catalog')
    def catalog(media_type:str='video',q:str='',date_from:str='',date_to:str='',keyword:str='',limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
        if media_type not in ('video','image'):raise HTTPException(400,'Invalid media type')
        with db() as conn:
            if media_type=='video':
                rows=conn.execute('SELECT c.*,s.filename AS original_name FROM clips c LEFT JOIN sources s ON s.id=c.source_id WHERE c.deleted_at IS NULL ORDER BY c.created_at DESC').fetchall()
            else:rows=conn.execute('SELECT * FROM image_assets WHERE deleted_at IS NULL ORDER BY created_at DESC').fetchall()
            items=[]
            for row in rows:
                item=dict(row)
                tags=[r[0] for r in conn.execute('SELECT keyword FROM clip_keywords WHERE clip_id=?',(row['id'],))] if media_type=='video' else json.loads(row['keywords_json'])
                day=row['created_at'][:10]
                hay=' '.join([item.get('original_name') or '',item.get('filename') or '',row['description'],' '.join(tags)]).lower()
                if date_from and day<date_from or date_to and day>date_to or keyword and keyword.lower() not in [t.lower() for t in tags] or any(t not in hay for t in q.lower().split()):continue
                item.update(type=media_type,keywords=tags,content_url='/media/clips/'+row['filename'] if media_type=='video' else '/api/v1/images/'+row['id']+'/content',thumb_url='/media/thumbs/'+(row['thumb_filename'] or '') if media_type=='video' else '/api/v1/images/'+row['id']+'/preview')
                items.append(item)
        return {'items':items[offset:offset+limit],'total':len(items)}

    @app.post('/api/local/delete')
    def bulk_delete(body:MediaBatch):
        if body.media_type not in ('video','image'):raise HTTPException(400,'Invalid media type')
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            busy(conn);protect(conn,body.media_type,body.ids)
            # Validate every record before irreversible filesystem operations.
            table='clips' if body.media_type=='video' else 'image_assets'
            if any(not conn.execute(f'SELECT 1 FROM {table} WHERE id=?',(i,)).fetchone() for i in body.ids):raise HTTPException(404,'Media not found')
            erase(root,conn,body.media_type,body.ids,now)
        return {'ok':True,'deleted':len(body.ids)}

    @app.post('/api/local/replacements')
    def begin_replace():
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE');busy(conn)
            if conn.execute("SELECT 1 FROM library_replacements WHERE state='pending'").fetchone():raise HTTPException(409,'A replacement is already pending')
            old={kind:[r[0] for r in conn.execute(f'SELECT id FROM {table} WHERE deleted_at IS NULL')] for kind,table in [('video','clips'),('image','image_assets')]}
            ident=uuid.uuid4().hex
            conn.execute('INSERT INTO library_replacements VALUES(?,?,?,?)',(ident,json.dumps(old),'pending',now()))
        return {'id':ident,'state':'pending','detail':'Upload and approve the replacement media. Old library remains available until verified commit.'}

    @app.get('/api/local/replacements')
    def replacements():
        with db() as conn:rows=conn.execute("SELECT id,state,created_at FROM library_replacements WHERE state='pending'").fetchall()
        return {'items':[dict(r) for r in rows]}

    @app.post('/api/local/replacements/{ident}/cancel')
    def cancel_replace(ident:str):
        with db() as conn:conn.execute("UPDATE library_replacements SET state='cancelled' WHERE id=? AND state='pending'",(ident,))
        return {'ok':True}

    @app.post('/api/local/replacements/{ident}/commit')
    def commit_replace(ident:str):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE');busy(conn)
            row=conn.execute('SELECT * FROM library_replacements WHERE id=?',(ident,)).fetchone()
            if not row:raise HTTPException(404,'Replacement not found')
            if row['state']=='completed':return {'ok':True,'idempotent':True}
            if row['state']!='pending':raise HTTPException(409,'Replacement is not pending')
            if conn.execute("SELECT 1 FROM local_reviews WHERE state!='cleaned'").fetchone():raise HTTPException(409,'Complete outstanding pair reviews first')
            old=json.loads(row['old_json']);new_count=0
            for kind,table in [('video','clips'),('image','image_assets')]:
                protect(conn,kind,old[kind])
                for asset in conn.execute(f"SELECT * FROM {table} WHERE deleted_at IS NULL AND status='approved'"):
                    if asset['id'] not in old[kind]:verify(root,asset,kind);new_count+=1
            if not new_count:raise HTTPException(409,'Save and approve verified replacement media first')
            for kind in ('video','image'):erase(root,conn,kind,old[kind],now)
            conn.execute("UPDATE library_replacements SET state='completed' WHERE id=?",(ident,))
        return {'ok':True,'state':'completed','new_assets':new_count}

"""Local SML lifecycle. All destructive operations serialize with editor reservations.

Historical Drive receipts remain in SQLite for provenance; no remote IO is performed.
"""
from pathlib import Path
import json
import shutil
import subprocess
import time
from fastapi import HTTPException, Query
from pydantic import BaseModel, Field
from media_integrity import sha256_file
from media_catalog import probe_video


class Choice(BaseModel):
    keep_id: str | None = None  # None is allowed only for an odd singleton.


class PairDecision(Choice):
    pair_index: int = Field(ge=0)


class PairDecisions(BaseModel):
    decisions: list[PairDecision] = Field(min_length=1, max_length=5)


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
            source=conn.execute('SELECT s.status,s.drive_state,s.clip_count,c.local_selection FROM sources s JOIN clips c ON c.source_id=s.id WHERE c.id=?',(ident,)).fetchone()
            # Failed/incomplete extraction retains its source/partial clips. A
            # proven formerly complete Drive-era source can be owner-deleted.
            legacy_complete = bool(source and source['local_selection']=='legacy' and
                                   source['clip_count'] > 0 and
                                   source['drive_state'] in ('verified','sync_failed','disabled'))
            if source and (source['status'] in ('queued','processing') or
                           (source['status']=='failed' and not legacy_complete)):
                raise HTTPException(409, 'Pending, active or failed source media is protected')
        if pending and kind == 'video':
            row = conn.execute('SELECT local_selection FROM clips WHERE id=?', (ident,)).fetchone()
            if row and row['local_selection'] == 'pending':
                raise HTTPException(409, 'Complete pair review before changing this clip')


def reconcile_manual_pairs(conn, changed_ids):
    """A manual trash/delete/restore counts as a pair choice, not a review lock.

    Preserve original pair membership for restore, and never touch reviews that
    have already been finalized. Caller holds the same write transaction as
    the asset change, so review state and catalog cannot drift apart.
    """
    if not changed_ids:
        return
    placeholders = ','.join('?' for _ in changed_ids)
    sources = conn.execute(
        f'SELECT DISTINCT source_id FROM clips WHERE id IN ({placeholders})',
        tuple(changed_ids)
    ).fetchall()
    for source in sources:
        sid = source['source_id']
        review = conn.execute('SELECT state FROM local_reviews WHERE source_id=?', (sid,)).fetchone()
        if not review or review['state'] == 'cleaned':
            continue
        for pair in conn.execute('SELECT pair_index,ids_json FROM local_pairs WHERE source_id=?', (sid,)).fetchall():
            members = json.loads(pair['ids_json'])
            if not any(ident in changed_ids for ident in members):
                continue
            live = [
                ident for ident in members
                if (row := conn.execute('SELECT deleted_at FROM clips WHERE id=?', (ident,)).fetchone())
                and row['deleted_at'] is None
            ]
            # If exactly one survivor remains, the owner's delete selected it.
            # If both were removed there is no keeper for this pair. A restore
            # of a full pair returns it to an undecided review state.
            decided = 1 if len(live) <= 1 else 0
            keeper = live[0] if len(live) == 1 else None
            conn.execute(
                'UPDATE local_pairs SET decided=?,keep_id=? WHERE source_id=? AND pair_index=?',
                (decided, keeper, sid, pair['pair_index'])
            )


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
            row = rows.get(ident)
            # A manually trashed clip remains restorable; a permanently erased
            # clip has no catalog row or bytes left to clean a second time.
            if not row or row['deleted_at']:
                continue
            for folder, name in [('clips',row['filename']),('thumbs',row['thumb_filename']),('metadata',ident+'.json')]:
                if name:safe_file(root,folder,name).unlink(missing_ok=True)
            conn.execute("UPDATE clips SET local_selection='discarded',status='rejected',deleted_at=?,updated_at=? WHERE id=?", (now(),now(),ident))
        safe_file(root,'sources',src['stored_name']).unlink(missing_ok=True)
        conn.execute("UPDATE sources SET source_deleted_at=?,source_cleanup_mode='local_pair_verified',stage='selection_saved',updated_at=? WHERE id=?", (now(),now(),source_id))
        conn.execute("UPDATE local_reviews SET state='cleaned',cleaned_at=?,error=NULL WHERE source_id=?", (now(),source_id))
    return {'ok':True,'state':'cleaned','kept':len(kept),'discarded':len(ids)-len(kept)}


def guard_manual(conn, kind, ids):
    """Block media actively used by a job, not unrelated old queued jobs."""
    if conn.execute("SELECT 1 FROM ai_jobs WHERE state='running' AND (media_id IS NULL OR media_type IS NULL) LIMIT 1").fetchone():
        raise HTTPException(409, 'A library-wide AI job is running; retry when complete')
    for ident in ids:
        if conn.execute("SELECT 1 FROM ai_jobs WHERE state IN ('queued','running') AND media_type=? AND media_id=? LIMIT 1",
                        (kind,ident)).fetchone():
            raise HTTPException(409, 'Selected media has an active AI job: ' + ident)
        if kind == 'video':
            if conn.execute("""SELECT 1 FROM ai_jobs WHERE state IN ('queued','running') AND media_type='source'
                AND media_id=(SELECT source_id FROM clips WHERE id=?) LIMIT 1""",(ident,)).fetchone():
                raise HTTPException(409, 'Selected source has an active AI job: ' + ident)
            if conn.execute("""SELECT 1 FROM media_imports WHERE status IN ('queued','processing')
                AND source_id=(SELECT source_id FROM clips WHERE id=?) LIMIT 1""",(ident,)).fetchone():
                raise HTTPException(409, 'Selected source has an active import: ' + ident)


def erase(root, conn, kind, ids, now):
    guard_manual(conn,kind,ids)
    # Manual owner deletion is independent of the optional pair-review flow.
    # Keep editor reservations, active jobs and incomplete-source safeguards.
    protect(conn,kind,ids,pending=False)
    table = 'clips' if kind == 'video' else 'image_assets'
    affected_sources = set()
    for ident in ids:
        row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
        if row and kind == 'video':
            affected_sources.add(row['source_id'])
        if not row:raise HTTPException(404,'Media not found: '+ident)
        if kind == 'video':
            files=[('clips',row['filename']),('thumbs',row['thumb_filename']),('metadata',ident+'.json')]
        else:
            files=[('images',row['stored_name']),('image_previews',row['thumb_name']),
                   ('image_previews',ident+'-sharp-v2.jpg')]
        for folder,name in files:
            if name:safe_file(root,folder,name).unlink(missing_ok=True)
        # Permanent deletion removes catalog/AI data as well as local bytes.
        # Historical unrelated Drive receipts and source provenance remain intact.
        if kind == 'video':
            conn.execute("DELETE FROM clip_keywords WHERE clip_id=?",(ident,))
        conn.execute("DELETE FROM ai_metadata WHERE media_type=? AND media_id=?",(kind,ident))
        conn.execute("DELETE FROM ai_metadata_history WHERE media_type=? AND media_id=?",(kind,ident))
        conn.execute(f'DELETE FROM {table} WHERE id=?',(ident,))
    if kind == 'video':
        # The affected clip rows no longer exist. Reconcile from the surviving
        # original pair IDs without relying on deleted catalog rows.
        for sid in affected_sources:
            reconcile_source_pairs(conn, sid, ids)


def reconcile_source_pairs(conn, source_id, changed_ids):
    review = conn.execute('SELECT state FROM local_reviews WHERE source_id=?', (source_id,)).fetchone()
    if not review or review['state'] == 'cleaned':
        return
    for pair in conn.execute('SELECT pair_index,ids_json FROM local_pairs WHERE source_id=?', (source_id,)).fetchall():
        members = json.loads(pair['ids_json'])
        if not any(ident in changed_ids for ident in members):
            continue
        live = [
            ident for ident in members
            if (row := conn.execute('SELECT deleted_at FROM clips WHERE id=?', (ident,)).fetchone())
            and row['deleted_at'] is None
        ]
        if len(live) <= 1:
            conn.execute('UPDATE local_pairs SET decided=1,keep_id=? WHERE source_id=? AND pair_index=?',
                         (live[0] if live else None, source_id, pair['pair_index']))


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
    def review(source_id:str, offset:int=Query(0,ge=0), limit:int=Query(500,ge=1,le=500)):
        with db() as conn:
            source=conn.execute('SELECT * FROM local_reviews WHERE source_id=?',(source_id,)).fetchone()
            if not source:raise HTTPException(404,'Review not found')
            summary=conn.execute(
                'SELECT COUNT(*) total,COALESCE(SUM(decided),0) saved,'
                ' MIN(CASE WHEN decided=0 THEN pair_index END) first_pending'
                ' FROM local_pairs WHERE source_id=?',(source_id,)).fetchone()
            pairs=[]
            for pair in conn.execute(
                'SELECT * FROM local_pairs WHERE source_id=? ORDER BY pair_index LIMIT ? OFFSET ?',
                (source_id,limit,offset)):
                items=[]
                for ident in json.loads(pair['ids_json']):
                    row=conn.execute('SELECT * FROM clips WHERE id=?',(ident,)).fetchone()
                    # Manually removed/trashed clips must not break review pages.
                    if row and row['deleted_at'] is None:
                        items.append({**dict(row),'clip_url':'/media/clips/'+row['filename']})
                pairs.append({**dict(pair),'items':items})
        return {**dict(source),'total_pairs':summary['total'],
                'saved_pairs':summary['saved'],
                'first_pending_pair_index':summary['first_pending'],'pairs':pairs}

    @app.put('/api/local/reviews/{source_id}/pairs/batch')
    def choose_batch(source_id:str, body:PairDecisions):
        # One SQLite transaction per batch; no half-saved batch or early cleanup.
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            review=conn.execute('SELECT state FROM local_reviews WHERE source_id=?',(source_id,)).fetchone()
            if not review:raise HTTPException(404,'Review not found')
            if review['state']=='cleaned':raise HTTPException(409,'Selection already saved')
            indices=[decision.pair_index for decision in body.decisions]
            if len(set(indices)) != len(indices):
                raise HTTPException(400,'Duplicate pair indexes in batch')
            if max(indices)-min(indices)>=5 or min(indices)//5 != max(indices)//5:
                raise HTTPException(400,'Batch must contain pairs from one five-pair page')
            for decision in body.decisions:
                pair=conn.execute('SELECT ids_json FROM local_pairs WHERE source_id=? AND pair_index=?',
                                  (source_id,decision.pair_index)).fetchone()
                if not pair:raise HTTPException(404,'Review pair not found')
                ids=json.loads(pair['ids_json'])
                live=[ident for ident in ids if (row := conn.execute('SELECT deleted_at FROM clips WHERE id=?',(ident,)).fetchone()) and row['deleted_at'] is None]
                if decision.keep_id not in live and not (len(live)<=1 and decision.keep_id is None):
                    raise HTTPException(400,'Keep one available clip from each pair')
            for decision in body.decisions:
                conn.execute('UPDATE local_pairs SET decided=1,keep_id=? WHERE source_id=? AND pair_index=?',
                             (decision.keep_id,source_id,decision.pair_index))
        try:
            result=finalize(root,db,now,source_id)
        except HTTPException as exc:
            # All decisions are durably committed even when verification blocks cleanup.
            return {'ok':True,'saved':True,'saved_count':len(body.decisions),
                    'cleanup_pending':True,'detail':exc.detail}
        return {**result,'saved':True,'saved_count':len(body.decisions)}

    @app.put('/api/local/reviews/{source_id}/pairs/{index}')
    def choose(source_id:str,index:int,body:Choice):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            r=conn.execute('SELECT * FROM local_reviews WHERE source_id=?',(source_id,)).fetchone()
            pair=conn.execute('SELECT * FROM local_pairs WHERE source_id=? AND pair_index=?',(source_id,index)).fetchone()
            if not r or not pair:raise HTTPException(404,'Review pair not found')
            if r['state']=='cleaned':raise HTTPException(409,'Selection already saved')
            ids=json.loads(pair['ids_json'])
            live=[ident for ident in ids if (row := conn.execute('SELECT deleted_at FROM clips WHERE id=?',(ident,)).fetchone()) and row['deleted_at'] is None]
            if body.keep_id not in live and not (len(live)<=1 and body.keep_id is None):
                raise HTTPException(400,'Keep one available clip from this pair')
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
            # Validate and guard every selected asset before any filesystem unlink.
            # Unrelated import/AI jobs must not lock the owner's entire library.
            # erase() checks target AI jobs and editor reservations atomically.
            # Validate every record before irreversible filesystem operations.
            table='clips' if body.media_type=='video' else 'image_assets'
            if any(not conn.execute(f'SELECT 1 FROM {table} WHERE id=?',(i,)).fetchone() for i in body.ids):raise HTTPException(404,'Media not found')
            erase(root,conn,body.media_type,body.ids,now)
        return {'ok':True,'deleted':len(body.ids)}

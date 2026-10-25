"""Additive AI workspace, durable SQLite queue and isolated inference subprocess.

The web process never loads model weights. A single fenced worker serializes
inference and writes versioned metadata/embeddings; search text is a queued job.
All original media, IDs, tables and machine API credentials remain untouched.
"""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime,timezone
from fastapi import File,HTTPException,Query,UploadFile
from pydantic import BaseModel,Field
from ai_engine import MODEL,VERSION,model_ready


def now():return datetime.now(timezone.utc).isoformat()
def connect(root):
    c=sqlite3.connect(Path(root)/'library.db',timeout=30);c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=30000');return c

def unpack(value,default=None):
    try:return json.loads(value) if value else default
    except (ValueError,TypeError):return default

def cosine(a,b):
    if not a or not b or len(a)!=len(b):return 0.
    return sum(x*y for x,y in zip(a,b))/(math.sqrt(sum(x*x for x in a)*sum(x*x for x in b)) or 1.)


def initialize(root):
    with connect(root) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS ai_metadata (
          media_type TEXT NOT NULL,media_id TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
          model_version TEXT NOT NULL DEFAULT '',machine_json TEXT NOT NULL DEFAULT '{}',
          manual_json TEXT NOT NULL DEFAULT '{}',embedding_json TEXT,error TEXT,
          revision INTEGER NOT NULL DEFAULT 0,analyzed_at TEXT,updated_at TEXT NOT NULL,
          PRIMARY KEY(media_type,media_id));
        CREATE TABLE IF NOT EXISTS ai_jobs (
          id TEXT PRIMARY KEY,kind TEXT NOT NULL,media_type TEXT,media_id TEXT,payload TEXT NOT NULL,
          state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,result_json TEXT,error TEXT,
          created_at REAL NOT NULL,updated_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS ai_jobs_queue ON ai_jobs(state,kind,created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS ai_one_asset_job ON ai_jobs(media_type,media_id)
          WHERE kind='analyze' AND state IN ('queued','running');
        CREATE TABLE IF NOT EXISTS ai_metadata_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,media_type TEXT,media_id TEXT,
          origin TEXT NOT NULL,before_json TEXT,after_json TEXT,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_collections (
          id TEXT PRIMARY KEY,name TEXT NOT NULL,query TEXT NOT NULL,filters_json TEXT NOT NULL,
          created_at TEXT NOT NULL);
        ''')
        if 'manual_embedding_json' not in {x['name'] for x in c.execute('PRAGMA table_info(ai_metadata)')}:
            c.execute('ALTER TABLE ai_metadata ADD COLUMN manual_embedding_json TEXT')
        if 'text_embedding_json' not in {x['name'] for x in c.execute('PRAGMA table_info(ai_metadata)')}:
            c.execute('ALTER TABLE ai_metadata ADD COLUMN text_embedding_json TEXT')
        # Historical filename fallback is context, never verified visual inference.
        c.execute("UPDATE clips SET ai_state='pending',ai_confidence=NULL WHERE ai_state='complete' AND NOT EXISTS(SELECT 1 FROM ai_metadata a WHERE a.media_id=clips.id AND a.media_type='video' AND a.state='complete')")
        for key,value in {'ai_engine_enabled':'true','ai_batch_limit':'32','ai_job_timeout':'120'}.items():
            c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(key,value))


def settings(root):
    with connect(root) as c:return {r['key']:r['value'] for r in c.execute('SELECT key,value FROM settings')}


def catalog(root,media_type='all'):
    root=Path(root);out=[]
    with connect(root) as c:
        if media_type in ('video','all'):
            for r in c.execute('''SELECT c.*,s.filename AS source_filename,s.width,s.height,s.orientation,s.codec,s.fps,
                s.rights_confirmed,s.source_kind,s.stored_name AS source_stored_name FROM clips c LEFT JOIN sources s ON c.source_id=s.id
                WHERE c.deleted_at IS NULL ORDER BY c.created_at DESC LIMIT 5000'''):
                x=dict(r);x.update(type='video',path=str(root/'clips'/r['filename']),
                    thumb_url='/media/thumbs/'+(r['thumb_filename'] or ''),content_url='/media/clips/'+r['filename'])
                x['keywords']=[k['keyword'] for k in c.execute('SELECT keyword FROM clip_keywords WHERE clip_id=?',(r['id'],))]
                out.append(x)
        if media_type in ('image','all'):
            for r in c.execute('SELECT * FROM image_assets WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 5000'):
                x=dict(r);w,h=r['width'],r['height'];x.update(type='image',path=str(root/'images'/r['stored_name']),
                    source_id=r['id'],source_filename=r['original_name'],duration=0,quality=0,
                    orientation='landscape' if w>h*1.12 else 'portrait' if h>w*1.12 else 'square',
                    keywords=unpack(r['keywords_json'],[]),thumb_url=f"/api/v1/images/{r['id']}/preview",
                    content_url=f"/api/v1/images/{r['id']}/content")
                out.append(x)
        if media_type=='source':
            for r in c.execute('SELECT * FROM sources ORDER BY created_at DESC LIMIT 5000'):
                x=dict(r);x.update(type='source',path=str(root/'sources'/r['stored_name']),duration=r['duration'] or 0)
                out.append(x)
    return out


def locate(root,media_type,media_id):
    # Read one asset by primary key. The list catalog intentionally limits its
    # preview to recent rows, but direct edits must work for older assets too.
    if media_type not in ('image','video','source'):
        raise HTTPException(400,'Invalid media type')
    root=Path(root)
    with connect(root) as c:
        if media_type=='video':
            r=c.execute('''SELECT c.*,s.filename AS source_filename,s.width,s.height,
                s.orientation,s.codec,s.fps,s.rights_confirmed,s.source_kind,
                s.stored_name AS source_stored_name FROM clips c
                LEFT JOIN sources s ON c.source_id=s.id
                WHERE c.id=? AND c.deleted_at IS NULL''',(media_id,)).fetchone()
            if r:
                row=dict(r)
                row.update(type='video',path=str(root/'clips'/r['filename']),
                           thumb_url='/media/thumbs/'+(r['thumb_filename'] or ''),
                           content_url='/media/clips/'+r['filename'])
                row['keywords']=[x['keyword'] for x in c.execute(
                    'SELECT keyword FROM clip_keywords WHERE clip_id=?',(media_id,))]
        elif media_type=='image':
            r=c.execute('SELECT * FROM image_assets WHERE id=? AND deleted_at IS NULL',
                        (media_id,)).fetchone()
            if r:
                row=dict(r)
                width,height=r['width'],r['height']
                row.update(type='image',path=str(root/'images'/r['stored_name']),
                           source_id=r['id'],source_filename=r['original_name'],duration=0,quality=0,
                           orientation='landscape' if width>height*1.12 else
                                       'portrait' if height>width*1.12 else 'square',
                           keywords=unpack(r['keywords_json'],[]),
                           thumb_url=f"/api/v1/images/{r['id']}/preview",
                           content_url=f"/api/v1/images/{r['id']}/content")
        else:
            r=c.execute('SELECT * FROM sources WHERE id=?',(media_id,)).fetchone()
            if r:
                row=dict(r)
                row.update(type='source',path=str(root/'sources'/r['stored_name']),
                           duration=r['duration'] or 0)
    if not r:
        raise HTTPException(404,'Asset unavailable or in trash')
    path=Path(row['path'])
    if not path.resolve().is_relative_to(root.resolve()):
        raise HTTPException(400,'Invalid asset path')
    return row


def metadata(root,media_type,media_id):
    asset=locate(root,media_type,media_id)
    with connect(root) as c:
        row=c.execute('SELECT * FROM ai_metadata WHERE media_type=? AND media_id=?',(media_type,media_id)).fetchone()
    if not row:return {'state':'pending','machine':{},'manual':{},'revision':0,'asset':public_asset(asset)}
    result=dict(row);result['machine']=unpack(result.pop('machine_json'),{});result['manual']=unpack(result.pop('manual_json'),{})
    result.pop('embedding_json',None);result.pop('manual_embedding_json',None);result.pop('text_embedding_json',None);result['asset']=public_asset(asset)
    result['effective']={**result['machine'],**result['manual']}
    return result


def public_asset(x):
    return {k:x.get(k) for k in ('id','type','source_id','source_filename','description','status','duration',
        'width','height','orientation','codec','fps','quality','source_kind','created_at','rights_confirmed','thumb_url','content_url')}


def enqueue(root,kind,media_type='',media_id='',payload=None):
    stamp=time.time();job_id=uuid.uuid4().hex
    with connect(root) as c:
        c.execute('BEGIN IMMEDIATE')
        if kind=='analyze':
            prior=c.execute("SELECT id FROM ai_jobs WHERE kind='analyze' AND media_type=? AND media_id=? AND state IN ('queued','running')",(media_type,media_id)).fetchone()
            if prior:return prior['id']
            c.execute("INSERT INTO ai_metadata(media_type,media_id,state,updated_at) VALUES(?,?,'pending',?) ON CONFLICT(media_type,media_id) DO UPDATE SET state='pending',error=NULL,updated_at=excluded.updated_at",(media_type,media_id,now()))
        c.execute("INSERT INTO ai_jobs(id,kind,media_type,media_id,payload,state,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?)",(job_id,kind,media_type,media_id,json.dumps(payload or {}),stamp,stamp))
    return job_id


def manual_patch(root,media_type,media_id,patch,expected_revision=None):
    locate(root,media_type,media_id)
    allowed={'description','keywords','objects','actions','environment','lighting','mood','shot','camera_movement',
             'ocr','transcript','rights_confirmed','review_reason','duplicate_decision','custom','aliases'}
    if set(patch)-allowed:raise HTTPException(400,'Unsupported metadata field')
    if len(json.dumps(patch))>16000:raise HTTPException(413,'Metadata edit too large')
    for key,value in patch.items():
        if key=='rights_confirmed' and not isinstance(value,bool):raise HTTPException(400,'rights_confirmed must be boolean')
        if key=='keywords' and (not isinstance(value,list) or len(value)>60 or any(not isinstance(x,str) or len(x)>80 for x in value)):raise HTTPException(400,'Use at most 60 text keywords')
        if key in ('custom','aliases') and not isinstance(value,dict):raise HTTPException(400,'Custom fields and aliases must be objects')
        if key not in ('rights_confirmed','keywords','custom','aliases') and (not isinstance(value,str) or len(value)>2000):raise HTTPException(400,'Metadata text must be under 2000 characters')
    with connect(root) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("INSERT OR IGNORE INTO ai_metadata(media_type,media_id,state,updated_at) VALUES(?,?,'manual',?)",(media_type,media_id,now()))
        row=c.execute('SELECT * FROM ai_metadata WHERE media_type=? AND media_id=?',(media_type,media_id)).fetchone()
        if expected_revision is not None and row['revision']!=expected_revision:raise HTTPException(409,'Metadata changed; reload before saving')
        before=unpack(row['manual_json'],{});after={**before,**patch}
        c.execute('INSERT INTO ai_metadata_history(media_type,media_id,origin,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',(media_type,media_id,'manual',json.dumps(before),json.dumps(after),now()))
        c.execute('UPDATE ai_metadata SET manual_json=?,revision=revision+1,updated_at=? WHERE media_type=? AND media_id=?',(json.dumps(after),now(),media_type,media_id))
    if model_ready(Path(root)/'ai_models'):
        enqueue(root,'manual_index',media_type,media_id,{'text':json.dumps(after),'revision':row['revision']+1})
    return metadata(root,media_type,media_id)


def run_worker(root):
    from ai_engine import ClipEngine
    root=Path(root)
    with (root/'.ai-worker.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        with connect(root) as c:
            c.execute("UPDATE ai_jobs SET state='queued',updated_at=? WHERE state='running'",(time.time(),))
        engine=None
        while True:
            config=settings(root)
            if config.get('ai_engine_enabled')!='true':time.sleep(.5);continue
            with connect(root) as c:
                c.execute('BEGIN IMMEDIATE')
                job=c.execute("SELECT * FROM ai_jobs WHERE state='queued' ORDER BY CASE kind WHEN 'query' THEN 0 WHEN 'reference' THEN 1 ELSE 2 END,created_at LIMIT 1").fetchone()
                if job:c.execute("UPDATE ai_jobs SET state='running',attempts=attempts+1,updated_at=? WHERE id=?",(time.time(),job['id']))
            if not job:time.sleep(.3);continue
            try:
                if engine is None:engine=ClipEngine(root/'ai_models')
                payload=unpack(job['payload'],{})
                if job['kind']=='query':result={'embedding':engine.embed_text([payload['query']])[0].tolist(),'model_version':VERSION}
                elif job['kind']=='manual_index':
                    vector=engine.embed_text([payload['text'][:1000]])[0].tolist()
                    with connect(root) as c:
                        c.execute('UPDATE ai_metadata SET manual_embedding_json=? WHERE media_type=? AND media_id=? AND manual_json=?',(json.dumps(vector),job['media_type'],job['media_id'],payload['text']))
                    result={'indexed':True}
                elif job['kind']=='reference':
                    from PIL import Image
                    path=root/'ai_references'/payload['filename']
                    with Image.open(path) as image:result={'embedding':engine.embed_image(image).tolist(),'model_version':VERSION}
                    path.unlink(missing_ok=True)
                else:
                    asset=locate(root,job['media_type'],job['media_id'])
                    if not Path(asset['path']).is_file():raise RuntimeError('Local media unavailable; restore verified Drive copy before analysis')
                    contact=root/'ai_previews'/(job['media_id']+'-contact.jpg')
                    source_audio=root/'sources'/(asset.get('source_stored_name') or '')
                    original_audio=source_audio if source_audio.is_file() else None
                    result=engine.analyze(asset['path'],job['media_type'],asset.get('duration') or 0,contact,audio_path=original_audio,audio_start=float(asset.get('start_time') or 0) if original_audio else 0)
                    result['contact_sheet']='/media/ai-previews/'+contact.name
                    result['asset_id']=job['media_id'];result['analyzed_at']=now()
                    for group,entries in result['fields'].items():
                        for entry in entries:entry.update(asset_id=job['media_id'],analyzed_at=result['analyzed_at'],range_start=asset.get('start_time',0),range_end=asset.get('end_time',asset.get('duration',0)))
                    vector=result.pop('embedding')
                    searchable=' '.join(result.get(k,{}).get('text','') for k in ('ocr','transcript'))
                    text_vector=engine.embed_text([searchable[:2000]])[0].tolist() if searchable.strip() else None
                    # Recheck trash and cancellation under the write transaction.
                    locate(root,job['media_type'],job['media_id'])
                    with connect(root) as c:
                        c.execute('BEGIN IMMEDIATE')
                        if c.execute('SELECT state FROM ai_jobs WHERE id=?',(job['id'],)).fetchone()['state']=='cancelled':continue
                        old=c.execute('SELECT machine_json FROM ai_metadata WHERE media_type=? AND media_id=?',(job['media_type'],job['media_id'])).fetchone()
                        c.execute('INSERT INTO ai_metadata_history(media_type,media_id,origin,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',(job['media_type'],job['media_id'],'generated',old['machine_json'] if old else '{}',json.dumps(result),now()))
                        c.execute("UPDATE ai_metadata SET state='complete',machine_json=?,embedding_json=?,text_embedding_json=?,model_version=?,error=NULL,revision=revision+1,analyzed_at=?,updated_at=? WHERE media_type=? AND media_id=?",(json.dumps(result),json.dumps(vector),json.dumps(text_vector) if text_vector else None,VERSION,now(),now(),job['media_type'],job['media_id']))
                        if job['media_type']=='video':c.execute("UPDATE clips SET ai_state='complete' WHERE id=?",(job['media_id'],))
                    result={'media_type':job['media_type'],'media_id':job['media_id'],'model_version':VERSION}
                with connect(root) as c:c.execute("UPDATE ai_jobs SET state='completed',result_json=?,error=NULL,updated_at=? WHERE id=? AND state='running'",(json.dumps(result),time.time(),job['id']))
            except Exception as exc:
                # Do not include provider tokens, request payloads or filesystem paths in logs.
                detail='Analysis failed: '+type(exc).__name__
                if isinstance(exc,RuntimeError) and str(exc).startswith(('Local media','Local visual')):detail=str(exc)
                with connect(root) as c:
                    c.execute("UPDATE ai_jobs SET state='failed',error=?,updated_at=? WHERE id=? AND state='running'",(detail,time.time(),job['id']))
                    if job['kind']=='analyze':c.execute("UPDATE ai_metadata SET state='failed',error=?,updated_at=? WHERE media_type=? AND media_id=?",(detail,now(),job['media_type'],job['media_id']))


class Workspace:
    def __init__(self,root,housekeeping_callback=None):
        self.housekeeping_callback=housekeeping_callback
        self.root=Path(root);self.stop=threading.Event();self.thread=None;self.process=None
    def start(self):
        initialize(self.root)
        if os.getenv('SML_AI_WORKER','1')=='0':return
        self.thread=threading.Thread(target=self.supervise,daemon=True);self.thread.start()
    def close(self):
        self.stop.set()
        if self.process and self.process.poll() is None:self.process.terminate()
        if self.thread:self.thread.join(timeout=6)
        if self.process:
            with contextlib.suppress(subprocess.TimeoutExpired):self.process.wait(timeout=4)
            if self.process.poll() is None:self.process.kill();self.process.wait(timeout=3)
    def supervise(self):
        last_scan=0;last_housekeeping=0
        while not self.stop.wait(1):
            try:
                ready=model_ready(self.root/'ai_models');config=settings(self.root)
                if ready and config.get('ai_engine_enabled')=='true':
                    if not self.process or self.process.poll() is not None:
                        self.process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',str(self.root)],env={**os.environ,'OMP_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'2','TOKENIZERS_PARALLELISM':'false'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    rss=0
                    with contextlib.suppress(OSError,ValueError):
                        rss=int(next(line.split()[1] for line in Path('/proc',str(self.process.pid),'status').read_text().splitlines() if line.startswith('VmRSS:')))
                    with connect(self.root) as c:
                        if rss>1536*1024:
                            self.process.kill();self.process.wait(timeout=3)
                            c.execute("UPDATE ai_jobs SET state='failed',error='Analysis memory budget exceeded',updated_at=? WHERE state='running'",(time.time(),))
                        active=c.execute("SELECT id,media_type,media_id FROM ai_jobs WHERE state='running' AND updated_at<?",(time.time()-int(config.get('ai_job_timeout','120')),)).fetchall()
                        if active:
                            self.process.kill();self.process.wait(timeout=3)
                            for row in active:
                                c.execute("UPDATE ai_jobs SET state='failed',error='Worker timeout; retry with smaller media',updated_at=? WHERE id=?",(time.time(),row['id']))
                                c.execute("UPDATE ai_metadata SET state='failed',error='Worker timeout' WHERE media_type=? AND media_id=?",(row['media_type'],row['media_id']))
                    if config.get('ai_auto')=='true' and time.time()-last_scan>5:
                        last_scan=time.time();self.enqueue_missing(int(config.get('ai_batch_limit','32')))
                if self.housekeeping_callback and time.time()-last_housekeeping>15:
                    last_housekeeping=time.time();self.housekeeping_callback()
                # Remove only generated search reference uploads after 12h; never model/media.
                with connect(self.root) as c:
                    retained={unpack(r['payload'],{}).get('filename') for r in c.execute("SELECT payload FROM ai_jobs WHERE kind='reference' AND state IN ('queued','running')")}
                for p in (self.root/'ai_references').glob('*'):
                    if p.name not in retained and p.is_file() and time.time()-p.stat().st_mtime>43200:p.unlink(missing_ok=True)
            except (OSError,sqlite3.Error,ValueError):self.stop.wait(2)
    def enqueue_missing(self,limit):
        with connect(self.root) as c:
            indexed={(r['media_type'],r['media_id']) for r in c.execute('SELECT media_type,media_id,state,model_version FROM ai_metadata') if r['state'] in ('failed','cancelled') or r['state']=='complete' and r['model_version']==VERSION}
            busy=c.execute("SELECT COUNT(*) FROM ai_jobs WHERE state IN ('queued','running')").fetchone()[0]
        n=0
        for asset in catalog(self.root):
            if n+busy>=limit:break
            if (asset['type'],asset['id']) not in indexed:
                enqueue(self.root,'analyze',asset['type'],asset['id']);n+=1
        return n
    def status(self):
        config=settings(self.root);ready=model_ready(self.root/'ai_models')
        with connect(self.root) as c:
            counts={r['state']:r['n'] for r in c.execute('SELECT state,COUNT(*) n FROM ai_metadata GROUP BY state')}
            jobs={r['state']:r['n'] for r in c.execute('SELECT state,COUNT(*) n FROM ai_jobs GROUP BY state')}
        assets=catalog(self.root)
        with connect(self.root) as c:used={r['media_id'] for r in c.execute("SELECT DISTINCT media_id FROM media_reservations WHERE state='committed'")}
        totals={'images':sum(a['type']=='image' for a in assets),'clips':sum(a['type']=='video' for a in assets),'approved_unused':sum(a['status']=='approved' and a['id'] not in used for a in assets),'exact_duplicate_review':sum(a.get('duplicate_state')=='duplicate' for a in assets)}
        return {'totals':totals,'free_disk_bytes':__import__('shutil').disk_usage(self.root).free,'model':MODEL,'model_version':VERSION,'ready':ready,'enabled':config.get('ai_engine_enabled')=='true',
            'state':'AI Disabled' if config.get('ai_engine_enabled')!='true' else 'Ready' if ready else 'AI Disabled — local model not installed',
            'worker_running':bool(self.process and self.process.poll() is None),'counts':counts,'jobs':jobs,
            'threads':2,'max_worker_rss_mb':1536,'max_catalog':5000,'max_batch':int(config.get('ai_batch_limit','32')),
            'job_timeout_seconds':int(config.get('ai_job_timeout','120')),
            'ocr_available':bool(__import__('shutil').which('tesseract')),'speech_available':__import__('ai_text').speech_ready(self.root/'speech_models'),
            'unsupported':['face identity','face clustering (disabled)','camera motion inference','subject bounding boxes'],
            'score_note':'CLIP candidate-relative suggestions, not calibrated probabilities. Review actions/mood/lighting.',
            'drive_cleanup':'Blocked when Drive disabled; original media and models retained'}
    def require_ready(self):
        status=self.status()
        if not status['enabled'] or not status['ready']:raise HTTPException(503,status['state'])
    def query_vector(self,query):
        self.require_ready();key=hashlib.sha256((VERSION+query).encode()).hexdigest()
        with connect(self.root) as c:
            cached=c.execute("SELECT result_json FROM ai_jobs WHERE id=? AND state='completed'",(key,)).fetchone()
            if cached:return unpack(cached['result_json'])['embedding']
            c.execute("INSERT OR IGNORE INTO ai_jobs(id,kind,payload,state,created_at,updated_at) VALUES(?,'query',?,'queued',?,?)",(key,json.dumps({'query':query}),time.time(),time.time()))
        deadline=time.monotonic()+18
        while time.monotonic()<deadline:
            with connect(self.root) as c:row=c.execute('SELECT * FROM ai_jobs WHERE id=?',(key,)).fetchone()
            if row['state']=='completed':return unpack(row['result_json'])['embedding']
            if row['state']=='failed':raise HTTPException(503,row['error'])
            time.sleep(.15)
        raise HTTPException(503,'AI worker busy; search is queued, retry shortly')
    def search(self,query='',vector=None,media_type='all',status='all',orientation='all',min_quality=0,min_duration=0,max_duration=36000,
               min_width=0,codec='',min_fps=0,subject='',action='',environment='',lighting='',mood='',source='',used='all',duplicate='all',date_from='',date_to='',rights_only=False,visual_only=False,limit=60):
        if len(query)>500:raise HTTPException(400,'Query too long')
        if query and vector is None:vector=self.query_vector(query)
        with connect(self.root) as c:
            records={(r['media_type'],r['media_id']):dict(r) for r in c.execute('SELECT * FROM ai_metadata')}
            usage={r['media_id']:r['n'] for r in c.execute("SELECT media_id,COUNT(*) n FROM media_reservations WHERE state='committed' GROUP BY media_id")}
        items=[]
        for a in catalog(self.root,media_type):
            r=records.get((a['type'],a['id']),{});machine=unpack(r.get('machine_json'),{});manual=unpack(r.get('manual_json'),{})
            effective={**machine,**manual};tags=' '.join(str(effective.get(k,machine.get('fields',{}).get(k,''))) for k in ('objects','actions','environment','lighting','mood','keywords')).lower()
            tags+=' '+json.dumps(manual.get('aliases',{})).lower()
            q=machine.get('quality',{}).get('score',a.get('quality') or 0)
            count=usage.get(a['id'],0);rights=manual.get('rights_confirmed',bool(a.get('rights_confirmed')))
            if status!='all' and a['status']!=status:continue
            if orientation!='all' and a.get('orientation')!=orientation:continue
            if not min_duration<=float(a.get('duration') or 0)<=max_duration or q<min_quality:continue
            if (a.get('width') or 0)<min_width or (a.get('fps') or 0)<min_fps:continue
            if codec and a.get('codec')!=codec:continue
            if source and source not in (a.get('source_kind') or '')+' '+(a.get('source_id') or ''):continue
            if date_from and (a.get('created_at') or '')<date_from:continue
            if date_to and (a.get('created_at') or '')[:10]>date_to:continue
            if used=='unused' and count or used=='used' and not count:continue
            if rights_only and not rights:continue
            if any(term and term.lower() not in tags for term in (subject,action,environment,lighting,mood)):continue
            if duplicate=='exact' and a.get('duplicate_state')!='duplicate':continue
            if duplicate=='clear' and a.get('duplicate_state')=='duplicate':continue
            emb=unpack(r.get('embedding_json'),[])
            if vector and (r.get('model_version')!=VERSION or not emb):continue
            similarity=cosine(vector,emb) if vector else 0
            if vector and rights_only and similarity<.20:continue
            text_similarity=max(cosine(vector,unpack(r.get('manual_embedding_json'),[])),cosine(vector,unpack(r.get('text_embedding_json'),[]))) if vector and not visual_only else 0
            text=(str(effective.get('description') or '')+' '+tags+' '+json.dumps(effective.get('ocr',{}))+' '+json.dumps(effective.get('transcript',{}))).lower()
            terms=query.lower().split();lexical=sum(t in text for t in terms)/max(1,len(terms))
            score=.85*similarity+.10*(q/100)+.05*(.5*lexical+.5*max(0.,(text_similarity-.4)/.6)) if vector else q/100
            items.append({**public_asset(a),'description':effective.get('description',a.get('description')),
                'ai_state':r.get('state','pending'),'model_version':r.get('model_version',''),'quality':q,
                'rights_confirmed':bool(rights),'usage_count':count,'semantic_similarity':round(similarity,5),
                'score':round(score,5),'metadata':effective,'manual_fields':list(manual),'revision':r.get('revision',0)})
        items.sort(key=lambda x:(x['score'],x['id']),reverse=True)
        return {'items':items[:limit],'total':len(items),'query':query,'model_version':VERSION,
                'ranking':'85% visual-text cosine + 10% visual quality + 5% metadata relevance (lexical and text-index); filters applied first',
                'index_limit':5000,'semantic':bool(vector)}


class AnalyzeRequest(BaseModel):
    media_type:str='all'
    ids:list[str]=Field(default_factory=list,max_length=100)
    reindex:bool=False
class MetadataEdit(BaseModel):
    fields:dict
    expected_revision:int|None=None
class SavedSearch(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    query:str=Field(default='',max_length=500)
    filters:dict=Field(default_factory=dict)
class AIConfig(BaseModel):
    enabled:bool=True
    batch_limit:int=Field(default=32,ge=1,le=64)
    job_timeout:int=Field(default=120,ge=30,le=300)


def register_ai_workspace(app,root,housekeeping_callback=None):
    workspace=Workspace(root,housekeeping_callback);root=Path(root);app.state.ai_workspace=workspace
    app.router.add_event_handler('startup',workspace.start);app.router.add_event_handler('shutdown',workspace.close)
    @app.get('/api/v1/ai/status')
    def ai_status():return workspace.status()
    @app.patch('/api/v1/ai/settings')
    def ai_settings(body:AIConfig):
        with connect(root) as c:
            for key,value in {'ai_engine_enabled':str(body.enabled).lower(),'ai_batch_limit':str(body.batch_limit),'ai_job_timeout':str(body.job_timeout)}.items():
                c.execute('UPDATE settings SET value=? WHERE key=?',(value,key))
        return workspace.status()
    @app.post('/api/v1/ai/analyze')
    def analyze(body:AnalyzeRequest):
        workspace.require_ready()
        if body.media_type not in ('all','video','image','source'):raise HTTPException(400,'Invalid media type')
        assets=catalog(root,body.media_type)
        if body.ids:assets=[x for x in assets if x['id'] in body.ids]
        if body.reindex:
            with connect(root) as c:
                for a in assets:c.execute("UPDATE ai_metadata SET state='reindex_pending' WHERE media_type=? AND media_id=?",(a['type'],a['id']))
        cap=int(settings(root).get('ai_batch_limit','32'))
        return {'jobs':[enqueue(root,'analyze',a['type'],a['id']) for a in assets[:cap]],'remaining':max(0,len(assets)-cap)}
    @app.get('/api/v1/ai/jobs')
    def jobs():
        with connect(root) as c:
            rows=c.execute('SELECT id,kind,media_type,media_id,state,attempts,error,created_at,updated_at FROM ai_jobs ORDER BY created_at DESC LIMIT 100').fetchall()
        return {'items':[dict(x) for x in rows]}
    @app.post('/api/v1/ai/jobs/{job_id}/{action}')
    def job_action(job_id:str,action:str):
        if action not in ('retry','cancel'):raise HTTPException(400,'Use retry or cancel')
        with connect(root) as c:
            row=c.execute('SELECT * FROM ai_jobs WHERE id=?',(job_id,)).fetchone()
            if not row:raise HTTPException(404,'Job not found')
            if action=='retry':
                if row['state'] not in ('failed','cancelled') or row['attempts']>=3:raise HTTPException(409,'Retry allowed for failed/cancelled jobs up to three attempts')
                c.execute("UPDATE ai_jobs SET state='queued',error=NULL,updated_at=? WHERE id=?",(time.time(),job_id))
            else:c.execute("UPDATE ai_jobs SET state='cancelled',updated_at=? WHERE id=? AND state IN ('queued','running')",(time.time(),job_id))
        return {'ok':True}
    @app.get('/api/v1/ai/search')
    def search(query:str='',media_type:str='all',status:str='all',orientation:str='all',min_quality:int=0,min_duration:float=0,max_duration:float=36000,
               min_width:int=0,codec:str='',min_fps:float=0,subject:str='',action:str='',environment:str='',lighting:str='',mood:str='',source:str='',used:str='all',duplicate:str='all',date_from:str='',date_to:str='',limit:int=Query(60,ge=1,le=200)):
        if media_type not in ('all','video','image'):raise HTTPException(400,'Invalid media type')
        return workspace.search(**{k:v for k,v in locals().items() if k!="workspace"})
    @app.get('/api/v1/ai/assets/{media_type}/{media_id}')
    def asset_metadata(media_type:str,media_id:str):return metadata(root,media_type,media_id)
    @app.patch('/api/v1/ai/assets/{media_type}/{media_id}')
    def edit_metadata(media_type:str,media_id:str,body:MetadataEdit):return manual_patch(root,media_type,media_id,body.fields,body.expected_revision)
    @app.get('/api/v1/ai/assets/{media_type}/{media_id}/history')
    def history(media_type:str,media_id:str):
        locate(root,media_type,media_id)
        with connect(root) as c:rows=c.execute('SELECT * FROM ai_metadata_history WHERE media_type=? AND media_id=? ORDER BY id DESC LIMIT 50',(media_type,media_id)).fetchall()
        return {'items':[dict(x) for x in rows]}
    @app.get('/api/v1/ai/assets/{media_type}/{media_id}/similar')
    def similar(media_type:str,media_id:str):
        locate(root,media_type,media_id)
        with connect(root) as c:row=c.execute('SELECT * FROM ai_metadata WHERE media_type=? AND media_id=?',(media_type,media_id)).fetchone()
        if not row or not row['embedding_json']:raise HTTPException(409,'Analyze this asset first')
        result=workspace.search(vector=unpack(row['embedding_json']),visual_only=True,limit=61)
        result['items']=[x for x in result['items'] if x['id']!=media_id][:60];return result
    @app.post('/api/v1/ai/reference')
    async def reference(file:UploadFile=File(...)):
        workspace.require_ready();folder=root/'ai_references';folder.mkdir(exist_ok=True)
        name=uuid.uuid4().hex+'.image';path=folder/name
        try:
            total=0
            with path.open('wb') as f:
                while chunk:=await file.read(1024*1024):
                    total+=len(chunk)
                    if total>15*1024**2:raise HTTPException(413,'Reference image limit is 15 MB')
                    f.write(chunk)
            from PIL import Image
            with Image.open(path) as im:
                if im.width*im.height>40000000:raise HTTPException(413,'Reference image dimensions exceed memory budget')
                im.verify()
            job=enqueue(root,'reference',payload={'filename':name})
            return {'job_id':job}
        except Exception:
            path.unlink(missing_ok=True);raise
        finally:await file.close()
    @app.get('/api/v1/ai/reference/{job_id}')
    def reference_result(job_id:str):
        with connect(root) as c:row=c.execute("SELECT * FROM ai_jobs WHERE id=? AND kind='reference'",(job_id,)).fetchone()
        if not row:raise HTTPException(404,'Reference search not found')
        if row['state']!='completed':return {'state':row['state'],'error':row['error'],'items':[]}
        return {'state':'completed',**workspace.search(vector=unpack(row['result_json'])['embedding'],visual_only=True)}
    @app.get('/api/v1/ai/duplicates')
    def duplicates(limit: int = Query(40, ge=1, le=200), offset: int = Query(0, ge=0)):
        from duplicate_review import review_duplicates

        # One SQLite read for indexed AI fingerprints; exact hash matching
        # continues to work before AI model setup or inference.
        assets = catalog(root)
        with connect(root) as c:
            records = {(row['media_type'], row['media_id']): dict(row)
                       for row in c.execute(
                           'SELECT media_type,media_id,machine_json,embedding_json,model_version FROM ai_metadata')}
        result = review_duplicates(assets, records, public_asset, limit=limit, offset=offset)
        result['catalog_limit_per_type'] = 5000
        result['coverage_note'] = (
            'This review covers the newest 5000 assets of each media type. '
            'No deletion is automatic; review suggestions before making any changes.')
        return result
    @app.get('/api/v1/ai/collections')
    def collections():
        with connect(root) as c:rows=c.execute('SELECT * FROM ai_collections ORDER BY created_at DESC').fetchall()
        return {'items':[dict(r) for r in rows]}
    @app.post('/api/v1/ai/collections')
    def save_collection(body:SavedSearch):
        ident=uuid.uuid4().hex
        with connect(root) as c:c.execute('INSERT INTO ai_collections VALUES(?,?,?,?,?)',(ident,body.name,body.query,json.dumps(body.filters),now()))
        return {'id':ident}
    @app.get('/api/v1/ai/cleanup/status')
    def cleanup_status():
        config=settings(root)
        with connect(root) as c:
            active=c.execute("SELECT COUNT(*) FROM ai_jobs WHERE state IN ('queued','running')").fetchone()[0]
            retained=c.execute("SELECT COUNT(*) FROM sources WHERE source_deleted_at IS NULL").fetchone()[0]
        return {'owner':'self-media-library','drive_enabled':config.get('drive_enabled')=='true','active_ai_jobs':active,
                'retained_sources':retained,'rule':'Complete manual pair selection + local SHA-256/decode verification + no active dependencies',
                'protected':['originals pending complete verified selection','model cache','databases','credentials','approved library assets']}
    @app.get('/api/v1/ai/usage/{media_id}')
    def usage(media_id:str):
        with connect(root) as c:rows=c.execute('SELECT project_id,scene_id,state,created_at,expires_at,committed_at FROM media_reservations WHERE media_id=? ORDER BY created_at DESC LIMIT 200',(media_id,)).fetchall()
        return {'items':[dict(x) for x in rows]}
    return workspace


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--worker':
        os.nice(10);run_worker(Path(sys.argv[2]))

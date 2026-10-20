"""Standalone Edge TTS API for Voice Studio. Separate SQLite state and worker; no OmniVoice/Kokoro coupling."""
from __future__ import annotations
import json, os, re, sqlite3, time, uuid, unicodedata
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from queue_actions import migrate as migrate_queue, apply as apply_queue, job_view, QueueActionError

ROOT=Path(os.environ.get('EDGE_STUDIO_ROOT','/home/ubuntu/apps/edge_tts_voice_studio'))
DATA=ROOT/'data'; DB=DATA/'edge_jobs.sqlite3'; VOICES=DATA/'voice_catalog.json'; HB=DATA/'heartbeat.json'
router=APIRouter(prefix='/tts/api/edge',tags=['Edge TTS Studio'])
ID_RE=re.compile(r'^EDJ-[A-F0-9]{16}$')
LANG_RE=re.compile(r'^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){1,3}$')

def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def db():
    c=sqlite3.connect(str(DB),timeout=25)
    c.row_factory=sqlite3.Row
    c.execute('pragma busy_timeout=20000')
    return c

def init_storage():
    (DATA/'jobs').mkdir(parents=True,exist_ok=True)
    with db() as c:
        c.execute('pragma journal_mode=WAL')
        c.execute('''create table if not exists jobs(
          job_id text primary key,batch_id text not null,source_filename text not null,output_basename text not null,
          voice text not null,locale text not null,rate integer not null,pitch integer not null,volume integer not null,
          status text not null,delivery_status text not null,created_at text not null,updated_at text not null,
          input_file text not null,mp3_file text,wav_file text,srt_file text,error text,delivery_error text,
          progress integer default 0,attempt integer default 0,retry_at real default 0,
          delivery_retries integer default 0,next_delivery_retry real default 0,
          drive_txt text,drive_mp3 text,drive_wav text,drive_srt text)''')
        c.execute('create index if not exists edge_job_queue on jobs(status,retry_at,created_at)')
        migrate_output_metadata(c)
    with db() as c:
        migrate_queue(c, 'edge')

def safe_name(s,maxlen=70):
    s=unicodedata.normalize('NFKC', s or '')
    s=re.sub(r'[^\w-]+', '_', s, flags=re.UNICODE).strip('_-')[:maxlen].strip('_-')
    return s or 'narration'


def choose_output_name(custom_name: str, source_filename: str, text: str, job_id: str, source_basename: str | None = None):
    """Select a safe human-readable basename for one new job only."""
    if custom_name and custom_name.strip():
        return safe_name(custom_name), 'custom'
    if source_filename and source_filename.lower().endswith('.txt'):
        stem = source_basename or source_filename.replace('\\', '/').rsplit('/', 1)[-1][:-4]
        if stem.strip():
            return safe_name(stem), 'source_txt'
    letters = [ch for ch in unicodedata.normalize('NFKC', text) if unicodedata.category(ch).startswith('L')]
    prefix = safe_name(''.join(letters[:2]) or 'Au', 16)
    return prefix + '_' + job_id, 'text_fallback'

_METADATA_COLUMNS = {
    'naming_method': "TEXT NOT NULL DEFAULT 'legacy'",
    'drive_file_id': 'TEXT', 'drive_url': 'TEXT', 'drive_verified_at': 'TEXT',
    'drive_files_json': "TEXT NOT NULL DEFAULT '{}'",
}

def migrate_output_metadata(con):
    columns = {r[1] for r in con.execute('PRAGMA table_info(jobs)').fetchall()}
    for name, data_type in _METADATA_COLUMNS.items():
        if name not in columns:
            con.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + data_type)

def drive_files(row):
    try:
        raw = json.loads(row.get('drive_files_json') or '{}')
        return raw if isinstance(raw, dict) else {}
    except (TypeError, ValueError):
        return {}

def catalog():
    try:
        j=json.loads(VOICES.read_text(encoding='utf8'))
        if time.time()-float(j.get('epoch',0))>86400:return []
        voices=j.get('voices',[])
        return voices if isinstance(voices,list) else []
    except (ValueError,TypeError,OSError):return []

def readiness():
    try:
        hb=json.loads(HB.read_text(encoding='utf8'))
        if time.time()-float(hb.get('epoch',0))>50:return False,'worker_heartbeat_stale'
        if not hb.get('ready'):return False,hb.get('reason','worker_not_ready')
        if not catalog():return False,'voice_catalog_unavailable'
        return True,''
    except (ValueError,TypeError,OSError):return False,'worker_offline'

def normalize_voice(voice,locale):
    known={v['id']:v for v in catalog() if isinstance(v,dict) and v.get('id')}
    if voice not in known:raise HTTPException(422,'Voice is unavailable in current catalog')
    true_locale=known[voice]['locale']
    if locale and locale!=true_locale:raise HTTPException(422,'Voice/locale mismatch')
    return true_locale

def settings(voice,locale,rate,pitch,volume):
    ready,reason=readiness()
    if not ready:raise HTTPException(503,'Edge TTS offline: '+reason)
    locale=normalize_voice(voice,locale)
    if not (-50<=rate<=100 and -50<=pitch<=50 and -50<=volume<=100):
        raise HTTPException(422,'Rate, pitch or volume outside supported range')
    return locale

class GenerateRequest(BaseModel):
    text:str=Field(min_length=1,max_length=50000)
    voice:str=Field(min_length=3,max_length=100)
    locale:str=''
    rate:int=Field(default=0,ge=-50,le=100)
    pitch:int=Field(default=0,ge=-50,le=50)
    volume:int=Field(default=0,ge=-50,le=100)
    batch_name:str=Field(default='',max_length=100)
    output_name:str=Field(default='',max_length=120)

def create_job(text,filename,voice,locale,rate,pitch,volume,batch_name,stem=None,custom_name=''):
    text=text.strip()
    if not text or len(text)>50000:raise HTTPException(422,'TXT must contain 1–50000 characters')
    jid='EDJ-'+uuid.uuid4().hex[:16].upper()
    base,naming_method=choose_output_name(custom_name,filename,text,jid,stem)
    folder=DATA/'jobs'/jid
    folder.mkdir(mode=0o750,parents=True,exist_ok=False)
    src=folder/(base+'.txt')
    src.write_text(text+'\n',encoding='utf-8')
    with db() as c:
        c.execute('''insert into jobs(job_id,batch_id,source_filename,output_basename,voice,locale,rate,pitch,volume,status,delivery_status,created_at,updated_at,input_file,naming_method)
             values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(jid,batch_name,filename,base,voice,locale,rate,pitch,volume,'queued','waiting_generation',now(),now(),str(src),naming_method))
    return jid

@router.get('/status')
def status():
    init_storage();ready,reason=readiness()
    with db() as c:rows=c.execute('select status,count(*) n from jobs group by status').fetchall()
    return {'ready':ready,'reason':reason,'mode':'edge-tts-online','queue':{r['status']:r['n'] for r in rows},'output_formats':['mp3','wav','txt','srt'],'controls':['pause','resume','cancel','skip','retry','delete']}

@router.get('/voices')
def voices():
    return {'voices':catalog(),'source':'live_service_cache','ready':readiness()[0]}

@router.post('/generate')
def generate(payload:GenerateRequest):
    init_storage();loc=settings(payload.voice,payload.locale,payload.rate,payload.pitch,payload.volume)
    batch=safe_name(payload.batch_name or 'single',40)+'-'+uuid.uuid4().hex[:8]
    jid=create_job(payload.text,'text-input',payload.voice,loc,payload.rate,payload.pitch,payload.volume,batch,custom_name=payload.output_name)
    return {'job_id':jid,'count':1}

@router.post('/batch')
async def batch(request:Request):
    init_storage()
    raw=await request.body()
    if len(raw)>10_000_000:raise HTTPException(413,'Batch exceeds 10 MB')
    ct=request.headers.get('content-type','')
    if 'multipart/form-data' not in ct.lower() or 'boundary=' not in ct.lower():raise HTTPException(415,'multipart/form-data required')
    try:message=BytesParser(policy=policy.default).parsebytes(b'MIME-Version: 1.0\r\nContent-Type: '+ct.encode('ascii')+b'\r\n\r\n'+raw)
    except Exception:raise HTTPException(400,'Invalid multipart form')
    fields={};items=[]
    for part in message.iter_parts():
        key=part.get_param('name',header='content-disposition')
        filename=part.get_filename()
        data=part.get_payload(decode=True) or b''
        if key=='files':
            if not filename or not filename.lower().endswith('.txt') or len(data)>2_200_000:raise HTTPException(422,'Only TXT files up to 2.2 MB supported')
            try:txt=data.decode('utf-8-sig')
            except UnicodeDecodeError:raise HTTPException(422,'TXT must use UTF-8')
            if not txt.strip() or len(txt.strip())>50000:raise HTTPException(422,'Invalid TXT length')
            items.append((Path(filename).name,txt))
        elif key in ('voice','locale','rate','pitch','volume','batch_name','output_name'):
            fields[key]=data.decode('utf-8',errors='replace')[:110]
    if not 1<=len(items)<=100:raise HTTPException(422,'Choose 1–100 TXT files')
    try:rate=int(fields.get('rate',0));pitch=int(fields.get('pitch',0));volume=int(fields.get('volume',0))
    except ValueError:raise HTTPException(422,'Invalid voice settings')
    voice=fields.get('voice','');loc=settings(voice,fields.get('locale',''),rate,pitch,volume)
    batch_id=safe_name(fields.get('batch_name','batch'),40)+'-'+uuid.uuid4().hex[:8]
    used=set();jobs=[]
    for filename,txt in items:
        stem=safe_name(Path(filename).stem)
        candidate=stem;i=2
        while candidate in used:candidate=f'{stem}_{i}';i+=1
        used.add(candidate)
        jobs.append(create_job(txt,filename,voice,loc,rate,pitch,volume,batch_id,candidate,custom_name=(fields.get('output_name','')+(('_'+str(len(jobs)+1)) if jobs else ''))))
    return {'batch_id':batch_id,'count':len(jobs),'job_ids':jobs}

@router.get('/jobs')
def jobs(limit:int=100):
    init_storage();limit=min(250,max(1,limit))
    with db() as c:rows=c.execute('select * from jobs where hidden_at is null order by created_at desc,job_id desc limit ?',(limit,)).fetchall()
    answer=[]
    for r in rows:
        v=job_view(r, 'edge')
        item={k:v.get(k) for k in ('job_id','batch_id','source_filename','output_basename','voice','locale','status','queue_status','delivery_status','created_at','updated_at','error','delivery_error','progress','attempt','max_attempts','allowed_actions','drive_mp3','drive_wav','drive_txt','drive_srt','naming_method','drive_file_id','drive_url','drive_verified_at','delivery_retries')}
        item['output_filename']=v['output_basename']+'.mp3'
        item['drive_files']=drive_files(v)
        for ext in ('mp3','wav','srt','txt'):
            path=v.get({'mp3':'mp3_file','wav':'wav_file','srt':'srt_file','txt':'input_file'}[ext])
            if path and Path(path).is_file():item[ext+'_url']='/tts/api/edge/files/'+v['job_id']+'/'+ext
        answer.append(item)
    return {'jobs':answer}

@router.post('/jobs/{job_id}/{action}')
def control(job_id:str,action:str):
    init_storage()
    try:
        with db() as c:
            return apply_queue(c, 'edge', job_id, action)
    except QueueActionError as exc:
        raise HTTPException(exc.code, exc.detail) from exc

@router.post('/jobs/{job_id}/delivery/retry')
def retry_delivery(job_id: str):
    init_storage()
    if not ID_RE.fullmatch(job_id):
        raise HTTPException(404, 'Job not found')
    with db() as c:
        row = c.execute('SELECT status,delivery_status,input_file,mp3_file FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Job not found')
        if row['status'] != 'completed' or row['delivery_status'] in ('delivered', 'cleaned'):
            raise HTTPException(409, 'Only completed unverified outputs can retry Drive delivery')
        if not row['input_file'] or not row['mp3_file'] or not Path(row['input_file']).is_file() or not Path(row['mp3_file']).is_file():
            raise HTTPException(409, 'Local audio/source unavailable; generation is not restarted')
        c.execute("UPDATE jobs SET delivery_status='retry_pending',delivery_error=NULL,next_delivery_retry=0 WHERE job_id=?", (job_id,))
    return {'job_id': job_id, 'delivery_status': 'retry_pending'}

@router.get('/files/{job_id}/{ext}')
def file(job_id:str,ext:str):
    init_storage()
    if not ID_RE.fullmatch(job_id) or ext not in ('mp3','wav','txt','srt'):raise HTTPException(404,'File not found')
    key={'mp3':'mp3_file','wav':'wav_file','srt':'srt_file','txt':'input_file'}[ext]
    with db() as c:r=c.execute('select '+key+',output_basename from jobs where job_id=?',(job_id,)).fetchone()
    if not r or not r[key] or not Path(r[key]).is_file():raise HTTPException(404,'File not available')
    return FileResponse(r[key],media_type={'mp3':'audio/mpeg','wav':'audio/wav','srt':'text/plain','txt':'text/plain'}[ext],filename=r['output_basename']+'.'+ext)


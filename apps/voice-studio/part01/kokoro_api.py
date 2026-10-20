"""Isolated Kokoro ONNX job API for Voice Studio. No OmniVoice database coupling."""
from __future__ import annotations
import json, os, re, sqlite3, uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from queue_actions import migrate as migrate_queue, apply as apply_queue, job_view, QueueActionError

ROOT=Path(os.environ.get('KOKORO_STUDIO_ROOT','/home/ubuntu/apps/kokoro_voice_studio'))
DATA=ROOT/'data'
DB=DATA/'kokoro_jobs.sqlite3'
MODELS=ROOT/'models'
router=APIRouter(prefix='/tts/api/kokoro',tags=['Kokoro Voice Studio'])

class GenerateRequest(BaseModel):
    text: str=Field(min_length=1,max_length=50000)
    language: str=Field(min_length=2,max_length=16)
    voice: str=Field(min_length=3,max_length=40)
    speed: float=Field(ge=0.5,le=2.0)
    batch_name: str=Field(default='',max_length=120)

def utc():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def safe_name(value:str,maxlen:int=85)->str:
    val=re.sub(r'[^A-Za-z0-9_-]+','_',value or '').strip('_-')[:maxlen]
    return val or 'untitled'

def connection():
    c=sqlite3.connect(str(DB),timeout=20)
    c.row_factory=sqlite3.Row
    c.execute('pragma busy_timeout=20000')
    return c

def init_storage():
    (DATA/'jobs').mkdir(parents=True,exist_ok=True)
    with connection() as c:
        c.execute('pragma journal_mode=WAL')
        c.execute('''create table if not exists jobs(
            job_id text primary key, batch_id text not null, source_filename text not null,
            output_basename text not null, language text not null, voice text not null,
            speed real not null, status text not null, delivery_status text not null,
            created_at text not null, started_at text, finished_at text, input_file text not null,
            wav_file text, error text, delivery_error text, next_delivery_retry real default 0,
            delivery_retries integer default 0, drive_wav text, drive_txt text)''')
        c.execute('create index if not exists kokoro_jobs_queue on jobs(status,created_at)')
    with connection() as c:
        migrate_queue(c, 'kokoro')

def catalog():
    p=ROOT/'voice_catalog.json'
    if not p.is_file():return []
    try:
        records=json.loads(p.read_text())
        return records if isinstance(records,list) else []
    except (OSError,ValueError):return []

def worker_status():
    if not (MODELS/'kokoro-v1.0.int8.onnx').is_file() or not (MODELS/'voices-v1.0.bin').is_file():
        return False,'model_missing'
    try:
        h=json.loads((DATA/'heartbeat.json').read_text())
        stamp=float(h.get('epoch',0))
        import time
        if time.time()-stamp>50:return False,'worker_heartbeat_stale'
        if h.get('ready') is not True:return False,h.get('reason','worker_not_ready')
        return True,''
    except (OSError,ValueError,TypeError):return False,'worker_offline'

def validate(language:str,voice:str,speed:float):
    allowed={(r['language'],r['id']) for r in catalog() if isinstance(r,dict) and 'id'in r and 'language'in r}
    if (language,voice) not in allowed:raise HTTPException(422,'Unsupported Kokoro voice/language combination')
    if not (0.5<=speed<=2.0):raise HTTPException(422,'Speed out of range')
    ready,reason=worker_status()
    if not ready:raise HTTPException(503,'Kokoro worker not ready: '+reason)

def create_job(text:str,filename:str,language:str,voice:str,speed:float,batch:str,basename:str|None=None):
    text=text.strip()
    if not text or len(text)>50000:raise HTTPException(422,'TXT must contain 1–50000 characters')
    job_id='KOJ-'+uuid.uuid4().hex[:16].upper()
    output=safe_name(basename or Path(filename).stem)
    where=DATA/'jobs'/job_id
    where.mkdir(mode=0o750,parents=True,exist_ok=False)
    txt=where/(output+'.txt')
    txt.write_text(text+'\n',encoding='utf-8')
    with connection() as c:
        c.execute('''insert into jobs(job_id,batch_id,source_filename,output_basename,language,voice,speed,status,delivery_status,created_at,input_file) values(?,?,?,?,?,?,?,?,?,?,?)''',
            (job_id,batch,filename,output,language,voice,speed,'queued','waiting_generation',utc(),str(txt)))
    return job_id

@router.get('/status')
def status():
    init_storage()
    ok,reason=worker_status()
    with connection() as c:
        rows=c.execute('select status,count(*) as n from jobs group by status').fetchall()
    return {'ready':ok,'reason':reason,'mode':'kokoro-onnx-cpu','queue':{r['status']:r['n'] for r in rows},'sample_rate':24000}

@router.get('/voices')
def voices():return {'voices':catalog()}

@router.post('/generate')
def generate(payload:GenerateRequest):
    init_storage()
    validate(payload.language,payload.voice,payload.speed)
    batch='single-'+uuid.uuid4().hex[:8]
    id_=create_job(payload.text,'narration.txt',payload.language,payload.voice,payload.speed,batch,basename='narration')
    return {'job_id':id_,'count':1}

@router.post('/batch')
async def batch(request:Request):
    init_storage()
    raw=await request.body()
    if len(raw)>10000000:raise HTTPException(413,'Batch payload exceeds 10 MB')
    ct=request.headers.get('content-type','')
    if not ct.lower().startswith('multipart/form-data;') or 'boundary=' not in ct.lower():raise HTTPException(415,'multipart/form-data required')
    blob=b'MIME-Version: 1.0\r\nContent-Type: '+ct.encode('ascii',errors='ignore')+b'\r\n\r\n'+raw
    try: parsed=BytesParser(policy=policy.default).parsebytes(blob)
    except Exception:raise HTTPException(400,'Invalid form data')
    fields={};txts=[]
    for part in parsed.iter_parts():
        name=part.get_param('name',header='content-disposition')
        filename=part.get_filename()
        data=part.get_payload(decode=True) or b''
        if name=='files':
            if not filename or not filename.lower().endswith('.txt'):raise HTTPException(422,'Only TXT files supported')
            if len(data)>220000:raise HTTPException(413,'Single TXT exceeds maximum')
            try: text=data.decode('utf-8-sig')
            except UnicodeDecodeError:raise HTTPException(422,'TXT must use UTF-8 encoding')
            txts.append((Path(filename).name,text))
        elif name in {'language','voice','speed','batch_name'}:
            fields[name]=data.decode('utf-8',errors='ignore')[:120]
    if not 1<=len(txts)<=100:raise HTTPException(422,'Provide 1–100 TXT files')
    try: speed=float(fields.get('speed','1'))
    except ValueError:raise HTTPException(422,'Invalid speed')
    language=fields.get('language','')
    voice=fields.get('voice','')
    validate(language,voice,speed)
    batch_id=safe_name(fields.get('batch_name') or 'batch',40)+'-'+uuid.uuid4().hex[:8]
    seen=set();normalized=[]
    for filename,text in txts:
        if not text.strip() or len(text.strip())>50000:raise HTTPException(422,'TXT must contain 1–50000 characters')
        stem=safe_name(Path(filename).stem)
        original=stem
        ix=2
        while stem in seen:
            stem=original+'_'+str(ix);ix+=1
        seen.add(stem);normalized.append((filename,text,stem))
    ids=[create_job(text,filename,language,voice,speed,batch_id,basename=stem) for filename,text,stem in normalized]
    return {'batch_id':batch_id,'count':len(ids),'job_ids':ids}

@router.get('/jobs')
def jobs(limit:int=100):
    init_storage()
    limit=max(1,min(limit,250))
    with connection() as c:
        rows=c.execute('select * from jobs where hidden_at is null order by created_at desc,job_id desc limit ?',(limit,)).fetchall()
    result=[]
    for r in rows:
        d=job_view(r, 'kokoro')
        out={k:d.get(k) for k in ('job_id','batch_id','source_filename','output_basename','language','voice','speed','status','queue_status','delivery_status','created_at','started_at','finished_at','error','delivery_error','progress','attempt','max_attempts','allowed_actions','drive_wav','drive_txt')}
        out['filename']=d['output_basename']+'.txt'
        if d['wav_file'] and Path(d['wav_file']).is_file():out['audio_url']='/tts/api/kokoro/audio/'+d['job_id']
        result.append(out)
    return {'jobs':result}

@router.post('/jobs/{job_id}/{action}')
def control(job_id:str,action:str):
    init_storage()
    try:
        with connection() as c:
            return apply_queue(c, 'kokoro', job_id, action)
    except QueueActionError as exc:
        raise HTTPException(exc.code, exc.detail) from exc

@router.get('/audio/{job_id}')
def audio(job_id:str):
    init_storage()
    if not re.fullmatch(r'KOJ-[A-F0-9]{16}',job_id):raise HTTPException(404,'Audio not found')
    with connection() as c:row=c.execute('select wav_file,output_basename from jobs where job_id=?',(job_id,)).fetchone()
    if not row or not row['wav_file'] or not Path(row['wav_file']).is_file():raise HTTPException(404,'Audio not available')
    return FileResponse(row['wav_file'],media_type='audio/wav',filename=row['output_basename']+'.wav')

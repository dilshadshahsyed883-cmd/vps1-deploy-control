"""Single-worker isolated Kokoro ONNX CPU synthesis + verified Drive spool."""
from __future__ import annotations
import json, os, re, shutil, sqlite3, subprocess, sys, threading, time, hashlib
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

ROOT=Path(os.environ.get('KOKORO_STUDIO_ROOT','/home/ubuntu/apps/kokoro_voice_studio'))
DATA=ROOT/'data';DB=DATA/'kokoro_jobs.sqlite3'
MODEL=ROOT/'models/kokoro-v1.0.int8.onnx'; VOICES=ROOT/'models/voices-v1.0.bin'
RCLONE=shutil.which('rclone') or '';RCFILE='/home/ubuntu/.config/masterctl/rclone.conf'
LANGS={'af':'en-us','am':'en-us','bf':'en-gb','bm':'en-gb','hf':'hi','hm':'hi'}

def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def db():
 c=sqlite3.connect(str(DB),timeout=25)
 c.row_factory=sqlite3.Row
 c.execute('pragma busy_timeout=20000')
 return c

def ensure_output_metadata():
 with db() as c:
  columns={r[1] for r in c.execute('PRAGMA table_info(jobs)').fetchall()}
  if not columns:raise RuntimeError('Voice jobs table missing')
  for name,kind in {'naming_method':"TEXT NOT NULL DEFAULT 'legacy'",'drive_file_id':'TEXT','drive_url':'TEXT','drive_verified_at':'TEXT','drive_files_json':"TEXT NOT NULL DEFAULT '{}'"}.items():
   if name not in columns:c.execute('ALTER TABLE jobs ADD COLUMN '+name+' '+kind)
  c.execute("UPDATE jobs SET delivery_status='retry_pending',next_delivery_retry=0 WHERE status='completed' AND delivery_status='transferring'")

def delivery_loop():
 while True:
  try:
   deliver_once()
   cleanup_delivered()
  except Exception as exc:print('KOKORO_DRIVE_LOOP_RETRY='+type(exc).__name__,flush=True)
  time.sleep(4)

def heartbeat(ready:bool,reason:str=''):
 DATA.mkdir(parents=True,exist_ok=True)
 target=DATA/'heartbeat.json';tmp=DATA/'heartbeat.json.part'
 tmp.write_text(json.dumps({'ready':ready,'reason':reason,'epoch':time.time(),'at':now()}))
 os.replace(tmp,target)

def heartbeat_loop():
 while True:
  try:heartbeat(True)
  except Exception:pass
  time.sleep(8)

def catalog(engine):
 all_voices=engine.get_voices()
 voices=[]
 for voice in all_voices:
  language=LANGS.get(voice[:2]);
  if language:voices.append({'id':voice,'name':voice.replace('_',' ').title(),'language':language})
 voices.sort(key=lambda v:(v['language'],v['id']))
 tmp=ROOT/'voice_catalog.json.part';tmp.write_text(json.dumps(voices,indent=2));os.replace(tmp,ROOT/'voice_catalog.json')
 return voices

def split_sentences(s:str,max_chars:int=280):
 chunks=[]
 for paragraph in s.splitlines():
  words=paragraph.split()
  part=[];size=0
  for word in words:
   extra=len(word)+(1 if part else 0)
   if part and size+extra>max_chars:
    chunks.append(' '.join(part));part=[];size=0
   if len(word)>max_chars:
    if part:chunks.append(' '.join(part));part=[];size=0
    chunks.extend(word[i:i+max_chars] for i in range(0,len(word),max_chars))
   else:part.append(word);size+=extra
  if part:chunks.append(' '.join(part))
 return [c for c in chunks if c.strip()]

def update(job_id,**updates):
 if not updates:return
 fields=', '.join(k+'=?' for k in updates)
 with db() as c:c.execute(f'update jobs set {fields} where job_id=?',(*updates.values(),job_id))

def job_status(jid):
 with db() as c:
  row=c.execute("select status from jobs where job_id=?",(jid,)).fetchone()
 return row['status'] if row else 'cancelled'

def process_job(engine,row):
 jid=row['job_id'];source=Path(row['input_file']);out=source.with_suffix('.wav')
 # A staging WAV is never served or uploaded; os.replace is the publication point.
 staging=out.with_name(out.name+'.part')
 try:
  staging.unlink(missing_ok=True)
  if not source.is_file():raise RuntimeError('TXT input missing')
  text=source.read_text(encoding='utf-8')
  chunks=split_sentences(text)
  if not chunks:raise RuntimeError('No synthesizable text')
  catalog_items=json.loads((ROOT/'voice_catalog.json').read_text())
  if not any(v['id']==row['voice'] and v['language']==row['language'] for v in catalog_items):raise RuntimeError('Voice selection is not in verified catalog')
  with sf.SoundFile(str(staging),mode='w',samplerate=24000,channels=1,subtype='PCM_16',format='WAV') as wav:
   for i,chunk in enumerate(chunks):
    if job_status(jid) in ('cancel_requested','cancelled'):
     update(jid,status='cancelled',finished_at=now(),delivery_status='not_applicable')
     return
    samples,sr=engine.create(chunk,voice=row['voice'],speed=float(row['speed']),lang=row['language'])
    if sr!=24000:raise RuntimeError('Unexpected sample rate')
    mono=np.asarray(samples,dtype='float32').reshape(-1)
    if mono.size==0:raise RuntimeError('Empty audio segment')
    wav.write(mono)
    if i+1<len(chunks):wav.write(np.zeros(24000//6,dtype='float32'))
    update(jid,progress=min(95,int(95*(i+1)/len(chunks))))
  if job_status(jid) in ('cancel_requested','cancelled'):
   update(jid,status='cancelled',finished_at=now(),delivery_status='not_applicable')
   return
  meta=sf.info(str(staging))
  if staging.stat().st_size<2000 or meta.duration<=0:raise RuntimeError('Invalid generated WAV')
  # Only publish a validated complete file; leave any older finalized artifact alone.
  os.replace(staging,out)
  with db() as c:
   result=c.execute("""update jobs set status='completed',finished_at=?,wav_file=?,
     progress=100,delivery_status='pending',error=null
     where job_id=? and status='processing'""",(now(),str(out),jid))
  if result.rowcount!=1:
   # Cancellation can race only after synthesis; preserve the finished file,
   # but never schedule the cancelled job for Drive delivery.
   if job_status(jid) in ('cancel_requested','cancelled'):
    update(jid,status='cancelled',finished_at=now(),delivery_status='not_applicable')
   return
  print('KOKORO_GENERATED='+jid+'|DURATION='+str(round(meta.duration,2)),flush=True)
 except Exception as exc:
  if job_status(jid) in ('cancel_requested','cancelled'):
   update(jid,status='cancelled',finished_at=now(),delivery_status='not_applicable')
  else:
   update(jid,status='failed',finished_at=now(),delivery_status='not_applicable',error=type(exc).__name__+': '+str(exc)[:180])
  print('KOKORO_JOB_FAILED='+jid+'|TYPE='+type(exc).__name__,flush=True)
 finally:
  staging.unlink(missing_ok=True)

def pick():
 with db() as c:
  c.execute("update jobs set status='queued' where status='retry_wait' and retry_at<=?",(time.time(),))
  row=c.execute("select * from jobs where status='queued' and hidden_at is null order by created_at,job_id limit 1").fetchone()
  if not row:return None
  claim=c.execute("""update jobs set status='processing',started_at=?,
   attempt=coalesce(attempt,0)+1,progress=0 where job_id=? and status='queued'""",(now(),row['job_id']))
  return dict(row) if claim.rowcount==1 else None

def run_rclone(*args):
 cmd=[RCLONE,'--config',RCFILE,'--tpslimit','1','--tpslimit-burst','1','--retries','1','--low-level-retries','1',*args]
 r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=160,text=True)
 if r.returncode:
  diagnostic=(r.stderr or '').lower()[-8192:]
  if any(w in diagnostic for w in ('429','rate limit','ratelimit','too many requests','quota exceeded','user rate limit')):
   category='rate_or_quota'
  elif any(w in diagnostic for w in ('401','unauthorized','invalid_grant','invalid token','token expired')):
   category='authentication'
  elif any(w in diagnostic for w in ('403','forbidden','permission denied','access denied','insufficient permissions')):
   category='permission'
  elif any(w in diagnostic for w in ('not found','404','directory not found','path does not exist')):
   category='remote_not_found'
  elif any(w in diagnostic for w in ('timeout','timed out','connection reset','connection refused','network is unreachable','tls handshake')):
   category='network'
  else:
   category='other'
  raise RuntimeError('Drive operation unsuccessful:'+category)
 return r.stdout

def verified_copy(local:Path,remote:str):
 """Verify Drive identity, name, size, and MD5 where available."""
 if not local.is_file() or local.stat().st_size<1:raise RuntimeError('Local output missing')
 run_rclone('copyto','--ignore-existing',str(local),remote)
 data=json.loads(run_rclone('lsjson','--stat','--hash',remote))
 if int(data.get('Size',-1))!=local.stat().st_size:raise RuntimeError('Drive size verification failed')
 if data.get('Name') and data['Name']!=remote.rsplit('/',1)[-1]:raise RuntimeError('Drive filename mismatch')
 fid=str(data.get('ID') or '')
 if not re.fullmatch(r'[A-Za-z0-9_-]{8,}',fid):raise RuntimeError('Drive ID missing')
 md5=(data.get('Hashes') or {}).get('MD5')
 if md5:
  with local.open('rb') as payload:
   actual=hashlib.file_digest(payload,'md5').hexdigest()
  if md5.lower()!=actual:raise RuntimeError('Drive MD5 mismatch')
 return {'id':fid,'url':'https://drive.google.com/file/d/'+fid+'/view','size':local.stat().st_size,'name':remote.rsplit('/',1)[-1]}

def deliver_once():
 if not RCLONE or not Path(RCFILE).is_file():
  with db() as c:
   missing=c.execute("select job_id from jobs where status='completed' and delivery_status in ('pending','retry_pending','blocked') and next_delivery_retry<=? order by created_at limit 1",(time.time(),)).fetchone()
   if missing:c.execute("update jobs set delivery_status='retry_pending',delivery_error='drive_configuration_missing',delivery_retries=delivery_retries+1,next_delivery_retry=? where job_id=?",(time.time()+900,missing['job_id']))
  return
 with db() as c:
  row=c.execute("select * from jobs where status='completed' and delivery_status not in ('delivered','cleaned') and next_delivery_retry<=? order by created_at limit 1",(time.time(),)).fetchone()
 if row is None:return
 jid=row['job_id'];txt=Path(row['input_file']);wav=Path(row['wav_file'] or '')
 if not txt.is_file() or not wav.is_file():
  update(jid,delivery_status='blocked',delivery_error='Local pair missing',next_delivery_retry=time.time()+3600)
  return
 remote='gdrive:Omni Voice/Kokoro/'+row['language']+'/'+row['batch_id']+'/'+jid+'/'+row['output_basename']
 try:
  update(jid,delivery_status='transferring')
  text_receipt=verified_copy(txt,remote+'.txt')
  audio_receipt=verified_copy(wav,remote+'.wav')
  update(jid,delivery_status='delivered',delivery_error=None,drive_wav=remote+'.wav',drive_txt=remote+'.txt',
         drive_file_id=audio_receipt['id'],drive_url=audio_receipt['url'],drive_verified_at=now(),
         drive_files_json=json.dumps({'txt':text_receipt,'wav':audio_receipt},ensure_ascii=False))
  print('KOKORO_DRIVE_PAIR_VERIFIED='+jid,flush=True)
 except Exception as exc:
  retries=int(row['delivery_retries'] or 0)+1
  delay=min(3600,120*2**min(5,retries-1))
  failure=('verification_size_mismatch' if isinstance(exc,RuntimeError) and str(exc)=='Drive size verification failed' else (str(exc).split(':',1)[1] if isinstance(exc,RuntimeError) and str(exc).startswith('Drive operation unsuccessful:') else type(exc).__name__))
  update(jid,delivery_status='retry_pending',delivery_retries=retries,next_delivery_retry=time.time()+delay,delivery_error=failure)
  print('KOKORO_DRIVE_RETRY='+jid+'|CATEGORY='+failure+'|DELAY_SEC='+str(delay),flush=True)

def cleanup_delivered(limit=5):
 """Delete only finished Kokoro job payloads after verified Drive text+audio."""
 with db() as c:
  rows=c.execute("""select * from jobs where status='completed'
    and delivery_status='delivered' and drive_txt!='' and drive_wav!=''
    order by created_at limit ?""",(limit,)).fetchall()
 removed=0
 for row in rows:
  jid=str(row['job_id'])
  if not re.fullmatch(r'KOJ-[A-F0-9]{16}',jid):continue
  folder=DATA/'jobs'/jid
  if folder.is_symlink() or folder.resolve().parent!=(DATA/'jobs').resolve():continue
  files=(Path(str(row['input_file'])),Path(str(row['wav_file'])))
  if any(p.is_symlink() or p.parent.resolve()!=folder.resolve() for p in files):continue
  try:
   if folder.exists():shutil.rmtree(folder)
   update(jid,delivery_status='cleaned')
   removed+=1
  except (OSError,ValueError) as exc:
   print('KOKORO_LOCAL_CLEANUP_RETAINED='+jid+'|'+type(exc).__name__,flush=True)
 return removed

def main():
 if not MODEL.is_file() or not VOICES.is_file():raise SystemExit('Models missing')
 print('KOKORO_WORKER_START='+now(),flush=True)
 engine=Kokoro(str(MODEL),str(VOICES))
 verified=catalog(engine)
 if not verified:raise SystemExit('Voice catalog empty')
 DATA.mkdir(parents=True,exist_ok=True)
 ensure_output_metadata()
 with db() as c:
  existing={row[1] for row in c.execute("pragma table_info(jobs)")}
  for key,kind in (('attempt','integer default 0'),('progress','integer default 0'),('retry_at','real default 0'),('hidden_at','text')):
   if key not in existing:c.execute('alter table jobs add column '+key+' '+kind)
  # Never rerun interrupted audio automatically; a validated owner retry is required.
  c.execute("update jobs set status='failed',finished_at=?,error='Interrupted during worker restart' where status='processing'",(now(),))
  c.execute("update jobs set status='cancelled',delivery_status='not_applicable' where status='cancel_requested'")
 if '--smoke' in sys.argv:
  print('KOKORO_SMOKE_READY|VOICE_COUNT='+str(len(verified)))
  return
 thread=threading.Thread(target=heartbeat_loop,daemon=True);thread.start()
 threading.Thread(target=delivery_loop,daemon=True).start()
 heartbeat(True)
 while True:
  try:
   job=pick()
   if job:process_job(engine,job)
   else:
    time.sleep(4)
  except Exception as exc:
   print('KOKORO_LOOP_ERROR='+type(exc).__name__,flush=True)
   time.sleep(8)

if __name__=='__main__':main()

"""Single Edge TTS online worker, isolated SQLite queue, pair-verified Drive spool."""
from __future__ import annotations
import asyncio, json, os, re, shutil, sqlite3, subprocess, threading, time, hashlib
from datetime import datetime,timezone
from pathlib import Path
import edge_tts

ROOT=Path(os.environ.get('EDGE_STUDIO_ROOT','/home/ubuntu/apps/edge_tts_voice_studio'))
DATA=ROOT/'data';DB=DATA/'edge_jobs.sqlite3';CATALOG=DATA/'voice_catalog.json';HB=DATA/'heartbeat.json'
RCLONE=shutil.which('rclone');RCCONF=Path('/home/ubuntu/.config/masterctl/rclone.conf')

def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def db():
    c=sqlite3.connect(str(DB),timeout=25)
    c.row_factory=sqlite3.Row
    c.execute('pragma busy_timeout=20000')
    return c

def update(jid,**fields):
    if not fields:return
    fields['updated_at']=now()
    with db() as c:
        c.execute('update jobs set '+','.join(k+'=?' for k in fields)+' where job_id=?',(*fields.values(),jid))

def ensure_output_metadata():
    with db() as c:
        cols={r[1] for r in c.execute('PRAGMA table_info(jobs)').fetchall()}
        if not cols:raise RuntimeError('Voice jobs table missing')
        for name,kind in {'naming_method':"TEXT NOT NULL DEFAULT 'legacy'",'drive_file_id':'TEXT','drive_url':'TEXT','drive_verified_at':'TEXT','drive_files_json':"TEXT NOT NULL DEFAULT '{}'"}.items():
            if name not in cols:c.execute('ALTER TABLE jobs ADD COLUMN '+name+' '+kind)
        c.execute("UPDATE jobs SET delivery_status='retry_pending',next_delivery_retry=0 WHERE status='completed' AND delivery_status='transferring'")

def delivery_loop():
    while True:
        try:
            deliver()
            cleanup_delivered()
        except Exception as exc:
            print('EDGE_DRIVE_LOOP_RETRY='+type(exc).__name__,flush=True)
        time.sleep(4)

def heartbeat(ready=False,reason=''):
    DATA.mkdir(parents=True,exist_ok=True)
    tmp=DATA/'heartbeat.json.part'
    tmp.write_text(json.dumps({'epoch':time.time(),'ready':ready,'reason':reason,'at':now()}),encoding='utf8')
    os.replace(tmp,HB)

def catalog_age():
    try:return time.time()-float(json.loads(CATALOG.read_text())['epoch'])
    except (OSError,ValueError,KeyError,TypeError):return float('inf')

def refresh_voices():
    try:
        remote=asyncio.run(asyncio.wait_for(edge_tts.list_voices(),timeout=25))
        voices=[]
        for v in remote:
            name=v.get('ShortName');locale=v.get('Locale')
            if name and locale:
                voices.append({'id':name,'name':v.get('FriendlyName') or name,'locale':locale,'gender':v.get('Gender') or ''})
        if not voices:raise RuntimeError('Empty online voice catalog')
        tmp=DATA/'voice_catalog.json.part'
        tmp.write_text(json.dumps({'epoch':time.time(),'voices':voices},ensure_ascii=False),encoding='utf8')
        os.replace(tmp,CATALOG)
        print('EDGE_VOICE_CATALOG_COUNT='+str(len(voices)),flush=True)
        return True
    except Exception as exc:
        print('EDGE_VOICE_CATALOG_RETRY='+type(exc).__name__,flush=True)
        return catalog_age()<86400

def status(jid):
    with db() as c:r=c.execute('select status from jobs where job_id=?',(jid,)).fetchone()
    return r['status'] if r else 'cancelled'

def parts(text,limit=1400):
    # Never send entire long submissions in a single request; allow pause/cancel between chunks.
    words=re.findall(r'\S+',text)
    if not words:return []
    result=[];buf=''
    for w in words:
        if len(w)>limit:
            if buf:result.append(buf);buf=''
            result.extend(w[i:i+limit] for i in range(0,len(w),limit))
        elif len(buf)+len(w)+1>limit:
            result.append(buf);buf=w
        else:buf+=((' ' if buf else '')+w)
    if buf:result.append(buf)
    return result

async def synthesize(text,voice,rate,pitch,volume,output,events):
    r=f'{rate:+d}%';p=f'{pitch:+d}Hz';v=f'{volume:+d}%'
    communication=edge_tts.Communicate(text,voice,rate=r,pitch=p,volume=v,boundary='SentenceBoundary')
    count=0;boundary=[]
    with output.open('wb') as f:
        async for msg in communication.stream():
            if msg['type']=='audio':f.write(msg['data']);count+=len(msg['data'])
            elif msg['type'] in ('SentenceBoundary','WordBoundary'):
                boundary.append({'offset':msg.get('offset',0),'duration':msg.get('duration',0),'text':msg.get('text','')})
    if count<800:raise RuntimeError('No valid MP3 data received')
    events.write_text(json.dumps(boundary,ensure_ascii=False),encoding='utf8')

def run(cmd,timeout=180):
    result=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('Media conversion failed')
    return result.stdout

def duration(path):
    return float(run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],30).strip())

def timestamp(ms):
    ms=max(0,int(ms));h,ms=divmod(ms,3600000);m,ms=divmod(ms,60000);s,ms=divmod(ms,1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'

def make_outputs(row,chunks):
    base=Path(row['input_file']).parent
    work=base/'parts'; mp3=base/(row['output_basename']+'.mp3')
    wav=base/(row['output_basename']+'.wav');srt=base/(row['output_basename']+'.srt')
    playlist=work/'concat.txt'
    playlist.write_text(''.join("file '"+str((work/f'{i:04d}.mp3').resolve())+"'\n" for i in range(len(chunks))),encoding='utf8')
    run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(playlist),'-c:a','libmp3lame','-b:a','96k',str(mp3)],180)
    run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(mp3),'-ar','24000','-ac','1','-c:a','pcm_s16le',str(wav)],180)
    if mp3.stat().st_size<1000 or wav.stat().st_size<3000:raise RuntimeError('Invalid output file size')
    lines=[];idx=1;elapsed=0
    for i in range(len(chunks)):
        seg=work/f'{i:04d}.mp3';meta=work/f'{i:04d}.json'
        if meta.is_file():
            for x in json.loads(meta.read_text(encoding='utf8')):
                text=' '.join(str(x.get('text','')).split())
                if not text:continue
                start=elapsed+int(x.get('offset',0))/10000
                end=max(start+200,elapsed+(int(x.get('offset',0))+int(x.get('duration',0)))/10000)
                lines.extend([str(idx),timestamp(start)+' --> '+timestamp(end),text,''])
                idx+=1
        elapsed+=duration(seg)*1000
    if lines:srt.write_text('\n'.join(lines)+'\n',encoding='utf8')
    return mp3,wav,srt if lines else None

def mark_waiting(jid,attempt,reason):
    if attempt>=3:
        update(jid,status='failed',error=reason[:140],delivery_status='not_applicable')
    else:
        delay=min(1800,60*(2**max(0,attempt-1)))
        update(jid,status='retry_wait',retry_at=time.time()+delay,error=reason[:140])
    print('EDGE_RETRY_OR_FAIL='+jid+'|ATTEMPT='+str(attempt),flush=True)

def process(row):
    jid=row['job_id'];folder=Path(row['input_file']).parent;work=folder/'parts';work.mkdir(exist_ok=True)
    attempt=int(row['attempt'] or 0)+1
    update(jid,attempt=attempt,error=None)
    try:
        text=Path(row['input_file']).read_text(encoding='utf8')
        chunks=parts(text)
        if not chunks:raise RuntimeError('Empty input')
        total=len(chunks)
        for i,piece in enumerate(chunks):
            st=status(jid)
            if st in ('cancel_requested','cancelled'):
                update(jid,status='cancelled',delivery_status='not_applicable');return
            if st in ('pause_requested','paused'):
                update(jid,status='paused');return
            segment=work/f'{i:04d}.mp3';meta=work/f'{i:04d}.json'
            if segment.exists() and segment.stat().st_size>=800 and meta.exists():
                update(jid,progress=int(85*(i+1)/total));continue
            segment.unlink(missing_ok=True);meta.unlink(missing_ok=True)
            try:
                asyncio.run(asyncio.wait_for(synthesize(piece,row['voice'],row['rate'],row['pitch'],row['volume'],segment,meta),timeout=220))
            except Exception:
                segment.unlink(missing_ok=True);meta.unlink(missing_ok=True)
                raise
            update(jid,progress=int(85*(i+1)/total))
        if status(jid)=='cancel_requested':update(jid,status='cancelled',delivery_status='not_applicable');return
        if status(jid)=='pause_requested':update(jid,status='paused');return
        mp3,wav,srt=make_outputs(row,chunks)
        update(jid,status='completed',progress=100,mp3_file=str(mp3),wav_file=str(wav),srt_file=str(srt) if srt else None,delivery_status='pending')
        print('EDGE_AUDIO_GENERATED='+jid,flush=True)
    except Exception as e:mark_waiting(jid,attempt,type(e).__name__)

def pick():
    with db() as c:
        c.execute("update jobs set status='queued' where status='retry_wait' and retry_at<=?",(time.time(),))
        r=c.execute("select * from jobs where status='queued' order by created_at,job_id limit 1").fetchone()
        if r:c.execute("update jobs set status='processing',updated_at=? where job_id=? and status='queued'",(now(),r['job_id']))
        return dict(r) if r else None

def rclone_cmd(*args):
    if not RCLONE or not RCCONF.is_file():raise RuntimeError('Drive credentials unavailable')
    cmd=[RCLONE,'--config',str(RCCONF),'--tpslimit','1','--tpslimit-burst','1','--retries','1','--low-level-retries','1',*args]
    r=subprocess.run(cmd,capture_output=True,text=True,timeout=160)
    if r.returncode:raise RuntimeError('Drive upload or stat failed')
    return r.stdout

def verified_copy(local,remote):
    """Write no permissions; require verified Drive ID and size."""
    if not local.is_file() or local.stat().st_size<1:raise RuntimeError('Local output missing')
    rclone_cmd('copyto','--ignore-existing',str(local),remote)
    metadata=json.loads(rclone_cmd('lsjson','--stat','--hash',remote))
    if int(metadata.get('Size',-1))!=local.stat().st_size:raise RuntimeError('Drive remote size mismatch')
    if metadata.get('Name') and metadata['Name']!=remote.rsplit('/',1)[-1]:raise RuntimeError('Drive filename mismatch')
    fid=str(metadata.get('ID') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,}',fid):raise RuntimeError('Drive ID missing')
    md5=(metadata.get('Hashes') or {}).get('MD5')
    if md5:
        with local.open('rb') as payload:
            actual=hashlib.file_digest(payload,'md5').hexdigest()
        if actual!=md5.lower():raise RuntimeError('Drive hash mismatch')
    return {'id':fid,'url':'https://drive.google.com/file/d/'+fid+'/view','size':local.stat().st_size,'name':remote.rsplit('/',1)[-1]}

def deliver():
    if not RCLONE or not RCCONF.is_file():
        with db() as c:
            missing=c.execute("select job_id from jobs where status='completed' and delivery_status in ('pending','retry_pending','blocked') and next_delivery_retry<=? order by created_at limit 1",(time.time(),)).fetchone()
            if missing:c.execute("update jobs set delivery_status='retry_pending',delivery_error='drive_configuration_missing',delivery_retries=delivery_retries+1,next_delivery_retry=? where job_id=?",(time.time()+900,missing['job_id']))
        return
    with db() as c:
        r=c.execute("select * from jobs where status='completed' and delivery_status not in ('delivered','cleaned','blocked') and next_delivery_retry<=? order by created_at limit 1",(time.time(),)).fetchone()
    if r is None:return
    row=dict(r);jid=row['job_id']
    required=[('txt','input_file'),('mp3','mp3_file'),('wav','wav_file')]
    optional=[('srt','srt_file')]
    for _,key in required:
        if not row.get(key) or not Path(row[key]).is_file():
            update(jid,delivery_status='blocked',delivery_error='Required local file missing');return
    remote='gdrive:Omni Voice/Edge TTS/'+row['locale']+'/'+row['batch_id']+'/'+jid+'/'+row['output_basename']
    try:
        update(jid,delivery_status='transferring')
        result={};receipts={}
        for ext,key in required+optional:
            if row.get(key) and Path(row[key]).is_file():
                target=remote+'.'+ext
                receipts[ext]=verified_copy(Path(row[key]),target)
                result['drive_'+ext]=target
        update(jid,delivery_status='delivered',delivery_error=None,
               drive_file_id=receipts['mp3']['id'],drive_url=receipts['mp3']['url'],drive_verified_at=now(),
               drive_files_json=json.dumps(receipts,ensure_ascii=False),**result)
        print('EDGE_DRIVE_FILES_VERIFIED='+jid,flush=True)
    except Exception as e:
        retries=int(row['delivery_retries'] or 0)+1
        delay=min(3600,120*2**min(5,retries-1))
        update(jid,delivery_status='retry_pending',delivery_retries=retries,next_delivery_retry=time.time()+delay,delivery_error=type(e).__name__)
        print('EDGE_DRIVE_DELIVERY_RETRY='+jid,flush=True)

def cleanup_delivered(limit=5):
    """Only remove this finished Edge job's private media directory after Drive receipts."""
    with db() as c:
        rows=c.execute("""select * from jobs where status='completed'
            and delivery_status='delivered' and drive_txt!='' and drive_mp3!=''
            and drive_wav!='' order by created_at limit ?""",(limit,)).fetchall()
    removed=0
    for row in rows:
        jid=str(row['job_id'])
        if not re.fullmatch(r'EDJ-[A-F0-9]{16}',jid):continue
        folder=DATA/'jobs'/jid
        if folder.is_symlink():continue
        root=(DATA/'jobs').resolve()
        if folder.resolve().parent!=root:continue
        files=[Path(str(row[key])) for key in ('input_file','mp3_file','wav_file')]
        if row['srt_file']:
            if not row['drive_srt']:continue
            files.append(Path(str(row['srt_file'])))
        if any(p.is_symlink() or p.parent.resolve()!=folder.resolve() for p in files):continue
        try:
            if folder.exists():shutil.rmtree(folder)
            update(jid,delivery_status='cleaned')
            removed+=1
        except (OSError,ValueError) as exc:
            print('EDGE_LOCAL_CLEANUP_RETAINED='+jid+'|'+type(exc).__name__,flush=True)
    return removed

def main():
    DATA.mkdir(parents=True,exist_ok=True)
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise SystemExit('Media converters required')
    print('EDGE_WORKER_START='+now(),flush=True)
    def keepalive():
        while True:
            try:heartbeat(catalog_age()<86400,'voice_catalog_unavailable' if catalog_age()>=86400 else '')
            except Exception:pass
            time.sleep(8)
    threading.Thread(target=keepalive,daemon=True).start()
    ensure_output_metadata()
    with db() as c:c.execute("update jobs set status='queued' where status in ('processing','pause_requested')")
    threading.Thread(target=delivery_loop,daemon=True).start()
    last_attempt=0
    while True:
        try:
            if catalog_age()>43200 and time.time()-last_attempt>600:
                last_attempt=time.time();refresh_voices()
            ready=catalog_age()<86400
            heartbeat(ready,'voice_catalog_unavailable' if not ready else '')
            if ready:
                row=pick()
                if row:process(row)
            time.sleep(4)
        except Exception as e:
            heartbeat(False,type(e).__name__)
            print('EDGE_LOOP_RECOVER='+type(e).__name__,flush=True)
            time.sleep(10)

if __name__=='__main__':main()


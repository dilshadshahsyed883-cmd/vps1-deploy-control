"""Guarded, Scene-only PyAV compatibility repair for faster-whisper 1.2.0.

A cached rollback wheel is required before changing the idle worker venv.
The owner's failed project and all input/checkpoint files remain read-only.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time

from production.scene_production_worker import (ENV, RECORD, STATE, UNIT, URL, VENV,
                                               entries, health, require)
AV_VERSION = '16.1.0'
TERMINAL = frozenset({'failed','cancelled','completed','cleaned'})

# Exercise the same decoder used by WhisperModel.transcribe on actual WAV bytes.
DECODE_PROBE = '''import io,json,wave,math,struct,importlib.metadata
from faster_whisper.audio import decode_audio
import numpy as np
f=io.BytesIO()
with wave.open(f,'wb') as w:
    w.setnchannels(2);w.setsampwidth(2);w.setframerate(22050)
    w.writeframes(b''.join(struct.pack('<hh',int(12000*math.sin(i*.08)),int(8000*math.sin(i*.09))) for i in range(22050)))
f.seek(0)
a=decode_audio(f,sampling_rate=16000)
assert a.dtype == np.float32 and a.ndim == 1 and 15000 <= len(a) <= 17000 and np.isfinite(a).all() and abs(a).max() > .01
print(json.dumps({'av':importlib.metadata.version('av'),'faster_whisper':importlib.metadata.version('faster-whisper'),'decoded_samples':len(a)}))
'''

CAPTION_PROBE = '''import json,os,pathlib,subprocess,sys,tempfile
from faster_whisper import WhisperModel
source=pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory(prefix='whisper-compat-',dir=sys.argv[2]) as temp:
    fragment=pathlib.Path(temp)/'narration.wav'
    subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-i',str(source),'-t','30','-ac','1','-ar','16000',str(fragment)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
    model=WhisperModel('base',device='cpu',compute_type='int8',cpu_threads=2)
    segments,info=model.transcribe(str(fragment),language='en',word_timestamps=True,vad_filter=True,beam_size=5,condition_on_previous_text=False)
    rows=list(segments)
    usable=[s for s in rows if s.text.strip() and s.end > s.start]
    words=sum(len(s.words or []) for s in usable)
    assert usable and words > 0, 'Real narration produced no caption segments'
    print(json.dumps({'caption_segments':len(usable),'word_timings':words,'language':info.language}))
'''


def idle_worker(payload):
    require(payload.get('ok') is True and payload.get('admission_only') is False,
            'configured production worker not healthy')
    require(isinstance(payload.get('queue_counts'),dict), 'worker queue state unknown')
    require(not any(int(count) for status,count in payload['queue_counts'].items() if status not in TERMINAL),
            'active/queued Scene projects block dependency maintenance')


class WhisperCompatibility:
    def __init__(self, journal):
        self.journal = Path(journal)/'whisper-compat'
        self.previous = ''
        self.mutated = False
        self.stopped = False
        self.token = ''

    def _run(self, args, timeout=120):
        from production.runtime import run
        return run(args,timeout=timeout,quiet=True)

    def _ready(self):
        for attempt in range(60):
            result=health(URL,self.token)
            if result.get('ok') is True: return result
            time.sleep(.5)
        raise RuntimeError('Scene worker health failed after PyAV transaction')

    def repair(self):
        require(os.geteuid() == 0, 'protected Scene deploy lane required')
        record=json.loads(RECORD.read_text())
        require(record.get('owner') == 'scene-production-worker-v1' and record.get('unit') == UNIT,
                'production worker ownership mismatch')
        self.token=entries(ENV.read_text())['SCENE_WORKER_TOKEN']
        idle_worker(health(URL,self.token))
        py=str(VENV/'bin/python')
        self.previous=self._run([py,'-c',"import importlib.metadata; print(importlib.metadata.version('av'))"]).strip()
        require(re.fullmatch(r'[0-9]+(?:\.[0-9]+)+(?:[a-zA-Z0-9.+-]*)',self.previous), 'unexpected installed PyAV version')
        self.journal.mkdir(mode=0o700)
        from production.runtime import atomic_json
        atomic_json(self.journal/'previous.json',{'av':self.previous,'unit':UNIT,'venv':str(VENV)},0o600)
        print('SCENE_WHISPER_INSTALLED_AV='+self.previous,flush=True)
        if self.previous != AV_VERSION:
            # Both wheels are local before the idle worker is stopped. Rollback
            # is independent of network availability and cannot change other deps.
            wheels=self.journal/'wheels'; wheels.mkdir(mode=0o700)
            for version in (self.previous,AV_VERSION):
                self._run([py,'-m','pip','download','--disable-pip-version-check','--only-binary=:all:',
                           '--no-deps','--dest',str(wheels),'av=='+version],timeout=180)
            idle_worker(health(URL,self.token))
            self.stopped=True
            self._run(['systemctl','stop',UNIT],timeout=45)
            # Catch a submission racing the read-only idle preflight. Never
            # change dependencies beneath a queued or interrupted user job.
            with sqlite3.connect('file:'+str(STATE/'queue.sqlite3')+'?mode=ro',uri=True) as conn:
                counts=dict(conn.execute('SELECT status,count(*) FROM jobs GROUP BY status').fetchall())
            require(not any(int(n) for status,n in counts.items() if status not in TERMINAL),
                    'new Scene submission detected; dependency change aborted')
            self.mutated=True
            self._run([py,'-m','pip','install','--disable-pip-version-check','--no-index','--find-links',str(wheels),
                       '--no-deps','--force-reinstall','av=='+AV_VERSION],timeout=120)
        decoded=json.loads(self._run([py,'-c',DECODE_PROBE],timeout=45))
        require(decoded.get('av') == AV_VERSION and decoded.get('decoded_samples',0) >= 15000,
                'pinned PyAV real-audio decoder proof failed')
        if self.stopped:
            self._run(['systemctl','start',UNIT],timeout=45); self.stopped=False
        self._ready()
        caption=self.verify_narration()
        print('SCENE_WHISPER_DECODE=PASS AV='+AV_VERSION+' REAL_AUDIO=PASS',flush=True)
        print('SCENE_WHISPER_CAPTIONS='+json.dumps(caption,sort_keys=True),flush=True)
        return {'pyav':AV_VERSION,'real_audio_decode':'PASS',**caption}

    def verify_narration(self):
        # Select only a failed project whose recorded event identifies this
        # exact decoder error. No job is restarted, edited or copied wholesale.
        with sqlite3.connect('file:'+str(STATE/'queue.sqlite3')+'?mode=ro',uri=True) as conn:
            row=conn.execute("SELECT j.id FROM jobs j JOIN job_events e ON e.job_id=j.id "
                             "WHERE j.status='failed' AND e.message LIKE '%metadata_errors%' "
                             "ORDER BY e.id DESC LIMIT 1").fetchone()
        require(row and re.fullmatch('[a-f0-9]{32}',row[0]), 'affected failed narration unavailable for caption proof')
        root=(STATE/'jobs'/row[0]).resolve()
        require(root.is_relative_to((STATE/'jobs').resolve()), 'affected job path escaped Scene state')
        paths=list(root.rglob('narration_internal_audio_engine.mp3'))
        require(len(paths) == 1 and paths[0].resolve().is_relative_to(root), 'processed narration checkpoint unavailable')
        source=paths[0].resolve()
        before=hashlib.sha256(source.read_bytes()).hexdigest()
        result=self._run(['/usr/sbin/runuser','-u','vps1scene','--','/usr/bin/env',
                          'HF_HOME='+str(STATE/'model-cache'),'XDG_CACHE_HOME='+str(STATE/'cache'),
                          str(VENV/'bin/python'),'-c',CAPTION_PROBE,str(source),str(STATE)],timeout=600)
        require(hashlib.sha256(source.read_bytes()).hexdigest() == before, 'source narration changed during read-only smoke')
        return json.loads(result)

    def rollback(self):
        if self.mutated:
            # Preserve any project admitted after worker recovery. A working
            # decoder is retained rather than interrupting a new user's job.
            if not self.stopped:
                idle_worker(health(URL,self.token))
                self._run(['systemctl','stop',UNIT],timeout=45)
                self.stopped=True
            # A failed install can leave this worker stopped already; never
            # issue a second redundant stop before restoring cached wheels.
            py=str(VENV/'bin/python')
            self._run([py,'-m','pip','install','--disable-pip-version-check','--no-index',
                       '--find-links',str(self.journal/'wheels'),'--no-deps','--force-reinstall',
                       'av=='+self.previous],timeout=120)
        if self.stopped:
            self._run(['systemctl','start',UNIT],timeout=45); self.stopped=False
            self._ready()
        print('SCENE_WHISPER_DEPENDENCY_ROLLBACK=PASS PROJECTS=PRESERVED',flush=True)

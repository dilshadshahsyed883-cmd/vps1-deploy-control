"""Optional local OCR and bounded CPU speech recognition; never external uploads."""
from __future__ import annotations
import csv
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
from ai_engine import digest

SPEECH_MODEL='Systran/faster-whisper-tiny'
SPEECH_REVISION='d90ca5fe260221311c53c58e660288d3deb8d356'
SPEECH_FILES = {item["path"]:item["sha256"] for item in [
    {"path":'model.bin', "sha256":'dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1'},
    {"path":'config.json', "sha256":'a73a28cdfe1c43ccc7202fa333d1f89c202477271407ae9a7f19afa52039cac8'},
    {"path":'tokenizer.json', "sha256":'fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab'},
    {"path":'vocabulary.txt', "sha256":'34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913'},
]}

def speech_ready(folder):
    return all((Path(folder)/name).is_file() for name in SPEECH_FILES)

def prepare_speech(folder):
    import fcntl
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with (folder/'.download.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        for name,expected in SPEECH_FILES.items():
            target=folder/name
            if target.is_file() and digest(target)==expected:continue
            tmp=folder/(name+'.partial')
            try:
                with urllib.request.urlopen(f'https://huggingface.co/{SPEECH_MODEL}/resolve/{SPEECH_REVISION}/{name}',timeout=90) as src,tmp.open('wb') as out:
                    count=0
                    while data:=src.read(1024*1024):
                        count+=len(data)
                        if count>90*1024**2:raise RuntimeError('Unexpected speech model size')
                        out.write(data)
                if digest(tmp)!=expected:raise RuntimeError('Speech model integrity failed')
                tmp.replace(target)
            finally:tmp.unlink(missing_ok=True)


def recognize_text(image,timestamp=0):
    tool=shutil.which('tesseract')
    if not tool:return {'state':'not_configured','text':'','items':[]}
    try:
        image=image.copy();image.thumbnail((1800,1800))
        data=io.BytesIO();image.save(data,format='PNG')
        result=subprocess.run([tool,'stdin','stdout','-l','eng','--psm','11','tsv'],input=data.getvalue(),capture_output=True,timeout=15,check=True)
        rows=list(csv.DictReader(io.StringIO(result.stdout.decode('utf-8')),delimiter='\t'))
        words=[r for r in rows if r.get('text','').strip() and float(r.get('conf',-1))>=45]
        items=[{'value':r['text'][:200],'confidence':round(float(r['conf'])/100,4),'timestamp':timestamp,
                'provider':'tesseract','model_version':'system-eng','origin':'generated'} for r in words[:200]]
        return {'state':'complete','text':' '.join(x['value'] for x in items),'items':items,
                'note':'English OCR; small, stylized or other-language text may be missed.'}
    except (OSError,ValueError,subprocess.SubprocessError,UnicodeError):return {'state':'failed','text':'','items':[]}


class SpeechEngine:
    def __init__(self,folder):self.folder=Path(folder);self.model=None
    def transcribe(self,path,duration=0,start=0):
        try:return self._transcribe(path,duration,start)
        except Exception as exc:return {'state':'failed','text':'','segments':[],'error':type(exc).__name__}
    def _transcribe(self,path,duration=0,start=0):
        if not speech_ready(self.folder):return {'state':'not_configured','text':'','segments':[]}
        probe=subprocess.run(['ffprobe','-v','error','-select_streams','a','-show_entries','stream=index','-of','json',str(path)],capture_output=True,text=True,timeout=15)
        if not json.loads(probe.stdout or '{}').get('streams'):return {'state':'no_audio','text':'','segments':[]}
        with tempfile.TemporaryDirectory(prefix='sml-speech-') as temp:
            audio=Path(temp)/'audio.wav'
            subprocess.run(['ffmpeg','-v','error','-ss',str(max(0,start)),'-i',str(path),'-t',str(min(float(duration or 30),30)),'-vn','-ac','1','-ar','16000','-threads','1',str(audio)],capture_output=True,check=True,timeout=35)
            if self.model is None:
                from faster_whisper import WhisperModel
                self.model=WhisperModel(str(self.folder),device='cpu',compute_type='int8',cpu_threads=2,num_workers=1,local_files_only=True)
            import wave
            import numpy as np
            with wave.open(str(audio),'rb') as stream:
                samples=np.frombuffer(stream.readframes(stream.getnframes()),dtype=np.int16).astype(np.float32)/32768.
            segments,info=self.model.transcribe(samples,beam_size=1,vad_filter=False,condition_on_previous_text=False)
            result=[{'value':s.text.strip(),'start':s.start,'end':s.end,'confidence':round(math.exp(s.avg_logprob),4),
                'source_start':s.start+start,'source_end':s.end+start,'provider':SPEECH_MODEL,'model_version':SPEECH_REVISION,'origin':'generated'} for s in segments if s.text.strip()]
            return {'state':'complete','text':' '.join(x['value'] for x in result),'segments':result,
                'language':info.language,'analyzed_seconds':min(float(duration or 30),30),'truncated':duration>30,
                'note':'Tiny multilingual model, first 30 seconds per asset; uncertain transcript requires review.'}

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--prepare',type=Path,required=True);args=p.parse_args();prepare_speech(args.prepare)
    print(json.dumps({'speech_model':SPEECH_MODEL,'ready':speech_ready(args.prepare)}))

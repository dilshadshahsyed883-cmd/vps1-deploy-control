"""Real FFmpeg scene acceptance; all inputs and outputs are temporary/local."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'apps/short-stream/part01'))
from auto_stream_engine import StreamEngine
from auto_stream_compositor import validate,compile_scene,render_worker,read_render,asset

@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.media=self.root/'media';self.media.mkdir()
        self.engine=StreamEngine(self.root/'engine',self.media)
        self.runff(['-f','lavfi','-i','color=red:s=320x180:r=30','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','0.5','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(self.media/'video.mp4')])
        self.runff(['-f','lavfi','-i','color=blue:s=32x32','-frames:v','1','-threads','1',str(self.media/'logo.png')])
        self.runff(['-f','lavfi','-i','sine=frequency=220:sample_rate=44100','-t','0.5',str(self.media/'audio.wav')])
    def runff(self,args):
        result=subprocess.run(['ffmpeg','-v','error','-y',*args],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
    def document(self):
        return {'profile':'480p','duration':1,'active_scene':0,'scenes':[{'name':'Composition','sources':[
            {'type':'video','media_path':'video.mp4','volume':0.3},
            {'type':'logo','media_path':'logo.png','x':0.5,'y':0.1,'width':0.1,'height':0.2},
            {'type':'text','text':"Studio: 100% user's text [safe]",'y':0.6,'font_size':20},
            {'type':'audio','media_path':'audio.wav','volume':0.2}]}]}
    def test_real_preview_and_composition_with_audio_logo_and_text(self):
        doc=self.document()
        for preview,name in [(True,'preview.png'),(False,'output.mp4')]:
            target=self.root/name
            command=compile_scene(self.engine,doc,self.root/'work',target,preview=preview)
            self.assertFalse(any(x.startswith(('http','rtmp')) for x in command))
            result=subprocess.run(command,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertGreater(target.stat().st_size,0)
        probe=subprocess.run(['ffprobe','-v','error','-show_streams','-of','json',str(self.root/'output.mp4')],capture_output=True,text=True,check=True)
        streams=json.loads(probe.stdout)['streams']
        self.assertEqual({s['codec_type'] for s in streams},{'video','audio'})
        video=next(s for s in streams if s['codec_type']=='video')
        self.assertEqual((video['width'],video['height']),(854,480))
    def test_playlist_renders_and_durable_status_survives_read(self):
        doc=self.document();doc['scenes'][0]['sources']=[{'type':'playlist','files':['video.mp4','video.mp4']}]
        rid='render_test'
        with self.engine._connect() as con:con.execute("INSERT INTO scene_renders(id,document,state,created_at,updated_at) VALUES(?,?,'WAITING','now','now')",(rid,json.dumps(doc)))
        render_worker(self.engine,rid,validate(self.engine,doc))
        row=read_render(self.engine,rid)
        self.assertEqual(row['state'],'READY',row)
        self.assertTrue(self.engine._source(row['media_path']).exists())
    def test_source_traversal_and_filter_injection_rejected(self):
        doc=self.document();doc['scenes'][0]['sources'][0]['media_path']='../outside.mp4'
        with self.assertRaises(ValueError):validate(self.engine,doc)
        doc=self.document();doc['scenes'][0]['sources'][0]['x']='0;movie=https://example.invalid'
        with self.assertRaises(ValueError):validate(self.engine,doc)
    def test_hidden_and_muted_layers_compile_silence(self):
        doc=self.document();doc['scenes'][0]['sources']=[{'type':'video','media_path':'video.mp4','muted':True},{'type':'text','text':'hidden','visible':False}]
        command=compile_scene(self.engine,doc,self.root/'muted',self.root/'muted.mp4')
        self.assertIn('anullsrc=',command[command.index('-filter_complex')+1])
        self.assertNotIn('drawtext=',command[command.index('-filter_complex')+1])
        result=subprocess.run(command,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()

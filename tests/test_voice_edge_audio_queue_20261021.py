"""Edge Queue must match Kokoro: audio download and verified Drive open only.

This checks the protected old-template overlay and exercises the actual Edge
browser renderer with recorded-shape (nonproduction) jobs in Node.
"""
import inspect
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from production import voice_template_release as release

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_bytes()


def previous_live_template():
    """Build exact prior UI state from current source without touching APIs."""
    older = SOURCE.replace(release.EDGE_AUDIO_RENDER_NEW,
                           release.EDGE_AUDIO_RENDER_OLD, 1)
    older = older.replace(release.VOICE_UI_MARKUP_PAIRS[0][1],
                          release.EDGE_QUEUE_PREVIOUS, 1)
    return older


class EdgeQueueAudioOnlyTests(unittest.TestCase):
    def test_installer_updates_both_previous_live_markup_and_links(self):
        old = previous_live_template()
        self.assertNotEqual(old, SOURCE)
        new = release.voice_ui_parity_overlay(old, SOURCE)
        new = release.voice_edge_audio_overlay(new, SOURCE)
        self.assertEqual(new, SOURCE)
        release.verify_edge_audio_markup(new)

    def test_overlay_repeat_safe_and_fails_closed(self):
        self.assertEqual(release.voice_edge_audio_overlay(SOURCE, SOURCE), SOURCE)
        with self.assertRaisesRegex(RuntimeError, "VOICE_UI_RELEASE_BLOCKED"):
            release.voice_edge_audio_overlay(
                SOURCE.replace(release.EDGE_AUDIO_RENDER_NEW,
                               b"unknown Renderer", 1), SOURCE)
        with self.assertRaisesRegex(RuntimeError, "VOICE_UI_RELEASE_BLOCKED"):
            release.voice_edge_audio_overlay(
                SOURCE, SOURCE.replace(b"const audioFormats=['mp3','wav'];",
                                       b"const audioFormats=['txt'];", 1))
        with self.assertRaisesRegex(RuntimeError, "VOICE_UI_RELEASE_BLOCKED"):
            release.verify_edge_audio_markup(previous_live_template())

    def test_old_and_new_renderer_differ_only_in_visual_output(self):
        previous = previous_live_template()
        after = release.voice_ui_parity_overlay(previous, SOURCE)
        after = release.voice_edge_audio_overlay(after, SOURCE)
        self.assertEqual(after, SOURCE)
        restored = after.replace(release.EDGE_AUDIO_RENDER_NEW,
                                 release.EDGE_AUDIO_RENDER_OLD, 1)
        restored = restored.replace(release.VOICE_UI_MARKUP_PAIRS[0][1],
                                    release.EDGE_QUEUE_PREVIOUS, 1)
        self.assertEqual(restored, previous)
        for behavior in (b"j.allowed_actions||[]", b"/delivery/retry",
                         b"drive_verified_at", b"drive_file_id",
                         b"j.delivery_error", b"j.queue_status",
                         b"confirm('Hide this finished job"):
            self.assertIn(behavior, after)

    def test_guard_checks_both_live_upstream_and_public_html(self):
        installer = inspect.getsource(release.install)
        guard = inspect.getsource(release.verify_runtime)
        self.assertIn("voice_edge_audio_overlay(patched_html, source_html)", installer)
        self.assertIn("verify_edge_audio_markup(body)", guard)
        self.assertIn("verify_edge_audio_markup(full_html)", guard)
        self.assertIn("rollback", inspect.getsource(release.install))

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for DOM renderer test")
    def test_edge_real_js_renderer_only_audio_and_verified_drive(self):
        text = SOURCE.decode("utf-8")
        marker = '<script id="voiceStudioEdgeEngineV1Script">'
        source = text.split(marker, 1)[1].split("</script>", 1)[0]
        self.assertEqual(source.count("})();"), 1)
        source = source.replace("})();", "window.__testLoadQueue=loadJobs;})();", 1)
        node = r"""
const vm = require('vm');
function element(tag) {
 const obj = {tagName:tag, children:[], _text:'', href:'', target:'', rel:'', disabled:false,
  append(...kids){this.children.push(...kids);},
  replaceChildren(...kids){this.children=[...kids];},
  hasChildNodes(){return this.children.length>0;},
  set textContent(value){this._text=String(value);this.children=[];},
  get textContent(){return this._text;}
 }; return obj;
}
function links(root){
 return (root.tagName==='a'?[{text:root.textContent,href:root.href,target:root.target,rel:root.rel}]:[])
   .concat((root.children||[]).flatMap(links));
}
function buttons(root){
 return (root.tagName==='button'?[root.textContent]:[])
   .concat((root.children||[]).flatMap(buttons));
}
function check(cond, message){if(!cond)throw Error(message);}
async function runCase(jobs){
 const rows=element('tbody'),message=element('div');
 const document={readyState:'loading',addEventListener(){},getElementById(id){
  return id==='vseQueueRows'?rows:message;
 },createElement:element,querySelectorAll(){return [];}};
 const window={};
 const context={document,window,location:{pathname:'/voice-studio/app',
 href:'https://app.scenebuilder.online/voice-studio/app'},
 URL,Date,FormData,Set,console,confirm:()=>true,
 fetch:async()=>({ok:true,json:async()=>({jobs})})};
 vm.runInNewContext(INJECTED_JS,context);
 await window.__testLoadQueue();
 return rows.children.map(tr=>({
  audio:links(tr.children[2]),ctrl:buttons(tr.children[3]),
  audioButtons:buttons(tr.children[2])
 }));
}
(async()=>{
 const uri=id=>'https://drive.google.com/file/d/'+id+'/view';
 const rich={job_id:'job1',status:'completed',queue_status:'completed',
  mp3_url:'/mp3',wav_url:'/wav',txt_url:'/txt',srt_url:'/srt',
  delivery_status:'cleaned',drive_verified_at:'today',drive_file_id:'mp3id',
  drive_files:{mp3:{id:'mp3id',url:uri('mp3id')},wav:{id:'wavid',url:uri('wavid')},
   txt:{id:'txtid',url:uri('txtid')},srt:{id:'srtid',url:uri('srtid')}},
  allowed_actions:['delete']};
 const wav={job_id:'job2',status:'completed',delivery_status:'delivered',
  drive_verified_at:'today',drive_file_id:'wavid',drive_files:{wav:{id:'wavid',url:uri('wavid')}}};
 const pending={job_id:'job3',status:'completed',mp3_url:'/mp3',delivery_status:'retry_pending'};
 const unverified={...rich,job_id:'job4',drive_verified_at:null,allowed_actions:[]};
 const r=await runCase([rich,wav,pending,unverified]);
 check(r.length===4,'Missing test jobs');
 check(r[0].audio.length===2,'Cleaned job should have exactly one audio download and one Drive link');
 check(r[0].audio[0].text==='Download MP3','Prefer MP3 audio link');
 check(r[0].audio[0].href.endsWith('/tts/api/edge/files/job1/mp3'),'Local MP3 URL not preserved');
 check(r[0].audio[1].text==='Google Drive: Open uploaded file','Kokoro-style Drive label missing');
 check(r[0].audio[1].href===uri('mp3id'),'Verified MP3 receipt URL not used');
 check(r[0].ctrl.join(',')==='Delete','Delete control changed');
 check(r[1].audio.length===2&&r[1].audio[0].text==='Download WAV (Drive)',
 'WAV-only verified Drive fallback missing');
 check(r[1].audio[1].href===uri('wavid'),'WAV remote URL wrong');
 check(r[2].audio.length===1&&r[2].audioButtons.includes('Retry Drive upload'),
 'Unverified retry behavior or audio download changed');
 check(r[3].audio.length===1,'Unverified Drive link must be hidden');
 for(const item of r)check(item.audio.every(a=>!/TXT|SRT|\.txt|\.srt/.test(a.text+a.href)),
 'TXT/SRT link leaked into audio-only Queue');
 console.log('EDGE_AUDIO_QUEUE_DOM_PASS verified/local/remote/retry/controls');
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
"""
        node = node.replace("INJECTED_JS", json.dumps(source, ensure_ascii=True), 1)
        result = subprocess.run(["node", "-e", node], cwd=ROOT, text=True,
                                capture_output=True, timeout=25, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EDGE_AUDIO_QUEUE_DOM_PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()

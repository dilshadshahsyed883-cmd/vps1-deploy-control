"""Short Maker: durable multi-submit browser queue without parallel VPS renders."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "apps/short-maker/part01/resumable_client.js"
SERVER = ROOT / "apps/short-maker/part01/app.py"


class MultiSubmissionTests(unittest.TestCase):
    def test_separate_pending_store_and_atomic_activation(self):
        js = CLIENT.read_text(encoding="utf-8")
        html = SERVER.read_text(encoding="utf-8")
        self.assertIn("indexedDB.open(SHORT_UPLOAD_DB,2)", js)
        self.assertIn("['meta','files','pending']", js)
        self.assertIn("db.transaction(['meta','files','pending'],'readwrite')", js)
        self.assertIn("tx.objectStore('pending').delete(next.key)", js)
        self.assertIn("metaStore.put(entry.meta,'active')", js)
        self.assertIn("fileStore.put(f.blob,f.kind+':'+f.index)", js)
        self.assertIn("tx.objectStore('pending').put({meta,blobs},key)", js)
        self.assertIn("while(true){", js)
        self.assertIn("await shortResumeInternal(meta)", js)
        self.assertIn("navigator.locks.request('short-maker-one-upload',run)", js)
        self.assertIn("shortKickUploadDrain()", js)
        self.assertNotIn("if(activeShortUpload)return;\n render.disabled=true", js)
        for marker in ('id="pendingUploadStatus"', 'id="resumeUpload"',
                       'id="discardUpload"', 'id="render"'):
            self.assertIn(marker, html)
        self.assertIn("executor = ThreadPoolExecutor(max_workers=1)", html)
        self.assertIn("executor.submit(render_job, job_id, cfg)", html)

    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_new_submission_while_first_upload_in_progress(self):
        js = CLIENT.read_text(encoding="utf-8")
        fragment = js[js.index("async function shortSubmitOrResume(){"):
                      js.index("form.addEventListener('submit',async event=>{")]
        source = r"""
const assert=require('node:assert/strict'), vm=require('node:vm');
const staged=[]; let kicks=0, audioRefresh=0;
const fileA={name:'first.JPG',size:10,webkitRelativePath:'pictures/first.JPG'};
const fileB={name:'second.png',size:20,webkitRelativePath:'pictures/second.png'};
const audioFile={name:'music.mp3',size:15};
const ctx={
 inputMode:{value:'upload'},
 images:{files:[fileA,fileB],value:'chosen'},
 audio:{value:'chosen'},
 selectedAudioFiles:[audioFile],
 imageCount:{textContent:'2 images selected'},
 statusEl:{textContent:''},
 crypto:{randomUUID:()=> 'fixed-request-id'},
 shortSettings:()=>({input_mode:'upload',output_count:300}),
 shortSupportedImageSelection:files=>({accepted:Array.from(files),skipped:[]}),
 drivePaths:()=>[],
 shortDBPendingCount:async()=>0,
 shortDBRead:async()=>null,
 shortDBStageUpload:async(meta,blobs)=>{staged.push({meta,blobs});},
 shortKickUploadDrain:()=>{kicks++;},
 renderSelectedAudio:()=>{audioRefresh++;},
 activeShortUpload:Promise.resolve('already transferring')
};
vm.createContext(ctx);
vm.runInContext(SOURCE,ctx);
async function check(){
 await vm.runInContext('shortSubmitOrResume()',ctx);
 assert.equal(staged.length,1);
 assert.equal(staged[0].meta.config.output_count,300);
 assert.equal(staged[0].meta.files.images.length,2);
 assert.equal(staged[0].meta.files.audio.length,1);
 assert.equal(staged[0].blobs.length,3);
 assert.strictEqual(staged[0].blobs[0].blob,fileA);
 assert.strictEqual(staged[0].blobs[1].blob,fileB);
 assert.strictEqual(staged[0].blobs[2].blob,audioFile);
 assert.equal(kicks,1);
 assert.equal(ctx.images.value,'');
 assert.equal(ctx.audio.value,'');
 assert.equal(ctx.imageCount.textContent,'0 images selected');
 assert.equal(ctx.selectedAudioFiles.length,0);
 assert.equal(audioRefresh,1);
 assert.equal(ctx.statusEl.textContent,'');
 // A second different project is staged before the first VPS transfer finishes.
 const another={name:'next.webp',size:42,webkitRelativePath:'next/next.webp'};
 ctx.images.files=[another];
 await vm.runInContext('shortSubmitOrResume()',ctx);
 assert.equal(staged.length,2);
 assert.equal(staged[1].meta.files.images.length,1);
 assert.strictEqual(staged[1].blobs[0].blob,another);
 assert.equal(kicks,2);
}
check().catch(err=>{console.error(err);process.exitCode=1;});
""".replace("SOURCE", json.dumps(fragment))
        result = subprocess.run(["node", "-e", source], capture_output=True,
                                text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

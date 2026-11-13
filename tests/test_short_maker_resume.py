"""Offline regression contracts for Short Maker persistent resumable upload."""
import ast
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "apps/short-maker/part01/app.py"
CLIENT = ROOT / "apps/short-maker/part01/resumable_client.js"


class UploadError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def isolated(names, symbols):
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == len(names)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SERVER), "exec"), symbols)
    return symbols


class ShortMakerResumeTests(unittest.TestCase):
    def test_upload_snapshot_resumes_exact_part_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            namespace = isolated({"upload_target", "upload_snapshot"}, {
                "Path": Path, "JOBS_ROOT": root, "HTTPException": UploadError,
                "safe_name": lambda name: Path(name).name
            })
            job = "0123456789abcdef"
            manifest = {"files": {
                "images": [{"name": "../photo.png", "size": 10},
                           {"name": "second.jpg", "size": 12}],
                "audio": [{"name": "track.mp3", "size": 8}]
            }}
            first = namespace["upload_target"](job, "images", 0, manifest["files"]["images"][0])
            second = namespace["upload_target"](job, "images", 1, manifest["files"]["images"][1])
            audio = namespace["upload_target"](job, "audio", 0, manifest["files"]["audio"][0])
            for p in (first, second, audio):
                p.parent.mkdir(parents=True, exist_ok=True)
            first.with_name(first.name + ".part").write_bytes(b"abcd")
            second.write_bytes(b"x" * 12)
            snapshot = namespace["upload_snapshot"](job, manifest)
            self.assertEqual([x["uploaded"] for x in snapshot["files"]["images"]], [4, 12])
            self.assertEqual(snapshot["files"]["audio"][0]["uploaded"], 0)
            self.assertEqual(snapshot["uploaded_bytes"], 16)
            self.assertEqual(snapshot["total_bytes"], 30)
            self.assertEqual(snapshot["upload_progress"], 53.3)
            audio.write_bytes(b"z" * 8)
            first.write_bytes(b"x" * 10)
            self.assertEqual(namespace["upload_snapshot"](job, manifest)["upload_progress"], 100)

    def test_upload_configuration_keeps_destinations_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Default").mkdir()
            names = isolated({"upload_config"}, {
                "Path": Path, "HTTPException": UploadError,
                "SHORT_VPS_DEFAULT": "Default", "DEFAULT_DRIVE_OUTPUT": "gdrive:Short_Maker/Outputs",
                "short_vps_folder_names": lambda: ["Default"],
                "short_vps_folder": lambda n: root / n,
                "normalize_drive_path": lambda p: p,
                "parse_drive_folder_inputs": lambda a, b: [a] if a else [],
                "safe_name": lambda n: Path(n).name
            })
            config = {"available_to_short_uploader": True, "upload_to_drive": False}
            result = names["upload_config"](config)
            self.assertEqual(result["short_vps_folder"], "Default")
            self.assertTrue(result["available_to_short_uploader"])
            self.assertFalse(result["verified_clean"])
            with self.assertRaises(UploadError):
                names["upload_config"]({**config, "upload_to_drive": True})
            with self.assertRaises(UploadError):
                names["upload_config"]({**config, "output_count": 1000})

    def test_browser_resume_and_idempotent_queue_contracts(self):
        app = SERVER.read_text(encoding="utf-8")
        js = CLIENT.read_text(encoding="utf-8")
        self.assertIn('id="progressDetail"', app)
        for marker in ('id="renderProgressTrack"', 'id="renderProgressBar"',
                       'id="uploadPctLabel"', 'id="renderPctLabel"',
                       'client_request_id', 'with upload_create_lock():'):
            self.assertIn(marker, app)
        self.assertIn('previous.get("client_request_id") != request_id', app)
        self.assertIn('id="mUploading"', app)
        self.assertIn('src="/short-maker/resumable_client.js"', app)
        self.assertIn('status="uploading"', app)
        self.assertIn('if state.get("status") != "uploading":', app)
        self.assertIn('with upload_session_lock(job_id):', app)
        self.assertIn('reserve_short_output_names(cfg["short_vps_folder"]', app)
        for fragment in ("indexedDB.open(", "navigator.locks", "SHORT_CHUNK_SIZE",
                         "file.slice(offset", "meta.job_id", "shortDBSave(meta,blobs)",
                         "shortDBRead('meta','active')", "uploaded_bytes", "'/finalize'",
                         "pollShortJob", "crypto.randomUUID()", "withShortUploadLock(",
                         "renderProgressBar.style.width", "client_request_id:meta.client_request_id"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, js)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_mixed_folder_filters_mp3_and_repairs_paused_upload(self):
        js = CLIENT.read_text(encoding="utf-8")
        helpers = js[js.index("const SHORT_IMAGE_EXTENSION="):js.index("function shortSettings(){")]
        recovery = js[js.index("async function shortNormalizePendingImages(meta){"):
                      js.index("async function shortResumeInternal(meta){")]
        script = """
const assert=require('node:assert/strict'),vm=require('node:vm');
let changeHandler, saved;
const data={
 'images:0':{size:3},'images:1':{size:4},'images:2':{size:8},'audio:0':{size:5}
};
const ctx={
 images:{files:[],addEventListener:(event,cb)=>{if(event==='change')changeHandler=cb;}},
 imageCount:{textContent:''},
 crypto:{randomUUID:()=> 'fresh-request-id'},
 shortDBRead:async(store,key)=>{assert.equal(store,'files');return data[key];},
 shortDBSave:async(meta,blobs)=>{saved={meta,blobs};}
};
vm.createContext(ctx);
vm.runInContext(SOURCE,ctx);
async function check(){
 const mixed=[{name:'photos/first.JPG',size:3},
  {name:'1_song.mp3 0600 wali video.mp3',size:4},
  {name:'sub/second.HEIC',size:8}];
 ctx.sample=mixed;ctx.images.files=mixed;changeHandler();
 assert.equal(ctx.imageCount.textContent,'2 image(s) selected · 1 non-image file(s) skipped');
 const selection=vm.runInContext('shortSupportedImageSelection(sample)',ctx);
 assert.equal(selection.accepted.map(f=>f.name).join(','),'photos/first.JPG,sub/second.HEIC');
 assert.equal(selection.skipped.length,1);
 ctx.pending={job_id:null,client_request_id:'old-id',config:{input_mode:'upload'},
  files:{images:mixed.map(f=>({name:f.name,size:f.size})),
   audio:[{name:'song.mp3',size:5}]}};
 const normalized=await vm.runInContext('shortNormalizePendingImages(pending)',ctx);
 assert.equal(normalized.client_request_id,'fresh-request-id');
 assert.equal(normalized.files.images.length,2);
 assert.equal(normalized.files.audio[0].name,'song.mp3');
 assert.equal(saved.blobs.map(b=>b.kind+':'+b.index).join(','),'images:0,images:1,audio:0');
 assert.strictEqual(saved.blobs[0].blob,data['images:0']);
 assert.strictEqual(saved.blobs[1].blob,data['images:2']);
 assert.strictEqual(saved.blobs[2].blob,data['audio:0']);
 ctx.pending={...ctx.pending,files:{images:[{name:'track.mp3',size:4}],audio:[]}};
 await assert.rejects(()=>vm.runInContext('shortNormalizePendingImages(pending)',ctx),
  /no supported images/i);
 ctx.pending={...ctx.pending,job_id:'existing-session'};
 const untouched=await vm.runInContext('shortNormalizePendingImages(pending)',ctx);
 assert.strictEqual(untouched,ctx.pending);
}
check().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("SOURCE", json.dumps(helpers+"\n"+recovery))
        done = subprocess.run(["node", "-e", script], text=True,
                              capture_output=True, timeout=15, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)



if __name__ == "__main__":
    unittest.main()

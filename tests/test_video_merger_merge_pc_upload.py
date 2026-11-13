"""Video Merger PC inputs are direct owner-owned queue sources, not Library entries."""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/"apps/video-merger/part01/app.py"
HTML=SOURCE.parent/"templates/index.html"


class MergePcInputs(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        with patch.dict(os.environ,VIDEO_MERGER_ROOT=str(root),
                        VIDEO_MERGER_PROXY_CONFIG_DIR=str(root/"config")):
            spec=importlib.util.spec_from_file_location("vm_merge_pc",SOURCE)
            self.app=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.app.init_db()
        p=patch.object(self.app.shutil,"disk_usage",
                      return_value=SimpleNamespace(free=32*1024**3))
        p.start()
        self.addCleanup(p.stop)
        self.app.resolve_vps_folder=lambda owner,name: name
        self.app.resolve_owner_folder=lambda owner,name: name
        now=self.app.utcnow()
        with self.app.db() as c:
            c.execute("""INSERT INTO library_items
                (id,name,filename,status,owner_username,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""",
                ("owner-library","First","first.mp4","ready","alice",now,now))

    def source(self, owner="alice", data=b"pc-direct-video", name="Second.MOV"):
        app=self.app
        uid=app.start_library_pc_upload(owner,name,len(data),purpose="merge")
        self.assertEqual(app.append_library_pc_chunk(owner,uid,0,len(data),io.BytesIO(data)),len(data))
        return uid

    def queue(self, uid, owner="alice", **extra):
        payload={"videos":[{"kind":"library","library_id":"owner-library"},
                           {"kind":"pc","upload_id":uid,"trim_enabled":True,
                            "start_min":0,"start_sec":1,"end_min":0,"end_sec":3}],
                 "filename_mode":"manual","filename":"merged.mp4",
                 "upload_to_drive":False,**extra}
        return self.app.add_job(payload,owner=owner)

    def test_merge_input_is_not_added_to_library_and_retains_trim_spec(self):
        uid=self.source()
        job,_=self.queue(uid)
        item=self.app.get_job(job)
        self.assertEqual(item["status"],"waiting")
        self.assertEqual(self.app.list_library_items("alice")[0]["id"],"owner-library")
        specs=self.app.json.loads(item["videos_json"])
        self.assertEqual(specs[1]["kind"],"pc")
        self.assertEqual(specs[1]["filename"],"Second.MOV")
        self.assertEqual(specs[1]["end_sec"],3)
        self.assertEqual(self.app.source_display_filename(specs[1]),"Second.mp4")
        session,_,part=self.app._merge_pc_session("alice",uid)
        self.assertEqual(session["assigned_job"],job)
        self.assertEqual(part.read_bytes(),b"pc-direct-video")
        dst=self.app.WORK/"copied.bin"
        with patch.object(self.app,"validate_downloaded_file"):
            self.app._pc_merge_source_to_file("alice",uid,job,dst,2)
        self.assertEqual(dst.read_bytes(),b"pc-direct-video")

    def test_owner_isolation_and_source_claim_prevent_reuse(self):
        uid=self.source(owner="bob")
        with self.assertRaisesRegex(ValueError,"another account"):
            self.queue(uid,owner="alice")
        self.app.cancel_library_pc_upload("bob",uid)
        own=self.source()
        job,_=self.queue(own)
        with self.assertRaisesRegex(ValueError,"already assigned"):
            self.queue(own)
        with self.assertRaisesRegex(ValueError,"already"):
            self.app.cancel_library_pc_upload("alice",own)
        with self.assertRaisesRegex(ValueError,"another account"):
            self.app._pc_merge_source_to_file("bob",own,job,self.app.WORK/"bad.bin",2)
        self.assertFalse((self.app.WORK/"bad.bin").exists())

    def test_request_key_is_idempotent_with_same_pc_source(self):
        uid=self.source()
        key="pcmerge-request-12345678"
        job,_=self.queue(uid,request_key=key)
        retried,_=self.queue(uid,request_key=key)
        self.assertEqual(job,retried)
        with self.app.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM jobs").fetchone()[0],1)

    def test_cancel_and_delete_clean_only_unreferenced_sources(self):
        uid=self.source()
        job,_=self.queue(uid)
        self.app.cancel_job(job,owner="alice")
        self.assertFalse((self.app.PC_UPLOAD_DIR/(uid+".part")).exists())
        self.app.delete_job(job,owner="alice")
        self.assertIsNone(self.app.get_job(job))

    def test_failed_job_retains_bytes_for_one_safe_retry(self):
        uid=self.source()
        job,_=self.queue(uid)
        self.app.update_job(job,"failed",100,"Failed")
        again,_=self.app.retry_job(job,owner="alice")
        self.assertNotEqual(again,job)
        self.assertEqual(self.app.get_job(again)["retry_of"],job)
        with patch.object(self.app,"validate_downloaded_file"):
            out=self.app.WORK/"retry.bin"
            self.app._pc_merge_source_to_file("alice",uid,again,out,2)
            self.assertEqual(out.read_bytes(),b"pc-direct-video")
        self.app.delete_job(job,owner="alice")
        self.assertTrue((self.app.PC_UPLOAD_DIR/(uid+".part")).is_file())
        with patch.object(self.app,"validate_downloaded_file"):
            moved=self.app.WORK/"retry_after_old_deleted.bin"
            self.app._pc_merge_source_to_file("alice",uid,again,moved,2)
            self.assertEqual(moved.read_bytes(),b"pc-direct-video")
        self.app.update_job(again,"complete",100,"Done")
        self.app._cleanup_merge_pc_uploads("alice",self.app.json.loads(
            self.app.get_job(again)["videos_json"]))
        self.assertFalse((self.app.PC_UPLOAD_DIR/(uid+".part")).exists())

    def test_uncommitted_library_upload_cannot_be_used_as_merge_source(self):
        uid=self.app.start_library_pc_upload("alice","Source.mp4",4)
        self.app.append_library_pc_chunk("alice",uid,0,4,io.BytesIO(b"1234"))
        with self.assertRaisesRegex(ValueError,"not a merge"):
            self.queue(uid)
        self.assertEqual(self.app.list_library_items("alice")[0]["id"],"owner-library")

    def test_merge_pc_row_renders_in_each_dynamic_url_slot(self):
        page=HTML.read_text(encoding="utf-8")
        for needed in ("video-pc-file","vm-pc-merge-choice","Upload from PC",
                       "function selectMergePcFile(", "async function uploadMergePcRow(",
                       "/api/merge/pc/start","/api/merge/pc/chunk",
                       "await uploadMergePcRow(item)", "source_display_filename"):
            if needed=="source_display_filename":
                self.assertIn(needed,SOURCE.read_text(encoding="utf-8"))
            else:
                self.assertIn(needed,page)
        self.assertIn("function toggleTrim(",page)
        self.assertIn('placeholder="Video ${n} URL"',page)


if __name__=="__main__":
    unittest.main()

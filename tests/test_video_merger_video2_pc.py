"""Video Merger Video 2 PC uploads reuse the verified owner-scoped Library pipeline."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/"apps/video-merger/part01/app.py"
HTML=APP.parent/"templates/index.html"

class MergePcVideoTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root=Path(temp.name)
        with patch.dict(os.environ,VIDEO_MERGER_ROOT=str(root),
                        VIDEO_MERGER_PROXY_CONFIG_DIR=str(root/"config")):
            spec=importlib.util.spec_from_file_location("vm_merge_pc",APP)
            self.app=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.app.init_db()
        now=self.app.utcnow()
        with self.app.db() as c:
            for uid,name in (("libfirst","Fixed Intro"),("libsecond","clip.mov [PC-12345678]"),
                             ("other","Other user file")):
                owner="bob" if uid=="other" else "alice"
                c.execute("""INSERT INTO library_items
                    (id,name,filename,status,owner_username,source_url,trim_json,
                     created_at,updated_at,size_bytes)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (uid,name,uid+".mp4","ready",owner,"PC upload: "+uid,
                     "{}",now,now,100))
                self.app.library_cache_path(uid).write_bytes(b"mock-verified-video")

    def video_payload(self, second="libsecond", **overrides):
        return {"videos":[{"kind":"library","library_id":"libfirst"},
                          {"kind":"library","library_id":second,
                           "trim_enabled":True,"start_min":0,"start_sec":1,
                           "end_min":0,"end_sec":5,**overrides}],
                "aspect":"9:16","quality":"720p","fit_mode":"crop",
                "filename_mode":"auto","upload_to_drive":False}

    def test_pc_video2_is_queued_with_trim_and_auto_name(self):
        app=self.app
        jid,filename=app.add_job(self.video_payload(),owner="alice")
        self.assertEqual(filename,"Queued-"+jid+".mp4")
        with app.db() as c:
            job=dict(c.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone())
        import json
        videos=json.loads(job["videos_json"])
        self.assertEqual([v["kind"] for v in videos],["library","library"])
        self.assertFalse(videos[0]["trim_enabled"])
        self.assertTrue(videos[1]["trim_enabled"])
        self.assertEqual(app.trim_window(videos[1],9,2),(1,5,4))
        self.assertEqual(app.source_display_filename(videos[1]),"clip.mp4")
        self.assertEqual(app.normalize_video_spec(videos[1],2,owner="alice")["library_id"],"libsecond")

    def test_owner_cannot_queue_another_users_pc_video(self):
        with self.assertRaisesRegex(ValueError,"belongs to another user"):
            self.app.add_job(self.video_payload(second="other"),owner="alice")
        with self.assertRaisesRegex(ValueError,"belongs to another user"):
            self.app.add_job(self.video_payload(second="libsecond"),owner="bob")

    def test_video1_remains_fixed_but_video2_trim_is_validated(self):
        app=self.app
        spec1=app.normalize_video_spec({"kind":"library","library_id":"libfirst",
                                       "trim_enabled":True,"start_sec":8},1,owner="alice")
        self.assertFalse(spec1["trim_enabled"])
        spec2=app.normalize_video_spec({"kind":"library","library_id":"libsecond",
                                       "trim_enabled":True,"start_sec":4},2,owner="alice")
        self.assertTrue(spec2["trim_enabled"])
        self.assertEqual(spec2["start_sec"],4)
        with self.assertRaisesRegex(ValueError,"seconds must be 0"):
            app.normalize_video_spec({"kind":"library","library_id":"libsecond",
                                      "trim_enabled":True,"start_sec":71},2,owner="alice")

    def test_existing_url_video2_still_supported(self):
        spec=self.app.normalize_video_spec({"kind":"url","url":"https://example.org/a.mp4"},2,
                                           owner="alice")
        self.assertEqual(spec["kind"],"url")
        self.assertEqual(self.app.source_display_filename(spec),"a.mp4")

    def test_pc_video2_loads_from_vps_library_without_network(self):
        app=self.app
        with patch.object(app,"source_to_file",side_effect=AssertionError("must not fetch PC URL")), \
             patch.object(app,"validate_downloaded_file"):
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/"input.bin"
                app.library_to_file("libsecond",path,2)
                self.assertEqual(path.read_bytes(),b"mock-verified-video")

    def test_picker_upload_progress_and_queue_gate_are_wired(self):
        html=HTML.read_text(encoding="utf-8")
        for item in ('Upload from PC','vm-merge-pc-file','function selectMergePcFile(',
                     'function clearMergePcFile(', 'function mergePcReady(',
                     'kind:\'library\',library_id:item.dataset.pcLibraryId',
                     '/api/library/pc/start','/api/library/pc/chunk',
                     '/api/library/pc/finish', 'every(mergePcReady)'):
            self.assertIn(item,html)
        self.assertIn('Video ${n} URL',html)
        self.assertIn("source_display_filename(videos[1])",APP.read_text(encoding="utf-8"))

if __name__=="__main__":
    unittest.main()

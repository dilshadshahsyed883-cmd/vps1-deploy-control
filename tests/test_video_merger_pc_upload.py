"""Owner-scoped PC upload regressions for Video Merger Library."""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "apps/video-merger/part01/app.py"
PAGE = SOURCE.parent / "templates/index.html"


class PcLibraryUploadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        with patch.dict(os.environ, VIDEO_MERGER_ROOT=str(root),
                        VIDEO_MERGER_PROXY_CONFIG_DIR=str(root / "config")):
            spec = importlib.util.spec_from_file_location("vm_pc_upload", SOURCE)
            self.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.app.init_db()
        disk = patch.object(self.app.shutil, "disk_usage",
                            return_value=SimpleNamespace(free=30 * 1024**3))
        disk.start()
        self.addCleanup(disk.stop)

    def upload(self, owner="alice", name="intro.mp4", data=b"fake-video-source"):
        app = self.app
        upload_id = app.start_library_pc_upload(owner, name, len(data))
        split = len(data) // 2
        self.assertEqual(app.append_library_pc_chunk(owner, upload_id, 0, split,
                                                    io.BytesIO(data[:split])), split)
        self.assertEqual(app.append_library_pc_chunk(owner, upload_id, split, len(data) - split,
                                                    io.BytesIO(data[split:])), len(data))
        return upload_id

    def finish(self, upload_id, owner="alice", name="Intro", **extra):
        with patch.object(self.app.threading, "Thread") as worker:
            library_id = self.app.finish_library_pc_upload(owner, upload_id,
                         {"name": name, "upload_to_drive": False, **extra})
        self.assertTrue(worker.called)
        return library_id, worker.call_args.kwargs["args"]

    def test_pc_upload_end_to_end_preserves_vps_first_library_and_trim(self):
        app = self.app
        upload_id = self.upload(data=b"source-media")
        library_id, args = self.finish(upload_id, trim_enabled=True,
                                      start_min=0, start_sec=1,
                                      end_min=0, end_sec=3)
        self.assertEqual(args[0], library_id)
        self.assertEqual(args[1], "PC upload: intro.mp4")
        self.assertFalse(args[4])  # Google Drive optional and unchecked
        self.assertTrue(args[5].is_file())
        self.assertEqual(args[5].read_bytes(), b"source-media")
        item = app.get_library_item(library_id)
        self.assertEqual(item["owner_username"], "alice")
        self.assertEqual(item["drive_requested"], 0)
        self.assertEqual(item["status"], "importing")
        self.assertTrue(item["source_url"].startswith("PC upload:"))
        self.assertTrue(item["trim_json"])
        with patch.object(app, "source_to_file", side_effect=AssertionError("URL fetched for PC file")), \
             patch.object(app, "probe", return_value={
                 "format": {"duration": "5.0"}, "streams": [{"codec_type": "video"}]}), \
             patch.object(app, "transcode_library",
                          side_effect=lambda src, dst, *trim: dst.write_bytes(b"final-mp4")), \
             patch.object(app, "verify_library_media", return_value=2.0):
            app.process_library_import(*args)
        ready = app.get_library_item(library_id)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["drive_status"], "not_requested")
        self.assertEqual(app.library_cache_path(library_id).read_bytes(), b"final-mp4")
        self.assertFalse(args[5].exists())

    def test_other_user_cannot_append_finish_or_cancel(self):
        uid = self.upload(owner="alice")
        with self.assertRaisesRegex(ValueError, "another account"):
            self.app.append_library_pc_chunk("bob", uid, 0, 1, io.BytesIO(b"x"))
        with self.assertRaisesRegex(ValueError, "another account"):
            self.app.finish_library_pc_upload("bob", uid, {"name": "stolen"})
        with self.assertRaisesRegex(ValueError, "another account"):
            self.app.cancel_library_pc_upload("bob", uid)
        self.app.cancel_library_pc_upload("alice", uid)
        with self.assertRaisesRegex(ValueError, "not found"):
            self.app.cancel_library_pc_upload("alice", uid)

    def test_upload_offsets_sizes_and_extensions_are_verified(self):
        app = self.app
        with self.assertRaisesRegex(ValueError, "supported video"):
            app.start_library_pc_upload("alice", "invalid.exe", 10)
        with self.assertRaisesRegex(ValueError, "between"):
            app.start_library_pc_upload("alice", "video.mp4", app.MAX_SOURCE_BYTES + 1)
        uid = app.start_library_pc_upload("alice", "..\\video.MOV", 5)
        with self.assertRaisesRegex(ValueError, "offset"):
            app.append_library_pc_chunk("alice", uid, 1, 2, io.BytesIO(b"ab"))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            app.append_library_pc_chunk("alice", uid, 0, 6, io.BytesIO(b"abcdef"))
        self.assertEqual(app.append_library_pc_chunk("alice", uid, 0, 3,
                                                    io.BytesIO(b"abc")), 3)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            app.finish_library_pc_upload("alice", uid, {"name": "Incomplete"})
        self.assertEqual(app.append_library_pc_chunk("alice", uid, 3, 2,
                                                    io.BytesIO(b"de")), 5)
        self.assertEqual(self.finish(uid)[1][1], "PC upload: video.MOV")

    def test_invalid_library_request_discards_staging_without_creating_item(self):
        uid = self.upload()
        with self.assertRaisesRegex(ValueError, "Library name is required"):
            self.app.finish_library_pc_upload("alice", uid, {"name": ""})
        self.assertFalse((self.app.WORK / ("pc_library_" + uid + ".bin")).exists())
        self.assertFalse((self.app.PC_UPLOAD_DIR / (uid + ".part")).exists())
        self.assertEqual(self.app.list_library_items("alice"), [])

    def test_failed_pc_import_can_be_replaced_by_same_owner_only(self):
        uid = self.upload()
        old, _ = self.finish(uid)
        self.app.update_library_item(old, status="failed", error="bad video")
        uid2 = self.upload(name="replacement.mp4", data=b"new-video")
        new, args = self.finish(uid2, replace_id=old)
        self.assertEqual(new, old)
        self.assertFalse(args[3])  # no previously verified video to preserve
        self.assertEqual(args[1], "PC upload: replacement.mp4")
        with self.assertRaisesRegex(ValueError, "Select and upload"):
            self.app.update_library_item(old, status="failed")
            self.app.retry_library_item(old)

    def test_file_picker_and_progress_wired_without_removing_url_import(self):
        html = PAGE.read_text(encoding="utf-8")
        for expected in ('id="libPcFile" type="file"', 'Upload from PC',
                         'libPcProgressText', 'function selectLibraryPcFile()',
                         'function clearLibraryPcFile()', 'function saveLibraryPc(',
                         '/api/library/pc/start', '/api/library/pc/chunk',
                         '/api/library/pc/finish', 'if(libPcFile.files.length)'):
            self.assertIn(expected, html)
        self.assertIn('YouTube / Google Drive / direct video URL', html)
        self.assertIn("upload_to_drive:libraryUploadDrive.checked", html)


if __name__ == "__main__":
    unittest.main()

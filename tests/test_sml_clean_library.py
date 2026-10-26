"""SML's owner-driven total library cleanup must never run implicitly."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"
sys.path.insert(0, str(ROOT))

@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI required")
class CleanLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sml-clean-")
        self.data = Path(self.temp.name) / "only-sml-data"
        self.data.mkdir()
        self.neighbor = Path(self.temp.name) / "other-app"
        self.neighbor.mkdir()
        (self.neighbor / "KEEP.jpg").write_bytes(b"OTHER APP")
        self.previous = {key: os.environ.get(key) for key in ("SML_DATA_ROOT", "SML_AI_WORKER")}
        os.environ.update(SML_DATA_ROOT=str(self.data), SML_AI_WORKER="0")
        sys.modules.pop("app", None)
        import app
        from fastapi.testclient import TestClient
        self.app = app
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        sys.modules.pop("app", None)
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def add_media(self):
        ts = "2026-10-26T00:00:00Z"
        with self.app.db() as conn:
            conn.execute(
                "INSERT INTO sources(id,filename,stored_name,size_bytes,status,created_at,updated_at) "
                "VALUES('SRC','source.mp4','source.mp4',6,'completed',?,?)", (ts, ts))
            conn.execute(
                "INSERT INTO clips(id,source_id,filename,thumb_filename,created_at,updated_at,deleted_at) "
                "VALUES('VID','SRC','video.mp4','video.jpg',?,?,NULL)", (ts, ts))
            conn.execute(
                "INSERT INTO clips(id,source_id,filename,thumb_filename,created_at,updated_at,deleted_at) "
                "VALUES('TRASH','SRC','trash.mp4','trash.jpg',?,?,'2026-10-26')", (ts, ts))
            conn.execute(
                "INSERT INTO image_assets(id,original_name,stored_name,size_bytes,sha256,created_at,updated_at) "
                "VALUES('IMG','image.png','image.png',6,'checksum',?,?)", (ts, ts))
            conn.execute(
                "INSERT INTO media_reservations(id,project_id,scene_id,media_type,media_id,source_id,state,created_at,expires_at) "
                "VALUES('COMMIT','scene-builder','scene-1','video','VID','SRC','committed',1,2)")
        for folder, name in (("sources","source.mp4"), ("clips","video.mp4"),
                             ("clips","trash.mp4"), ("thumbs","video.jpg"),
                             ("images","image.png"), ("temp","scratch.bin")):
            target = self.data / folder / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b"media!")
        (self.data / "ai_models").mkdir()
        (self.data / "ai_models" / "KEEP.bin").write_bytes(b"AI MODEL")
        (self.data / "proxy_profiles.json").write_text('{"keep":"settings"}')

    def preview(self):
        response = self.client.get("/api/library/clean/preview")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def execute(self, preview, **changes):
        body = dict(confirmation="CLEAN SML LIBRARY",
                    preview_token=preview["preview_token"],
                    acknowledge_editor_references=True)
        body.update(changes)
        return self.client.post("/api/library/clean", json=body,
                                headers={"X-SML-Clean-Intent": "confirmed"})

    def test_no_auto_cleanup_and_requires_exact_confirm(self):
        self.add_media()
        result = self.preview()
        self.assertEqual(result["videos"], 1)
        self.assertEqual(result["trash_videos"], 1)
        self.assertEqual(result["images"], 1)
        self.assertEqual(result["committed_editor_references"], 1)
        self.assertTrue((self.data / "clips/video.mp4").exists())
        self.assertEqual(self.client.post("/api/library/clean",
                         json={"confirmation":"CLEAN SML LIBRARY","preview_token":result["preview_token"],
                               "acknowledge_editor_references":True}).status_code, 403)
        self.assertEqual(self.execute(result, confirmation="DELETE").status_code, 400)
        self.assertEqual(self.execute(result, preview_token="bad").status_code, 409)
        self.assertEqual(self.execute(result, acknowledge_editor_references=False).status_code, 400)
        self.assertTrue((self.data / "clips/video.mp4").exists())

    def test_active_jobs_and_reservations_block(self):
        self.add_media()
        with self.app.db() as conn:
            conn.execute("UPDATE sources SET status='processing' WHERE id='SRC'")
        result = self.preview()
        self.assertFalse(result["ready"])
        self.assertEqual(self.execute(result).status_code, 409)
        with self.app.db() as conn:
            conn.execute("UPDATE sources SET status='completed' WHERE id='SRC'")
            conn.execute("INSERT INTO media_reservations(id,project_id,scene_id,media_type,media_id,source_id,state,created_at,expires_at) "
                         "VALUES('LIVE','scene-builder','scene-2','video','VID','SRC','reserved',?,?)",
                         (int(time.time()), int(time.time()) + 3600))
        result = self.preview()
        self.assertFalse(result["ready"])
        self.assertEqual(self.execute(result).status_code, 409)
        self.assertTrue((self.data / "clips/video.mp4").exists())

    def test_complete_reset_preserves_application_and_other_apps(self):
        self.add_media()
        with self.app.db() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES('owner_keep','yes')")
        result = self.preview()
        self.assertTrue(result["ready"], result["blockers"])
        response = self.execute(result)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        after = self.preview()
        self.assertEqual(after["videos"], 0)
        self.assertEqual(after["images"], 0)
        self.assertEqual(after["trash_videos"], 0)
        self.assertEqual(after["sources"], 0)
        self.assertEqual(after["files"], 0)
        with self.app.db() as conn:
            for table in ("clips","image_assets","sources","media_reservations",
                          "ai_jobs","clip_keywords","people","local_reviews"):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT value FROM settings WHERE key='owner_keep'").fetchone()[0], "yes")
            self.assertEqual(conn.execute("SELECT value FROM settings WHERE key='library_cleaning'").fetchone()[0], "false")
        self.assertTrue((self.data / "ai_models/KEEP.bin").exists())
        self.assertTrue((self.data / "proxy_profiles.json").exists())
        self.assertEqual((self.neighbor / "KEEP.jpg").read_bytes(), b"OTHER APP")
        self.assertEqual(self.execute(after).status_code, 200)  # idempotent

    def test_frontend_has_main_button_and_no_one_click_deletion(self):
        html = (ROOT / "static/index.html").read_text()
        js = (ROOT / "static/app.js").read_text()
        self.assertIn('id="openCleanLibrary"', html)
        self.assertIn("CLEAN SML LIBRARY", html)
        self.assertIn("acknowledge_editor_references", js)
        self.assertIn("x-sml-clean-intent", (ROOT / "library_clean.py").read_text())

if __name__ == "__main__":
    unittest.main()

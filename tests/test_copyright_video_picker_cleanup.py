"""Copyright Studio folder selection, queue reservation and verified-only cleanup."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps/copyright-uploader/part01"


class CopyrightPickerCleanup(unittest.TestCase):
    def test_source_panel_and_studio_tabs(self):
        base = (APP / "templates/base.html").read_text()
        form = (APP / "templates/app.html").read_text()
        js = (APP / "static/copyright_video_picker.js").read_text()
        self.assertIn("lum-copyright-workspace", base)
        self.assertIn("nav.vps1-native-upload-tabs", base)
        self.assertIn("copyright_video_picker.js", base)
        self.assertIn("id=\"copyright-folder-file\"", form)
        self.assertIn("id=\"copyright-folder-refresh\"", form)
        self.assertIn("id=\"copyright-folder-summary\"", form)
        self.assertIn("video" + "s are hidden", js)
        self.assertIn('mode.addEventListener("change"', js)
        self.assertIn("section.style.setProperty", js)
        self.assertIn('cache: "no-store"', js)
        self.assertNotIn("Auto Engine</a>", form.split("{% if engine_instance == 'copyright' %}", 1)[1].split("{% elif engine_instance == 'song' %}", 1)[0])

    def test_worker_requires_verified_yt_and_prevents_duplicate_upload(self):
        code = (APP / "lum_engine/worker.py").read_text()
        self.assertIn('if project.get("remote_verified_at") and project.get("youtube_video_id")', code)
        self.assertIn("self._finish_verified(project_id, project, staging)", code)
        self.assertIn('if not project.get("remote_verified_at") or not project.get("youtube_video_id")', code)
        self.assertIn("delete_verified_source(", code)
        self.assertIn("cleanup_project_staging(project_id)", code)

    def test_sqlite_queue_dropdown_claim_and_cleanup(self):
        program = r"""
import hashlib, json, os
from pathlib import Path
from lum_engine.project_store import ProjectStore
from lum_engine.config import STAGING_ROOT
from lum_engine import copyright_handoff as handoff
owner = "user-one"
storage = handoff.owner_root(owner)
storage.mkdir(parents=True, exist_ok=True)
file = storage / "sample-001__merge.mp4"
file.write_bytes(b"verified video fixture bytes")
fingerprint = hashlib.sha256(file.read_bytes()).hexdigest()
store = ProjectStore()
assert handoff.available_videos(owner, store.claimed_folder_files(owner)) == [file.name]
stage = STAGING_ROOT / "copy.mp4"
stage.write_bytes(file.read_bytes())
data = dict(owner_username=owner,ui_mode="heavy",source_mode="local_file",source_origin="folder",
 staging_path=str(stage),source_file_name=file.name,source_file_size=stage.stat().st_size,
 source_fingerprint=fingerprint,title="My Video",channel_db_id=1,privacy_status="private")
pid = store.create_project(data)
assert store.get(pid)["source_origin"] == "folder"
assert file.name in store.claimed_folder_files(owner)
assert handoff.available_videos(owner,store.claimed_folder_files(owner)) == []
try:
 store.create_project({**data,"channel_db_id":2})
 raise AssertionError("same folder video admitted into second channel")
except ValueError: pass
assert file.exists() and stage.exists(), "admission must never delete video"
# A wrong digest must be refused, preserving the real source.
try:
 handoff.delete_verified_source(owner,file.name,"0"*64)
 raise AssertionError("wrong digest accepted")
except RuntimeError: pass
assert file.exists()
assert handoff.delete_verified_source(owner,file.name,fingerprint) is True
assert not file.exists()
assert handoff.delete_verified_source(owner,file.name,fingerprint) is False
assert stage.exists(), "handoff source deletion must not touch staged file"
# Cancellation releases a claim so a safely restaged source becomes selectable.
file.write_bytes(b"restaged source")
store.update(pid,status="CANCELLED")
assert handoff.available_videos(owner,store.claimed_folder_files(owner)) == [file.name]
assert handoff.available_videos("other-user",set()) == []
other = storage / "another.mp4"
other.write_bytes(b"data")
assert handoff.available_videos(owner,set()) != []
print(json.dumps({"owner":True,"queued_hidden":True,"duplicate_block":True,
 "verified_cleanup":True,"cancel_releases":True}))
"""
        # Runs against disposable SQLite and isolated owner folder without YouTube access.
        with tempfile.TemporaryDirectory() as location:
            env = {**os.environ, "LUM_DATA_DIR": location,
                   "YOUTUBE_UPLOAD_MANAGER_DATA_DIR": str(Path(location) / "core"),
                   "COPYRIGHT_MEDIA_ROOT": str(Path(location) / "handoff"),
                   "LUM_ENGINE_INSTANCE": "copyright",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable,"-c",program],env=env,cwd=str(APP),
                                    capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertTrue(json.loads(result.stdout)["queued_hidden"])


if __name__ == "__main__":
    unittest.main()

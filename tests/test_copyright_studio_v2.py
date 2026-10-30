"""Offline Copyright Studio contracts; no uploads, external access or production state."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COPY = ROOT / "apps/copyright-uploader/part01"
MERGER = ROOT / "apps/video-merger/part01"
SCENE = ROOT / "apps/scene-builder/part01"


class CopyrightStudioContract(unittest.TestCase):
    def test_python_parses_without_import_side_effects(self):
        for path in (
            COPY / "lum_engine/standalone_app.py",
            COPY / "lum_engine/project_store.py",
            COPY / "lum_engine/worker.py",
            MERGER / "app.py",
            ROOT / "production/copyright_studio_release.py",
            ROOT / "production/runtime.py",
        ):
            with self.subTest(path=path):
                ast.parse(path.read_text(), filename=str(path))

    def test_jinja_manual_tabs_and_forms(self):
        from jinja2 import Environment
        template = (COPY / "templates/app.html").read_text()
        Environment().parse(template)
        self.assertIn("engine_instance == 'copyright'", template)
        for tab in ("dashboard", "channels", "sheets", "profiles", "add_video", "queue", "logs"):
            self.assertIn("tab=='" + tab + "'", template)
        for source in ("drive_link", "pc_upload", "folder"):
            self.assertIn('value="' + source + '"', template)
        self.assertIn('name="csrf_token"', template)
        self.assertIn('name="profile_id"', template)
        self.assertIn('name="video_file"', template)
        self.assertIn('name="folder_file"', template)
        self.assertIn('name="thumbnail"', template)
        # Copyright branch never offers Auto Engine navigation.
        copyright_nav = template.split("{% if engine_instance == 'copyright' %}",1)[1].split("{% elif engine_instance == 'song' %}",1)[0]
        self.assertNotIn("Auto Engine", copyright_nav)

    def test_automation_guard_and_separate_storage(self):
        app = (COPY / "lum_engine/standalone_app.py").read_text()
        self.assertIn('path.startswith(route("/actions/auto-v6/"))', app)
        self.assertIn('path.startswith(route("/api/auto-v6"))', app)
        self.assertIn('status_code=404', app)
        self.assertIn("copyright_stage_owner_file", app)
        self.assertIn("copyright_stage_file", app)
        config = (COPY / "templates/base.html").read_text()
        self.assertIn('data-workspace="copyright-uploader"', config)
        state = (COPY / "static/lum_ux_state_v2.js").read_text()
        self.assertIn("lum.copyright.main.tab", state)
        self.assertIn("lum.copyright.route", state)
        runtime = (ROOT / "production/runtime.py").read_text()
        for token in ("'copyright-uploader': dict(port=8020", "lum-copyright",
                      "LUM_ENGINE_INSTANCE='copyright'", "COPYRIGHT_MEDIA_ROOT="):
            self.assertIn(token, runtime)

    def test_merger_output_flags_are_independent_and_persistent(self):
        html = (MERGER / "templates/index.html").read_text()
        server = (MERGER / "app.py").read_text()
        self.assertIn('id="uploadDrive"', html)
        self.assertIn('id="addCopyrightFolder"', html)
        self.assertIn("upload_to_drive:uploadDrive.checked", html)
        self.assertIn("add_to_copyright_folder:addCopyrightFolder.checked", html)
        self.assertIn("retained = retain_verified_output", server)
        self.assertIn("if int(j.get('drive_enabled',1)):", server)
        self.assertIn("if int(j.get('copyright_enabled',0)):", server)
        self.assertIn("deliver_copyright_folder(retained", server)
        self.assertIn("COPYRIGHT_MEDIA_ROOT / owner_media_key(owner)", server)
        self.assertIn("api/jobs/([A-Za-z0-9]+)/download", server)
        self.assertIn("copyfile", server)
        self.assertNotIn("auto_queue_copyright", server)

    def test_navigation_moved_and_legacy_url_retained(self):
        scene = (SCENE / "static/vps1_native_site_shell.js").read_text()
        video = (MERGER / "templates/index.html").read_text()
        self.assertIn('["Copyright Studio", "/video-merger/", "copyright"]', scene)
        self.assertIn('tabs.setAttribute("aria-label", "Copyright Studio")', scene)
        self.assertIn('aria-label="Copyright Studio"', video)
        self.assertIn('href="/video-merger/"', video)
        self.assertNotIn('aria-label="Video Studio"', video)
        self.assertIn("PREFIX = '/video-merger'", (MERGER / "app.py").read_text())

    def test_sqlite_durable_queue_owner_and_duplicate_guard(self):
        code = """
import json
from pathlib import Path
from lum_engine.project_store import ProjectStore
from lum_engine.config import STAGING_ROOT
store = ProjectStore()
owner = "reviewer"
one = store.add_copyright_profile(owner,"Alpha","Group 1","manual")
assert store.copyright_profile(owner,one)["profile_name"] == "Alpha"
assert store.copyright_profile("other",one) is None
assert len(store.copyright_profiles(owner)) == 1
media = STAGING_ROOT / "verified.mp4"
media.write_bytes(b"test-media")
data = dict(owner_username=owner,ui_mode="heavy",source_mode="local_file",
 staging_path=str(media),drive_url="",source_fingerprint="file-sha",
 title="Test Upload",channel_db_id=2,privacy_status="private")
jid = store.create_project(data)
assert store.get(jid)["status"] == "QUEUED"
assert store.get(jid)["staging_path"] == str(media)
try:
 store.create_project(data)
 raise AssertionError("duplicate admitted")
except ValueError as err:
 assert "already queued" in str(err)
assert store.retry(jid) is False
store.update(jid,status="FAILED")
assert store.retry(jid) is True
assert store.get(jid)["status"] == "QUEUED"
print(json.dumps({"queue":"PASS", "owner":"PASS", "duplicate":"PASS", "retry":"PASS"}))
"""
        with tempfile.TemporaryDirectory() as temporary:
            env = {**os.environ,
                   "LUM_DATA_DIR": temporary,
                   "YOUTUBE_UPLOAD_MANAGER_DATA_DIR": str(Path(temporary)/"core"),
                   "LUM_ENGINE_INSTANCE": "copyright",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, "-c", code],
                                    cwd=str(COPY), env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["queue"], "PASS")


if __name__ == "__main__":
    unittest.main()

"""Copyright-only mode defaults and manual uploader invariants (no live access)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COPY = ROOT / "apps/copyright-uploader/part01"


class CopyrightModeCleanup(unittest.TestCase):
    def test_legacy_off_flags_upgraded_without_starting_uploader(self):
        script = """
from lum_engine.project_store import ProjectStore
store = ProjectStore()
for key in ('sheets_sync_enabled', 'auto_engine_enabled'):
    assert store.setting(key) == '1', key
    with store.connect() as db:
        assert db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()[0] == '1'
    store.set_setting(key,'0')
    assert store.setting(key) == '1'
    assert store.settings()[key] == '1'
    with store.connect() as db:
        assert db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()[0] == '1'
store.set_setting('engine_enabled','0')
assert store.setting('engine_enabled') == '0'
with store.connect() as db:
    db.execute("UPDATE settings SET value='0' WHERE key IN ('sheets_sync_enabled','auto_engine_enabled')")
    db.commit()
store = ProjectStore()
assert store.setting('sheets_sync_enabled') == '1'
assert store.setting('auto_engine_enabled') == '1'
assert store.setting('engine_enabled') == '0'
print('COPYRIGHT_FIXED_MODE_SETTINGS=PASS')
"""
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ,
                   "LUM_DATA_DIR": temp,
                   "YOUTUBE_UPLOAD_MANAGER_DATA_DIR": str(Path(temp) / "core"),
                   "LUM_ENGINE_INSTANCE": "copyright",
                   "PYTHONDONTWRITEBYTECODE": "1"}
            proc = subprocess.run([sys.executable, "-c", script], cwd=COPY,
                                  env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("COPYRIGHT_FIXED_MODE_SETTINGS=PASS", proc.stdout)

    def test_no_ui_mode_settings_or_manual_channel_forms(self):
        template = (COPY / "templates/app.html").read_text()
        self.assertNotIn("Your YouTube channels &amp; groups", template)
        self.assertNotIn("Add my channel", template)
        self.assertNotIn("Create my group", template)
        self.assertNotIn("Future modes", template)
        self.assertNotIn("Save modes", template)
        self.assertNotIn('/actions/studio-tenant/channel-create', template)
        self.assertNotIn('/actions/studio-tenant/group-create', template)
        self.assertIn('/actions/sheets/sync', template)
        self.assertIn('copyright_dashboard.html', template)

    def test_copyright_manual_only_backend_no_scheduler(self):
        server = (COPY / "lum_engine/standalone_app.py").read_text()
        self.assertIn('if os.getenv("LUM_ENGINE_INSTANCE") not in {"song","shortstudio","copyright"}:', server)
        self.assertIn('raise HTTPException(404, "Copyright uploader mode settings are fixed")', server)
        self.assertIn('Automatic uploader engine is disabled for Copyright Studio', server)
        self.assertIn('store.setting("sheets_sync_enabled", "0")', server)
        # Existing ownership-checked API contracts stay compatible; only the form is removed.
        self.assertIn('route("/actions/studio-tenant/channel-create"),', server)
        # Existing ownership-checked API contracts stay compatible; only the form is removed.
        self.assertIn('route("/actions/studio-tenant/group-create"),', server)
        workers = (ROOT / "production/workers.py").read_text()
        self.assertNotIn("lum-copyright-scheduler", workers)
        self.assertIn('LUM_ENGINE_INSTANCE', (ROOT / "production/runtime.py").read_text())
        self.assertIn("AUTO_SCRIPT=OFFLINE", (ROOT / ".github/workflows/vps1-copyright-studio-release.yml").read_text())


if __name__ == "__main__":
    unittest.main()

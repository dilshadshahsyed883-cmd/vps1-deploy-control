"""Copyright-only queue cancellation, claim race and verified cleanup UI contracts.

All data and media in the behavioral test are disposable; no YouTube calls.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps/copyright-uploader/part01"


class CopyrightQueueClaimCancel(unittest.TestCase):
    def test_atomic_claim_safe_cancel_source_preserved(self):
        program = r"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from lum_engine.project_store import ProjectStore
from lum_engine.config import STAGING_ROOT

store = ProjectStore()
def add(name, when=""):
    f = STAGING_ROOT / (name + ".mp4")
    f.write_bytes((name + "-original").encode())
    return store.create_project(dict(owner_username="alice",source_mode="local_file",
        source_origin="pc_upload",staging_path=str(f),source_file_name=f.name,
        source_fingerprint=name,title=name,channel_db_id=1,app_upload_at=when)),f

future=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
scheduled,sm = add("future",future)
first,original = add("waiting")
assert store.get(scheduled)["status"] == "SCHEDULED"
assert not store.cancel_waiting(first,"bob"), "other user must not cancel"
assert store.cancel_waiting(first,"alice")
assert store.get(first)["status"] == "CANCELLED"
assert original.read_bytes() == b"waiting-original"
assert not store.cancel_waiting(first,"alice"), "cancel must not replay"
due,media = add("due")
claimed = store.claim_next_eligible()
assert claimed and int(claimed["id"]) == due
assert store.get(due)["status"] == "SOURCE_READY"
assert not store.cancel_waiting(due,"alice"), "claimed worker media must be safe"
assert not store.claim_next_eligible(), "future jobs must remain scheduled"
assert store.get(scheduled)["status"] == "SCHEDULED"
assert media.read_bytes() == b"due-original"
store.update(due,status="COMPLETED",remote_verified_at="2026-10-31",youtube_video_id="remote-id")
assert not store.cancel_waiting(due,"alice"), "completed job must be immutable"
print("CANCEL_CLAIM_PASS")
"""
        with tempfile.TemporaryDirectory() as location:
            env = {**os.environ, "LUM_DATA_DIR": location,
                   "YOUTUBE_UPLOAD_MANAGER_DATA_DIR": str(Path(location) / "core"),
                   "LUM_ENGINE_INSTANCE": "copyright", "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run(
                [sys.executable, "-c", program], cwd=str(APP), env=env,
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CANCEL_CLAIM_PASS", result.stdout)

    def test_copyright_actions_and_queue_cleanup_status(self):
        from production.copyright_uploader_browser import fixture
        html = fixture("queue")
        self.assertIn("/actions/cancel/1", html)
        self.assertNotIn("/actions/cancel/2", html)
        self.assertNotIn("/actions/cancel/3", html)
        self.assertNotIn("/actions/cancel/4", html)
        self.assertIn("Original media will be kept", html)
        self.assertIn("Cleanup not confirmed", html)
        completed = fixture("queue", projects=[
            dict(id=4,title="Verified",channel_db_id=7,status="COMPLETED",
                 progress=100,cleanup_verified_at="2026-10-31T00:00:00Z")])
        self.assertIn("VPS cleanup recorded", completed)
        api = (APP / "lum_engine/standalone_app.py").read_text()
        worker = (APP / "lum_engine/worker.py").read_text()
        self.assertIn('route("/actions/cancel/{project_id}")', api)
        self.assertIn("require_csrf(request, csrf_token)", api)
        self.assertIn("store.cancel_waiting(project_id", api)
        self.assertIn("self.store.claim_next_eligible()", worker)
        self.assertIn("verify_youtube_video(self.auth, self.db, core_video_id)", worker)
        self.assertIn('if not project.get("remote_verified_at") or not project.get("youtube_video_id")', worker)


if __name__ == "__main__":
    unittest.main()

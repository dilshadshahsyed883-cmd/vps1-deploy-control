"""Offline regression tests for owner-scoped active Video Merger stop."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "apps/video-merger/part01/app.py"
HTML = SOURCE.parent / "templates/index.html"


class VideoMergerStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        with patch.dict(os.environ, {
            "VIDEO_MERGER_ROOT": str(root),
            "VIDEO_MERGER_PROXY_CONFIG_DIR": str(root / "proxy"),
        }):
            spec = importlib.util.spec_from_file_location("vm_stop_test", SOURCE)
            self.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.app.init_db()
        self.now = self.app.utcnow()

    def job(self, owner="alice", status="processing"):
        jid = "job" + str(time.monotonic_ns())
        with self.app.db() as c:
            c.execute(
                """INSERT INTO jobs(id,name,videos_json,aspect,quality,fit_mode,folder,filename,
                     status,order_idx,created_at,updated_at,owner_username)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (jid, "test", "[]", "9:16", "1080p", "crop", "", "test.mp4",
                 status, 1, self.now, self.now, owner))
        return jid

    def test_owner_only_and_repeated_stop(self):
        jid = self.job()
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.app.stop_job(jid, owner="bob", admin=False)
        self.assertEqual(self.app.get_job(jid)["status"], "processing")
        self.app.stop_job(jid, owner="alice")
        self.app.stop_job(jid, owner="alice")
        self.assertEqual(self.app.get_job(jid)["status"], "stopping")
        self.assertIn(jid, self.app.JOB_STOP_REQUESTS)

    def test_waiting_and_finished_cannot_be_stopped(self):
        for status in ("waiting", "complete", "failed", "cancelled"):
            jid = self.job(status=status)
            with self.assertRaisesRegex(ValueError, "Only an active"):
                self.app.stop_job(jid, owner="alice")

    def test_stop_terminates_running_subprocess(self):
        jid = self.job()
        started = threading.Event()
        result = []

        def work():
            self.app.JOB_THREAD.job_id = jid
            try:
                started.set()
                self.app.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=35)
                result.append("unexpected completion")
            except self.app.JobStopped:
                result.append("stopped")
            finally:
                self.app.JOB_THREAD.job_id = None

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(started.wait(2))
        time.sleep(0.3)
        self.app.stop_job(jid, owner="alice")
        thread.join(timeout=7)
        self.assertFalse(thread.is_alive(), "stop must interrupt the running subprocess")
        self.assertEqual(result, ["stopped"])

    def test_queue_stop_button_and_endpoint_are_wired(self):
        page = HTML.read_text(encoding="utf-8")
        self.assertIn("async function stopJob(id)", page)
        self.assertIn("'/api/jobs/'+id+'/stop'", page)
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("stop_job(m.group(1),self.principal(),admin=False)", source)


if __name__ == "__main__":
    unittest.main()

"""Owner isolation, safe queue controls, and approved Drive folders for Short Maker."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from PIL import Image
except ImportError:
    TestClient = None
    Image = None

SOURCE = Path(__file__).resolve().parents[1] / "apps/short-maker/part01/app.py"
USER = {"x-auth-user": "haseeb1"}
ADMIN = {"x-auth-user": "admin", "x-is-admin": "1"}
FOREIGN = {"x-auth-user": "other"}


@unittest.skipUnless(TestClient is not None and Image is not None, "requires FastAPI and Pillow")
class ShortMakerHandoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        with patch.dict(os.environ, {
            "SHORT_MAKER_DATA": str(root / "data"),
            "SHORT_STUDIO_VPS_FOLDER_ROOT": str(root / "folders"),
        }):
            spec = importlib.util.spec_from_file_location(
                "short_maker_handover_" + str(id(self)), SOURCE
            )
            self.maker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.maker)
        self.enqueue = patch.object(self.maker.executor, "submit", return_value=None)
        self.mock_submit = self.enqueue.start()
        self.client = TestClient(self.maker.app)
        self.client.__enter__()
        self.own_folder = self.client.get("/api/vps-folders", headers=USER).json()["folders"][0]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.enqueue.stop()
        self.maker.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def job(self, job_id, owner, status="queued"):
        job_dir = self.maker.JOBS_ROOT / job_id
        image = job_dir / "input" / "images" / "00001_a.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (6, 6), "red").save(image)
        folder = self.own_folder if owner == "haseeb1" else self.maker.short_personal_default(owner)
        cfg = {
            "video_duration": 16, "image_duration": 2, "output_count": 1,
            "order": "random", "fit": "fill_crop", "motion": "none",
            "fps": 30, "crf": 20, "base_name": "sample.mp4",
            "input_mode": "upload", "short_vps_folder": folder,
            "available_to_short_uploader": True, "upload_to_drive": False,
            "output_names": [folder + " 001_abcdef.mp4"],
        }
        self.maker.set_state(job_id, owner=owner, status=status, created_at=self.maker.utc_now(),
                             progress=0, outputs=[], config=cfg, phase="queued", control="")
        return image

    def test_nonadmin_and_admin_operate_only_on_their_own_jobs(self):
        own_id, foreign_id = "1" * 16, "2" * 16
        self.job(own_id, "haseeb1")
        self.job(foreign_id, "other")
        seen = self.client.get("/api/jobs", headers=USER).json()["jobs"]
        self.assertEqual([r["job_id"] for r in seen], [own_id])
        self.assertEqual(self.client.get("/api/jobs/" + own_id, headers=ADMIN).status_code, 404)
        self.assertEqual(self.client.post("/api/jobs/" + foreign_id + "/pause", headers=USER).status_code, 404)
        self.assertEqual(self.client.post("/api/jobs/" + own_id + "/pause", headers=USER).status_code, 200)
        self.assertEqual(self.maker.get_state(own_id)["status"], "pausing")
        blocked = self.client.post("/api/jobs/bulk/cancel", headers=USER,
                                   json={"job_ids": [own_id, foreign_id]})
        self.assertEqual(blocked.status_code, 404)
        self.assertEqual(self.maker.get_state(own_id)["control"], "pause")

    def test_pause_recovery_resume_cancel_and_retry(self):
        job_id = "3" * 16
        source = self.job(job_id, "haseeb1")
        self.assertEqual(self.client.post("/api/jobs/" + job_id + "/pause", headers=USER).status_code, 200)
        self.maker.render_job(job_id, {**self.maker.get_state(job_id)["config"],
                                       "images": [str(source)], "audio": []})
        self.assertEqual(self.maker.get_state(job_id)["status"], "paused")
        self.assertEqual(self.client.delete("/api/jobs/" + job_id, headers=USER).status_code, 409)
        self.assertEqual(self.client.post("/api/jobs/" + job_id + "/resume", headers=USER).status_code, 200)
        self.assertEqual(self.maker.get_state(job_id)["status"], "queued")
        self.assertEqual(self.mock_submit.call_count, 1)
        self.assertEqual(self.client.post("/api/jobs/" + job_id + "/cancel", headers=USER).status_code, 200)
        self.maker.render_job(job_id, {**self.maker.get_state(job_id)["config"],
                                       "images": [str(source)], "audio": []})
        self.assertEqual(self.maker.get_state(job_id)["status"], "cancelled")
        self.assertTrue(source.exists())
        self.assertEqual(self.client.post("/api/jobs/" + job_id + "/retry", headers=USER).status_code, 200)
        self.assertEqual(self.maker.get_state(job_id)["status"], "queued")
        self.assertEqual(self.mock_submit.call_count, 2)

    def test_safe_folder_delete_and_foreign_protection(self):
        new = self.client.post("/api/vps-folders", headers=USER, json={"name": "Personal Work"})
        self.assertEqual(new.status_code, 200, new.text)
        self.assertEqual(self.client.delete("/api/vps-folders/Personal Work", headers=ADMIN).status_code, 403)
        self.assertEqual(self.client.delete("/api/vps-folders/Personal Work", headers=FOREIGN).status_code, 403)
        folder = self.maker.short_vps_folder("Personal Work")
        (folder / "existing.mp4").write_bytes(b"do not delete")
        self.assertEqual(self.client.delete("/api/vps-folders/Personal Work", headers=USER).status_code, 409)
        self.assertTrue((folder / "existing.mp4").exists())
        (folder / "existing.mp4").unlink()
        self.assertEqual(self.client.delete("/api/vps-folders/Personal Work", headers=USER).status_code, 200)
        self.assertFalse(folder.exists())
        legacy = self.maker.short_vps_folder("Legacy")
        legacy.mkdir()
        self.assertEqual(self.client.delete("/api/vps-folders/Legacy", headers=USER).status_code, 403)

    def test_drive_approved_root_and_empty_only_delete(self):
        own_root = self.maker.short_drive_root("haseeb1")
        other_root = self.maker.short_drive_root("other")
        self.assertNotEqual(own_root, other_root)
        self.assertEqual(self.maker.short_owned_drive_path("haseeb1", own_root + "/Input"),
                         own_root + "/Input")
        with self.assertRaises(Exception):
            self.maker.short_owned_drive_path("haseeb1", other_root + "/Input")
        with self.assertRaises(Exception):
            self.maker.short_drive_inputs_owned("haseeb1",
                                                ["https://drive.google.com/drive/folders/abcdef123456"])
        with patch.object(self.maker, "rclone_run", return_value="Input/\nOutputs/\nExtra/") as remote:
            listing = self.client.get("/api/drive-folders", headers=USER)
            self.assertEqual(listing.status_code, 200, listing.text)
            self.assertTrue(all(v.startswith(own_root + "/") for v in listing.json()["folders"]))
            self.assertEqual(self.client.delete("/api/drive-folders/Input", headers=USER).status_code, 403)
            self.assertEqual(self.client.delete("/api/drive-folders/Extra", headers=USER).status_code, 200)
            self.assertIn("rmdir", [call.args[0][0] for call in remote.call_args_list])

    def test_cancel_kills_only_current_render_process_group(self):
        job_id = "4" * 16
        self.job(job_id, "haseeb1", status="running")
        self.maker.set_state(job_id, phase="render")
        result = []
        started = threading.Event()

        def run():
            self.maker._worker_context.job_id = job_id
            started.set()
            try:
                self.maker.worker_process([sys.executable, "-c", "import time; time.sleep(12)"])
            except self.maker.JobInterrupted:
                result.append("cancelled")
            finally:
                self.maker._worker_context.job_id = None

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(timeout=2))
        time.sleep(0.35)
        answer = self.client.post("/api/jobs/" + job_id + "/cancel", headers=USER)
        self.assertEqual(answer.status_code, 200, answer.text)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "render child process was not interrupted")
        self.assertEqual(result, ["cancelled"])

    def test_retry_never_republishes_consumed_short_uploader_output(self):
        job_id = "6" * 16
        self.job(job_id, "haseeb1", status="failed")
        # The Uploader may have already deleted the verified source MP4.
        self.maker.set_state(job_id, published_outputs=["already-consumed.mp4"])
        self.assertEqual(self.client.post("/api/jobs/" + job_id + "/retry", headers=USER).status_code, 409)
        self.assertEqual(self.mock_submit.call_count, 0)

    def test_crash_during_delivery_requires_manual_review(self):
        job_id = "7" * 16
        self.job(job_id, "haseeb1", status="running")
        self.maker.set_state(job_id, phase="delivery")
        self.maker.recover_pending_jobs()
        state = self.maker.get_state(job_id)
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["delivery_review_required"])
        self.assertEqual(self.client.post("/api/jobs/" + job_id + "/retry", headers=USER).status_code, 409)

    def test_retry_rejects_already_published_media(self):
        job_id = "5" * 16
        self.job(job_id, "haseeb1", status="failed")
        cfg = self.maker.get_state(job_id)["config"]
        video = self.maker.short_vps_folder(self.own_folder) / cfg["output_names"][0]
        video.write_bytes(b"published original")
        self.assertEqual(self.client.post("/api/jobs/" + job_id + "/retry", headers=USER).status_code, 409)
        self.assertEqual(video.read_bytes(), b"published original")
        self.assertEqual(self.mock_submit.call_count, 0)


if __name__ == "__main__":
    unittest.main()

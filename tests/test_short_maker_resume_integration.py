"""HTTP integration test: interrupted Short Maker PC upload resumes and finalizes once."""
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image


SOURCE = Path(__file__).resolve().parents[1] / "apps/short-maker/part01/app.py"


class ShortMakerHTTPResumeTests(unittest.TestCase):
    def test_refresh_and_duplicate_finalize_enqueue_once(self):
        with tempfile.TemporaryDirectory() as td:
            env = {
                "SHORT_MAKER_DATA": str(Path(td) / "state"),
                "SHORT_STUDIO_VPS_FOLDER_ROOT": str(Path(td) / "shorts"),
            }
            with patch.dict(os.environ, env):
                spec = importlib.util.spec_from_file_location("short_maker_resume_api_test", SOURCE)
                maker = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(maker)
            try:
                with patch.object(maker.executor, "submit", return_value=None) as enqueue:
                    with TestClient(maker.app, headers={"x-auth-user": "uploader-test"}) as client:
                        bio = io.BytesIO()
                        Image.new("RGB", (8, 8), "blue").save(bio, format="PNG")
                        picture = bio.getvalue()
                        payload = {
                            "config": {"video_duration": "16", "image_duration": "2.5",
                                       "output_count": "2", "fps": "30", "crf": "20",
                                       "short_vps_folder": "Default", "input_mode": "upload",
                                       "available_to_short_uploader": True, "upload_to_drive": False},
                            "files": {"images": [{"name": "test.png", "size": len(picture)}], "audio": []}
                        }
                        begun = client.post("/api/upload-sessions", json=payload)
                        self.assertEqual(begun.status_code, 200, begun.text)
                        job_id = begun.json()["job_id"]
                        half = len(picture) // 2
                        first = client.put(f"/api/upload-sessions/{job_id}/files/images/0",
                                           headers={"x-upload-offset": "0"}, content=picture[:half])
                        self.assertEqual(first.status_code, 200, first.text)
                        # Force an in-memory state loss as after a service restart.
                        maker.jobs.clear()
                        progress = client.get(f"/api/upload-sessions/{job_id}")
                        self.assertEqual(progress.status_code, 200)
                        self.assertEqual(progress.json()["files"]["images"][0]["uploaded"], half)
                        self.assertEqual(client.post(f"/api/upload-sessions/{job_id}/finalize").status_code, 409)
                        self.assertEqual(client.get(f"/api/upload-sessions/{job_id}",
                                                    headers={"x-auth-user": "someone-else"}).status_code, 403)
                        self.assertEqual(client.put(f"/api/upload-sessions/{job_id}/files/images/0",
                                                    headers={"x-upload-offset": "0"},
                                                    content=b"duplicate").status_code, 409)
                        rest = client.put(f"/api/upload-sessions/{job_id}/files/images/0",
                                          headers={"x-upload-offset": str(half)}, content=picture[half:])
                        self.assertEqual(rest.status_code, 200, rest.text)
                        self.assertTrue(rest.json()["complete"])
                        finished = client.post(f"/api/upload-sessions/{job_id}/finalize")
                        self.assertEqual(finished.status_code, 200, finished.text)
                        self.assertEqual(finished.json()["status"], "queued")
                        self.assertEqual(client.post(f"/api/upload-sessions/{job_id}/finalize").status_code, 200)
                        self.assertEqual(enqueue.call_count, 1)
                        recorded = maker.get_state(job_id)
                        self.assertEqual(recorded["status"], "queued")
                        self.assertEqual(recorded["upload_progress"], 100)
                        self.assertEqual(len(recorded["config"]["output_names"]), 2)
                        self.assertEqual(client.delete(f"/api/upload-sessions/{job_id}").status_code, 409)
            finally:
                maker.executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()

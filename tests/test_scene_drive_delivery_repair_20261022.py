"""Regression coverage for Drive-only completion and safe retention."""
from __future__ import annotations
import ast
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
WORKER=(ROOT/"apps/scene-builder/part02/api_server.py").read_text()
WEB=(ROOT/"apps/scene-builder/part01/app.py").read_text()
QUEUE=(ROOT/"apps/scene-builder/part01/templates/queue.html").read_text()
INSTALL=(ROOT/"production/scene_production_worker.py").read_text()

class SceneDriveDeliveryTests(unittest.TestCase):
    def test_delivery_gate_and_existing_backfill(self):
        ast.parse(WORKER)
        self.assertIn('drive_required = bool(DRIVE_UPLOAD_ENABLED)', WORKER)
        self.assertIn('if not drive_required:', WORKER)
        self.assertIn('def _backfill_existing_drive_deliveries()', WORKER)
        self.assertIn('def _complete_existing_drive_delivery(job_id:', WORKER)
        self.assertIn('@app.post("/jobs/{job_id}/upload-drive")', WORKER)
        self.assertIn('upload_output_to_drive(\n                job_id, project_name, file_path, channel_name)', WORKER)
        self.assertIn('DRIVE_UPLOAD_ENABLED", "0" if WORKER_ADMISSION_ONLY else "1"', WORKER)

    def test_verified_remote_and_scriptless_fallback(self):
        self.assertIn('["rclone", "lsjson", destination, "--stat", "--hash"]', WORKER)
        self.assertIn('if not remote_md5 or remote_md5 != _file_md5(output_file):', WORKER)
        self.assertIn('else ["rclone", "copyto", str(output_file)', WORKER)
        self.assertIn('"--immutable"', WORKER)

    def test_unverified_mp4_cannot_be_swept(self):
        self.assertIn('safe = str(row.get("drive_status") or "") == "completed"', WORKER)
        self.assertIn('A failed Drive transfer must never delete the already-rendered MP4.', WORKER)
        self.assertIn('if row["status"] == "completed" and row.get("drive_status") != "completed":', WORKER)

    def test_authenticated_web_and_pending_ui(self):
        ast.parse(WEB)
        self.assertIn('@app.post("/queue/{job_id}/upload-drive")', WEB)
        self.assertIn('require_job_access(request, job_id)', WEB.split('def upload_existing_render_to_drive_web',1)[1].split('@app.post(',1)[0])
        self.assertIn('Upload to Drive', QUEUE)
        self.assertIn('Retry Drive Upload', QUEUE)
        self.assertIn('DRIVE PENDING', QUEUE)
        self.assertIn("'DRIVE_UPLOAD_ENABLED':'1'", INSTALL)
        self.assertNotIn("'DRIVE_UPLOAD_ENABLED','DRIVE_REMOTE'", INSTALL)
        REFRESH=(ROOT/'production/scene_worker_source_refresh.py').read_text()
        ast.parse(REFRESH)
        self.assertIn('self.original_env', REFRESH)
        self.assertIn('rclone", "lsd", remote+":"', REFRESH)
        self.assertIn('DRIVE_UPLOAD_ENABLED=1', REFRESH)
        self.assertIn('worker.ENV, self.original_env, 0o600', REFRESH)
        import runpy
        policy=runpy.run_path(str(ROOT/'apps/scene-builder/part01/precutover_policy.py'))
        can_write=policy['mutation_allowed']
        owned='/queue/'+'a'*32+'/upload-drive'
        self.assertTrue(can_write('POST', owned, True))
        self.assertFalse(can_write('POST', owned, False))
        self.assertFalse(can_write('GET', owned, True))
        self.assertFalse(can_write('POST', '/queue/x/upload-drive', True))

if __name__=="__main__": unittest.main()

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

    def test_numbered_drive_duplicates_do_not_replace_prior_output(self):
        tree = ast.parse(WORKER)
        named = {node.name: node for node in tree.body
                 if isinstance(node, ast.FunctionDef)}
        functions = ["safe_project_name", "drive_output_filename",
                     "_numbered_drive_output_filename"]
        module = ast.Module(body=[named[name] for name in functions], type_ignores=[])
        namespace = {}
        exec(compile(module, "<filename-functions>", "exec"), namespace)
        filename = namespace["_numbered_drive_output_filename"]
        self.assertEqual(filename("Alex_Eala_2min_SceneBuilder_40_Scenes", 1),
                         "Alex_Eala_2min_SceneBuilder_40_Scenes.mp4")
        self.assertEqual(filename("Alex_Eala_2min_SceneBuilder_40_Scenes", 2),
                         "Alex_Eala_2min_SceneBuilder_40_Scenes_2.mp4")
        self.assertEqual(filename("Alex_Eala_2min_SceneBuilder_40_Scenes", 3),
                         "Alex_Eala_2min_SceneBuilder_40_Scenes_3.mp4")
        self.assertIn("_drive_name_reserved(destination, job_id)", WORKER)
        self.assertIn("with _drive_naming_lock:", WORKER)
        self.assertIn("local file preserved:", WORKER)
        self.assertIn("const labels=duplicateProjectLabels()", QUEUE)
        self.assertIn("totals.get(name)>1", QUEUE)

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
        self.assertIn("CANONICAL_RCLONE_CONFIGS", REFRESH)
        self.assertIn("self._private_scene_rclone_config(remote)", REFRESH)
        self.assertIn("self.original_rclone_path.unlink(missing_ok=True)", REFRESH)
        self.assertIn('"RCLONE_CONFIG="+str(config_file)', REFRESH)
        self.assertIn('ProtectHome=true', REFRESH)
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

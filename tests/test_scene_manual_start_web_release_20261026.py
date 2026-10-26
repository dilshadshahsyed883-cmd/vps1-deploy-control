"""Web-only Scene manual-start hotfix must preserve the writable worker and queue."""
from __future__ import annotations
from pathlib import Path
import ast
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "apps/scene-builder/part01/precutover_policy.py"
SOURCE = (ROOT / "production/scene_manual_start_web_release.py").read_text()
RUNTIME = (ROOT / "production/runtime.py").read_text()
WEB = (ROOT / "apps/scene-builder/part01/app.py").read_text()


class ManualStartWebReleaseTests(unittest.TestCase):
    def test_policy_allows_exact_authenticated_job_start_not_get(self):
        namespace = {}
        exec(compile(POLICY.read_text(), str(POLICY), "exec"), namespace)
        allowed = namespace["mutation_allowed"]
        prefix = "/queue/" + "a" * 32
        for action in ("start", "delete", "restart-stage"):
            self.assertTrue(allowed("POST", prefix + "/" + action, True))
            self.assertFalse(allowed("POST", prefix + "/" + action, False))
            self.assertFalse(allowed("GET", prefix + "/" + action, True))
            self.assertFalse(allowed("POST", prefix + "/" + action + "/extra", True))
        self.assertFalse(allowed("POST", "/queue/not-a-job/start", True))
        self.assertIn('@app.post("/queue/{job_id}/start")', WEB)
        self.assertIn('require_job_access(request, job_id)\n    worker_request("POST", f"/jobs/{job_id}/start")', WEB)

    def test_policy_only_overlay_never_mutates_render_worker(self):
        ast.parse(SOURCE)
        for phrase in ('shutil.copytree(old, target, symlinks=True)',
                       '"part01/precutover_policy.py"',
                       'atomic_link(CURRENT, target)', 'atomic_link(CURRENT, old)',
                       '"systemctl", "restart", UNIT',
                       '"worker": "UNTOUCHED"', '"user_jobs": "UNTOUCHED"',
                       'SCENE_MANUAL_START_WEB=PASS'):
            self.assertIn(phrase, SOURCE)
        for dangerous in ('scene-production-worker.service', 'DELETE FROM jobs',
                          'UPDATE jobs SET', 'worker_source_refresh.refresh()',
                          'shutil.rmtree(JOBS', 'rclone deletefile'):
            self.assertNotIn(dangerous, SOURCE)
        self.assertIn("if '[vps1-scene-web-policy-only]' in intent:", RUNTIME)
        self.assertIn("release_manual_start(revision)", RUNTIME)

    def test_compiled_runtime_and_policy(self):
        ast.parse(RUNTIME)
        ast.parse(POLICY.read_text())
        self.assertIn('SCENE_PRECUTOVER_EDITING', WEB)
        self.assertIn('candidate_mutation_allowed(', WEB)


if __name__ == "__main__":
    unittest.main()

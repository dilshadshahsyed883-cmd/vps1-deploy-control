"""Safety contracts for the one-shot Short Maker drain-aware production workflow."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/vps1-short-maker-drain-release.yml"
RUNTIME = ROOT / "production/runtime.py"


class ShortMakerDrainReleaseTests(unittest.TestCase):
    def test_defers_without_touching_live_job(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        runtime = RUNTIME.read_text(encoding="utf-8")
        self.assertIn("[vps1-short-maker-drain-release]", workflow)
        self.assertIn("http://127.0.0.1:8146/api/jobs", workflow)
        self.assertIn('if not isinstance(jobs, list):', workflow)
        self.assertIn('"completed", "failed", "cancelled", "canceled"', workflow)
        self.assertIn("SHORT_MAKER_UNFINISHED_JOBS=", workflow)
        self.assertIn("needs: wait-until-idle", workflow)
        self.assertIn("needs.wait-until-idle.result == 'success'", workflow)
        self.assertLess(workflow.index("wait-until-idle:"), workflow.index("\n  deploy:"))
        self.assertEqual(workflow.count("vps1-production-deploy install"), 1)
        self.assertIn("install $SOURCE_SHA short-maker", workflow)
        self.assertIn("group: vps1-production-applications", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("StrictHostKeyChecking=yes", workflow)
        self.assertIn("AUTO_SCRIPT=OFFLINE", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("cancel job", workflow)
        self.assertIn("require_short_maker_idle(state)", runtime)


if __name__ == "__main__":
    unittest.main()

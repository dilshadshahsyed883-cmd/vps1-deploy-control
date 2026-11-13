"""Safety contracts for Short Maker's protected, idle-guarded release."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/vps1-short-maker-drain-release.yml"
RUNTIME = ROOT / "production/runtime.py"
SERVER = ROOT / "apps/short-maker/part01/app.py"


class ShortMakerDrainReleaseTests(unittest.TestCase):
    def test_anonymous_readiness_does_not_claim_global_queue_idle(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        server = SERVER.read_text(encoding="utf-8")
        self.assertIn("[vps1-short-maker-drain-release]", workflow)
        self.assertIn('http://127.0.0.1:8146/health', workflow)
        self.assertIn('response.get("ok") is not True', workflow)
        self.assertIn('response.get("app") != "short-maker"', workflow)
        self.assertIn('SHORT_MAKER_FINAL_IDLE_PROOF=ROOT_INSTALLER_REQUIRED', workflow)
        self.assertNotIn('curl -fsS --max-time 15 http://127.0.0.1:8146/api/jobs', workflow)
        self.assertNotIn('X-Auth-User:', workflow)
        self.assertIn('@app.get("/api/jobs")', server)
        self.assertIn('if short_can_access(state, request)', server)
        self.assertIn('def short_identity(request: Request):', server)
        preflight = workflow.split("  preflight-live-health:", 1)[1].split("\n  deploy:", 1)[0]
        deploy = workflow.split("\n  deploy:", 1)[1]
        self.assertIn("environment: production", preflight)
        self.assertIn("environment: production", deploy)
        self.assertIn("secrets.VPS1_SSH_PRIVATE_KEY", preflight)
        self.assertIn("needs: preflight-live-health", deploy)
        self.assertIn("needs.preflight-live-health.result == 'success'", deploy)
        self.assertLess(workflow.index("preflight-live-health:"), workflow.index("\n  deploy:"))

    def test_install_fails_closed_before_short_maker_restart(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        runtime = RUNTIME.read_text(encoding="utf-8")
        self.assertEqual(workflow.count("vps1-production-deploy install"), 1)
        self.assertIn("install $SOURCE_SHA short-maker", workflow)
        self.assertIn("group: vps1-production-applications", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("StrictHostKeyChecking=yes", workflow)
        self.assertIn("AUTO_SCRIPT=OFFLINE", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("def require_short_maker_idle(state):", runtime)
        self.assertIn('if status not in {"completed", "failed", "cancelled", "canceled"}:', runtime)
        self.assertIn('raise RuntimeError("Short Maker has an unfinished job; preserving live render")', runtime)
        install = runtime.split("def install_app(app, revision):", 1)[1]
        before_stop = install.index("if app == 'short-maker' and active:")
        stop = install.index("run(['systemctl', 'stop', unit_name]")
        self.assertLess(before_stop, stop)
        self.assertIn("require_short_maker_idle(state)", install[before_stop:stop])


if __name__ == "__main__":
    unittest.main()

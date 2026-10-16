import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/apply_nginx_routes.sh"
MARKER = "# managed-by: vps1-deploy-control; renderer: v1"

class NginxGuardTests(unittest.TestCase):
    def test_shell_syntax(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(SCRIPT)]).returncode, 0)

    def test_candidate_marker_required_and_check_is_inert(self):
        with tempfile.TemporaryDirectory() as temp:
            candidate = pathlib.Path(temp) / "routes.conf"
            candidate.write_text("location ^~ /injected/ { return 200; }\n")
            blocked = subprocess.run(["bash", str(SCRIPT), "--check", str(candidate)], capture_output=True, text=True)
            self.assertNotEqual(blocked.returncode, 0)
            candidate.write_text(MARKER + "\n# synthetic fixture\n")
            verified = subprocess.run(["bash", str(SCRIPT), "--check", str(candidate)], capture_output=True, text=True)
            self.assertEqual(verified.returncode, 0)
            self.assertIn("NO_VPS_CHANGES=YES", verified.stdout)

    def test_apply_requires_privileged_drive_bridge(self):
        # Never attempt a real --apply on a GitHub runner. Confirm guarded calls
        # and rollback branch exist as source invariants only.
        source = SCRIPT.read_text()
        for gate in ("VPS1_CONFIG_CHANGE_APPROVED", "VPS1_AGENT_EXECUTION",
                     "DriveBridge", "restore_on_error", "NGINX_ROLLBACK_ATTEMPTED",
                     "nginx -t", "systemctl reload nginx"):
            self.assertIn(gate, source)

if __name__ == "__main__":
    unittest.main()

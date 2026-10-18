"""Offline contract for owner-only automatic VPS1 production admission.

GitHub production environment Required reviewers is an account-level setting,
not YAML. This contract protects owner-only runs after the owner disables
manual deployment approvals in Settings > Environments > production.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github/workflows/vps1-production.yml",
    ROOT / ".github/workflows/vps1-five-route-cutover.yml",
    ROOT / ".github/workflows/vps1-complete-production.yml",
)
OWNER = "github.actor == 'dilshadshahsyed883-cmd'"


class OwnerOnlyProductionAdmission(unittest.TestCase):
    def test_owner_only_main_and_production_environment(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                content = path.read_text()
                condition = content.split("    if: >-", 1)[1].split("    environment: production", 1)[0]
                self.assertIn(OWNER, condition)
                self.assertIn("github.ref == 'refs/heads/main'", condition)
                self.assertIn("    environment: production", content)
                self.assertIn("tailscale/github-action@v4", content)
                self.assertIn("VPS1_SSH_PRIVATE_KEY", content)
                self.assertIn("StrictHostKeyChecking=yes", content)
                self.assertIn("sudo -n /usr/local/sbin/vps1-production-deploy", content)

    def test_release_activation_requires_explicit_marker_or_manual_dispatch(self):
        app = WORKFLOWS[0].read_text()
        routes = WORKFLOWS[1].read_text()
        self.assertIn("github.event_name == 'workflow_dispatch'", app)
        self.assertIn("[vps1-production-release]", app)
        self.assertIn("[vps1-route-five-cutover]", routes)
        self.assertIn("github.event_name == 'push'", routes)
        self.assertIn("github.run_attempt == 1", routes)
        complete = WORKFLOWS[2].read_text()
        self.assertIn("[vps1-complete-production]", complete)
        self.assertIn("remote install scene-builder", complete)
        self.assertIn("remote install short-maker", complete)
        self.assertIn("remote nginx-route-only short-maker", complete)
        self.assertIn("AUTO_SCRIPT=OFFLINE", complete)

    def test_no_admin_bypass_or_approval_robot(self):
        for path in WORKFLOWS:
            with self.subTest(workflow=path.name):
                content = path.read_text().lower()
                self.assertNotIn("personal_access_token", content)
                self.assertNotIn("gh_pat", content)
                self.assertNotIn("review-deployments", content)
                self.assertNotIn("bypass_protection", content)


if __name__ == "__main__":
    unittest.main()

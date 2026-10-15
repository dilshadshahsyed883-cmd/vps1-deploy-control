"""Cross-component guardrails for source, shell, routing and release boundaries."""
import json
import unittest
from pathlib import Path

from rebuild import ci_release, deploy_control
from tools.render_nginx_routes import validate_registry

ROOT = Path(__file__).resolve().parents[2]


class IntegratedPolicyTests(unittest.TestCase):
    def test_all_managed_routes_registered_not_offline(self):
        groups, profiles, active, offline = deploy_control.registry()
        routing = json.loads((ROOT/"infrastructure/registry/apps.json").read_text())
        routes = validate_registry(routing)
        self.assertEqual(len(profiles), len(groups))
        self.assertTrue(set(x["id"] for x in routes) <= active)
        self.assertFalse(set(x["id"] for x in routes) & offline)
        for route in routes:
            ports = [service["port"] for service in profiles[route["id"]]["services"]]
            self.assertIn(route["upstream_port"], ports)
        self.assertEqual(offline, {"auto-script"})

    def test_ci_candidate_is_subset_of_offline_planner(self):
        config = json.loads((ROOT/"rebuild/groups.json").read_text())
        examples = [
            ["apps/short-maker/part01/app.py"],
            ["apps/voice-studio/part01/kokoro_api.py"],
            ["apps/auto-script/auto_script.py"],
            ["apps/kokoro/kokoro_api.py"],
            ["rebuild/groups.json", "apps/short-maker/part01/app.py"],
            ["infrastructure/registry/apps.json", "apps/short-maker/part01/app.py"],
        ]
        for paths in examples:
            with self.subTest(paths=paths):
                ci = ci_release.classify(paths, config)
                plan = deploy_control.plan(paths)
                if not ci["manual_review"]:
                    self.assertEqual(set(ci["apps"]), set(plan["source_release_candidates"]))
                self.assertFalse(plan["production_deployment_authorized"])
                self.assertFalse(plan["deploy_command_executed"])

    def test_no_unguarded_production_deploy(self):
        cfg = json.loads((ROOT/"rebuild/groups.json").read_text())
        for key in deploy_control.FLAGS:
            self.assertIs(cfg[key], False)


if __name__ == "__main__":
    unittest.main()

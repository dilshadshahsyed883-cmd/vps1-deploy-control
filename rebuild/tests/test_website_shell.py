import json
import tempfile
import unittest
from pathlib import Path

from rebuild import website_shell, fresh_vps


class WebsiteShellTests(unittest.TestCase):
    def setUp(self):
        self.groups = {
            "groups": [
                {"id": "scene-builder", "deployment_mode": "active"},
                {"id": "new-app", "deployment_mode": "active"},
                {"id": "auto-script", "deployment_mode": "offline"},
            ]
        }
        self.profiles = {"groups": {
            "scene-builder": {"services": [{"part": "part01", "port": 8080}]},
            "new-app": {"services": []},
            "auto-script": {"services": [], "deployment_mode": "offline"},
        }}

    def test_registration_is_dynamic_and_excludes_offline(self):
        catalog = website_shell.catalog(self.groups, self.profiles)
        self.assertEqual([a["id"] for a in catalog["apps"]], ["scene-builder", "new-app"])
        self.assertEqual(catalog["apps"][0]["path"], "/scene-builder/")
        self.assertTrue(catalog["apps"][0]["route_configured"])
        self.assertFalse(catalog["apps"][1]["route_configured"])
        self.assertFalse(any(a["runtime_verified"] for a in catalog["apps"]))

    def test_mismatched_registration_and_unsafe_slug_fail_closed(self):
        bad = {"groups": [{"id": "../unsafe"}]}
        with self.assertRaises(ValueError):
            website_shell.catalog(bad, {"groups": {"../unsafe": {"services": []}}})
        with self.assertRaises(ValueError):
            website_shell.catalog(self.groups, {"groups": {"scene-builder": {}}})
        profiles = json.loads(json.dumps(self.profiles))
        profiles["groups"]["new-app"]["deployment_mode"] = "offline"
        with self.assertRaises(ValueError):
            website_shell.catalog(self.groups, profiles)

    def test_creates_isolated_shell_without_app_ui_modification(self):
        with tempfile.TemporaryDirectory(prefix="website-shell-", dir="/tmp") as d:
            root = Path(d)
            result = website_shell.build(root, self.groups, self.profiles)
            shell = root / "srv/fresh-website/shell"
            self.assertEqual(result["navigation_apps"], 2)
            self.assertEqual(result["routed_apps"], 1)
            self.assertEqual(result["services_activated"], 0)
            self.assertTrue((shell / "index.html").is_file())
            self.assertTrue((shell / "assets/shell.css").is_file())
            self.assertTrue((shell / "assets/shell.js").is_file())
            payload = json.loads((shell / "assets/apps.json").read_text())
            self.assertEqual(payload["apps"][1]["id"], "new-app")
            self.assertNotIn("auto-script", (shell / "assets/apps.json").read_text())
            self.assertIn("appFrame", (shell / "index.html").read_text())

    def test_cannot_write_to_host(self):
        with self.assertRaises(ValueError):
            website_shell.build(Path("/etc"), self.groups, self.profiles)

    def test_current_rebuild_registration_and_nginx_source(self):
        catalog = website_shell.catalog(
            json.loads(fresh_vps.builder.GROUPS.read_text()),
            fresh_vps.registry(),
        )
        self.assertEqual(len(catalog["apps"]), 10)
        self.assertNotIn("auto-script", [a["id"] for a in catalog["apps"]])
        source = Path(fresh_vps.__file__).read_text()
        self.assertIn("website_shell.build(root", source)
        self.assertIn("location = / { alias /srv/fresh-website/shell/index.html; }", source)
        self.assertIn("location ^~ /shell-assets/", source)


if __name__ == "__main__":
    unittest.main()

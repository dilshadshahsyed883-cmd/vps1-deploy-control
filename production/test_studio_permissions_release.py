"""Offline release contract for the 3-Studio permission cutover (no VPS actions)."""
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from production import studio_permissions_release as release
from production import gateway, runtime
from production import short_studio_release as short
from production import copyright_studio_release as copyright

ROOT = Path(__file__).resolve().parents[1]


class StudioPermissionsReleaseTests(unittest.TestCase):
    def test_reviewed_scoped_sources_have_security_guards(self):
        base = ROOT / "apps/scene-builder"
        source = {name: (base / name).read_bytes() for name in release.FILES}
        release.validate_sources(source)
        self.assertEqual(set(release.FILES), {
            "part01/app.py",
            "part01/templates/base.html",
            "part01/templates/user_management.html",
            "part01/static/vps1_native_site_shell.js",
            "part01/static/vps1_native_site_shell.css",
        })
        source["part01/static/vps1_native_site_shell.js"] = b"/* identity not checked */"
        with self.assertRaisesRegex(RuntimeError, "native sidebar"):
            release.validate_sources(source)

    def test_studio_routes_migrate_without_rewriting_other_services(self):
        from production.route_uploader_scene_https import ANCHOR, MARKER as LUM_MARKER
        base = ("server {\n location = /_scene_uploader_auth {\n internal;\n}\n"
                " auth_request /_scene_uploader_auth;\n "
                + LUM_MARKER + "\n" + ANCHOR + "\n}\n")
        legacy = base.replace(ANCHOR, short.LEGACY_ROUTE + copyright.LEGACY_ROUTE + ANCHOR)
        upgraded_short = short.apply_route(legacy)
        upgraded_both = copyright.apply_route(upgraded_short)
        self.assertEqual(upgraded_both.count(short.ROUTE), 1)
        self.assertEqual(upgraded_both.count(copyright.ROUTE), 1)
        self.assertEqual(short.apply_route(upgraded_both), upgraded_both)
        self.assertEqual(copyright.apply_route(upgraded_both), upgraded_both)
        self.assertIn("auth_request /_scene_uploader_auth;", upgraded_both)
        self.assertIn(LUM_MARKER, upgraded_both)

    def test_release_fails_closed_on_invalid_revision_or_preflight(self):
        with self.assertRaisesRegex(RuntimeError, "source SHA"):
            release.install("invalid")
        with patch.object(release, "preflight", side_effect=RuntimeError("uploads active")):
            with self.assertRaisesRegex(RuntimeError, "uploads active"):
                release.install("a" * 40)

    def test_existing_root_and_gateway_lane_are_scoped(self):
        runtime_source = (ROOT / "production/runtime.py").read_text()
        gateway_source = (ROOT / "production/gateway.py").read_text()
        self.assertIn("if '[vps1-studio-permissions-release]' in intent:", runtime_source)
        self.assertIn("args.apps != ['scene-builder']", runtime_source)
        self.assertIn("from production.studio_permissions_release import install, rollback", runtime_source)
        self.assertIn("if '[vps1-studio-permissions-release]' in intent:", gateway_source)
        self.assertIn("THREE_STUDIO_PERMISSIONS_GATEWAY=NO_MUTATION", gateway_source)

    def test_no_worker_restart_or_media_mutation_in_release_source(self):
        contents = (ROOT / "production/studio_permissions_release.py").read_text()
        self.assertNotIn("workers.stop(", contents)
        self.assertNotIn("workers.install(", contents)
        self.assertNotIn("unlink(", contents)
        self.assertNotIn('run(["systemctl", "restart", WORKER]', contents)
        self.assertIn("backup_databases(STATE", contents)
        self.assertIn('worker_pid() == pid', contents)


if __name__ == "__main__":
    unittest.main()

"""Safe Storage Manager automatic-clean status regression (no VPS, no deletes)."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "apps/vps-storage-manager/part01/storage_manager.py"


def load_manager():
    spec = importlib.util.spec_from_file_location("storage_auto_status_probe", MANAGER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class AutoCleanReadOnlyView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = load_manager()

    def test_readonly_preview_never_opens_protected_app_databases(self):
        module = self.manager
        fake_disk = SimpleNamespace(total=1000, used=250, free=750)
        status = {
            "status": "active", "active": True, "enabled": True,
            "interval_hours": 12, "grace_hours": 12,
        }
        with patch.object(module, "acs_verified_projects", side_effect=AssertionError("ACS DB must not open")), \\
             patch.object(module, "scan_algrow", side_effect=AssertionError("Algrow DB must not open")), \\
             patch.object(module, "scan_scene", side_effect=AssertionError("Scene DB must not open")), \\
             patch.object(module, "scan_caches", side_effect=AssertionError("privileged cache scan must not run")), \\
             patch.object(module.shutil, "disk_usage", return_value=fake_disk), \\
             patch.object(module, "load_policy", return_value={"auto_enabled": False}), \\
             patch.object(module, "deployment_cleanup_status", return_value=status):
            preview = module.readonly_snapshot()
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["partial"])
        self.assertTrue(preview["read_only"])
        self.assertEqual(preview["deployment_cleanup"], status)
        self.assertEqual(preview["reclaimable_bytes"], 0)
        self.assertTrue(all(item["available"] is False for item in preview["categories"]))
        self.assertNotIn("operations", preview)

    def test_timer_fails_closed_when_systemd_is_unreadable(self):
        module = self.manager
        with patch.object(module, "run", side_effect=OSError("systemctl not available")):
            value = module.deployment_cleanup_status()
        self.assertEqual(value["status"], "unknown")
        self.assertFalse(value["active"])
        self.assertFalse(value["enabled"])

    def test_timer_active_only_when_enabled_and_active(self):
        module = self.manager

        def command(args, timeout=8):
            return SimpleNamespace(stdout="active\\n" if args[1] == "is-active" else "enabled\\n")
        with patch.object(module, "run", side_effect=command):
            value = module.deployment_cleanup_status()
        self.assertEqual(value["status"], "active")
        self.assertEqual(value["interval_hours"], 12)

    def test_protected_candidate_get_uses_preview_not_full_scan(self):
        module = self.manager
        handler = object.__new__(module.Handler)
        handler.path = "/v1/snapshot"
        handler.authenticated = lambda: True
        seen = []
        handler.respond = lambda status, data: seen.append((status, data))
        with patch.dict(os.environ, {"VPS1_RUNTIME_CANDIDATE": "1"}), \\
             patch.object(module, "readonly_snapshot", return_value={"ok": True, "partial": True}), \\
             patch.object(module, "latest_snapshot", side_effect=AssertionError("protected source read")):
            handler.do_GET()
        self.assertEqual(seen, [(200, {"ok": True, "partial": True})])

    def test_candidate_post_and_legacy_auto_delete_are_still_blocked(self):
        source = MANAGER.read_text()
        self.assertIn('return self.respond(403, {"ok": False, "error": "candidate read-only"})', source)
        self.assertIn('if os.getenv("FRESH_WEBSITE_SMOKE_TEST") == "1" or os.getenv("VPS1_RUNTIME_CANDIDATE") == "1":', source)
        self.assertIn('"auto_enabled": False', source)

    def test_html_and_js_distinguish_timer_from_project_cleanup(self):
        ui = (ROOT / "apps/scene-builder/part01").resolve()
        template = (ui / "templates/storage_manager.html").read_text()
        script = (ui / "static/storage_manager.js").read_text()
        self.assertIn('id="deployment-gc-state"', template)
        self.assertIn('id="deployment-gc-detail"', template)
        self.assertIn('App-owned · Drive receipt required', template)
        self.assertIn('const partial = data.partial === true || data.read_only === true', script)
        self.assertIn('renderAutomationStatus(data.deployment_cleanup)', script)
        self.assertIn("refresh && managerCanModify", script)
        self.assertIn("snapshot = partial ? null : data", script)
        self.assertIn('managerFallback || !managerCanModify || busy || !snapshot', script)

    def test_release_remains_scoped_and_no_cleanup_triggered(self):
        wf = (ROOT / ".github/workflows/vps1-storage-backend-retirement-release.yml").read_text()
        self.assertIn("[vps1-storage-auto-clean-ui-release]", wf)
        self.assertIn('install $SOURCE_SHA vps-storage-manager', wf)
        self.assertIn('install $SOURCE_SHA scene-builder', wf)
        self.assertIn("CLEANUP_NOT_EXECUTED=YES", wf)


if __name__ == "__main__":
    unittest.main()

"""Safe Storage Manager automatic-clean status regression (no VPS, no deletes)."""
from __future__ import annotations

import importlib.util
import json
import tempfile
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
        with patch.object(module, "acs_verified_projects", side_effect=AssertionError("ACS DB must not open")), \
             patch.object(module, "scan_algrow", side_effect=AssertionError("Algrow DB must not open")), \
             patch.object(module, "scan_scene", side_effect=AssertionError("Scene DB must not open")), \
             patch.object(module, "scan_caches", side_effect=AssertionError("privileged cache scan must not run")), \
             patch.object(module.shutil, "disk_usage", return_value=fake_disk), \
             patch.object(module, "load_policy", return_value={"auto_enabled": False}), \
             patch.object(module, "deployment_cleanup_status", return_value=status):
            preview = module.readonly_snapshot()
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["partial"])
        self.assertTrue(preview["read_only"])
        self.assertEqual(preview["deployment_cleanup"], status)
        self.assertEqual(preview["reclaimable_bytes"], 0)
        self.assertTrue(all(item["available"] is False for item in preview["categories"]))
        self.assertNotIn("operations", preview)

    def test_manual_spool_owner_is_scene_identity_not_candidate_identity(self):
        module = self.manager
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "manual"
            directory.mkdir(mode=0o700)
            # Storage Manager may run as another account; the spool belongs
            # to the authenticated Scene web worker. No root grant is needed.
            available, pending = module._manual_gc_spool_status(
                directory, owner_uid=os.geteuid(), path_active=True)
            self.assertTrue(available)
            self.assertFalse(pending)
            self.assertEqual(module._manual_gc_spool_status(
                directory, owner_uid=os.geteuid()+1, path_active=True),
                (False, False))
            self.assertEqual(module._manual_gc_spool_status(
                directory, owner_uid=os.geteuid(), path_active=False),
                (False, False))
            (directory / "request").touch()
            self.assertEqual(module._manual_gc_spool_status(
                directory, owner_uid=os.geteuid(), path_active=True),
                (True, True))
            (directory / "request").unlink()
            directory.chmod(0o755)
            self.assertEqual(module._manual_gc_spool_status(
                directory, owner_uid=os.geteuid(), path_active=True),
                (False, False))

    def test_manual_spool_owner_follows_verified_scene_service_identity(self):
        module = self.manager
        import pwd
        uid = os.geteuid()
        def command(args, timeout=8):
            return SimpleNamespace(stdout="User=testscene\nActiveState=active\n")
        with patch.object(module, "run", side_effect=command), \
             patch.object(module.pwd, "getpwnam",
                          return_value=SimpleNamespace(pw_uid=uid if uid > 0 else 33)):
            expected = module._scene_manual_spool_owner_uid()
        self.assertEqual(expected, uid if uid > 0 else 33)

        for invalid in ("User=root\nActiveState=active\n",
                        "User=\nActiveState=active\n",
                        "User=testscene\nActiveState=inactive\n",
                        "User=testscene\nActiveState=active\nUser=testscene\n"):
            with patch.object(module, "run", return_value=SimpleNamespace(stdout=invalid)):
                self.assertEqual(module._scene_manual_spool_owner_uid(), -1)

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
            return SimpleNamespace(stdout="active\n" if args[1] == "is-active" else "enabled\n")
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
        with patch.dict(os.environ, {"VPS1_RUNTIME_CANDIDATE": "1"}), \
             patch.object(module, "readonly_snapshot", return_value={"ok": True, "partial": True}), \
             patch.object(module, "latest_snapshot", side_effect=AssertionError("protected source read")):
            handler.do_GET()
        self.assertEqual(seen, [(200, {"ok": True, "partial": True})])

    def test_legacy_scheduler_can_only_choose_cache_categories(self):
        module = self.manager
        with patch.object(module.sqlite3, "connect", side_effect=OSError("fixture no state DB")):
            policy = module.load_policy()
        self.assertFalse(policy["auto_enabled"])
        self.assertTrue(set(policy["auto_categories"]).issubset(module.CACHE_CATEGORIES))
        self.assertTrue(set(module.PROJECT_CATEGORIES).isdisjoint(policy["auto_categories"]))

    def test_central_project_deletion_is_rejected_before_any_scan(self):
        module = self.manager
        with patch.object(module, "perform_scan", side_effect=AssertionError("must reject before scan")), \
             patch.object(module, "FULL_CLEAN_LOCK", Path("/tmp/nonexistent-storage-clean-lock-test")):
            with self.assertRaisesRegex(RuntimeError, "belongs to each application"):
                module.cleanup({"categories": ["acs_projects"]}, automatic=True)

    def test_candidate_post_and_legacy_auto_delete_are_still_blocked(self):
        source = MANAGER.read_text()
        self.assertIn('return self.respond(403, {"ok": False, "error": "candidate read-only"})', source)
        self.assertIn('if os.getenv("FRESH_WEBSITE_SMOKE_TEST") == "1" or os.getenv("VPS1_RUNTIME_CANDIDATE") == "1":', source)
        self.assertIn('"auto_enabled": False', source)

    def test_dashboard_has_no_legacy_delete_or_policy_controls(self):
        ui = (ROOT / "apps/scene-builder/part01").resolve()
        template = (ui / "templates/storage_manager.html").read_text()
        script = (ui / "static/storage_manager.js").read_text()
        for required in ('id="deployment-gc-state"', 'id="deployment-gc-detail"',
                         'id="gc-next-run"', 'id="gc-last-run"', 'id="gc-last-freed"',
                         'id="gc-eligible"', 'App-owned · Drive receipt required'):
            self.assertIn(required, template)
        for retired in ('id="policy-form"', 'id="confirm-dialog"',
                        'id="reclaim-button"', 'id="full-production-clean"',
                        'id="select-recommended"', 'id="category-grid"'):
            self.assertNotIn(retired, template)
        self.assertIn("renderCleanupStatus(data.deployment_cleanup)", script)
        self.assertIn("data.read_only !== true || data.partial !== true", script)
        self.assertIn("method:'GET'", script)
        self.assertIn("run-safe-cleanup", script)
        self.assertIn("X-Storage-CSRF", script)
        self.assertIn("confirmation:'RUN SAFE CLEANUP'", script)
        for blocked in ("/api/admin/storage/cleanup",
                        "/api/admin/storage/full-clean", "/api/admin/storage/policy",
                        "/api/admin/storage/scan", "Full Production Clean"):
            self.assertNotIn(blocked, script)

    def test_audit_and_next_run_are_read_only_and_use_actual_records(self):
        module = self.manager
        with tempfile.TemporaryDirectory() as tmp:
            entry = {
                "mode": "apply", "timestamp": 1792456676,
                "free_before_bytes": 50717003776, "free_after_bytes": 51126759424,
                "deleted": ["short-stream/old"], "skipped": [],
            }
            (Path(tmp) / "deployment-gc-history.jsonl").write_text(
                json.dumps(entry) + "\n", encoding="utf-8")
            def command(args, timeout=8):
                if args[1] == "show":
                    return SimpleNamespace(stdout="NextElapseUSecRealtime=Tue 2026-10-20 12:00:00 UTC\n")
                return SimpleNamespace(stdout="active\n" if args[1] == "is-active" else "enabled\n")
            with patch.object(module, "STATE_ROOT", Path(tmp)), \
                 patch.object(module, "run", side_effect=command):
                info = module.deployment_cleanup_status()
        self.assertEqual(info["status"], "active")
        self.assertEqual(info["last_cleanup"]["deleted_count"], 1)
        self.assertEqual(info["last_cleanup"]["measured_freed_bytes"], 409755648)
        self.assertEqual(info["next_run_at"], "Tue 2026-10-20 12:00:00 UTC")
        self.assertIsNone(info["eligible_bytes"])

    def test_no_audit_does_not_claim_zero_reclaim(self):
        module = self.manager
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(module, "STATE_ROOT", Path(tmp)), \
                 patch.object(module, "run", side_effect=OSError("systemctl blocked")):
                info = module.deployment_cleanup_status()
        self.assertIsNone(info["last_cleanup"])
        self.assertIsNone(info["eligible_bytes"])
        self.assertIsNone(info["next_run_at"])
        self.assertEqual(info["status"], "unknown")

    def test_release_remains_scoped_and_no_cleanup_triggered(self):
        wf = (ROOT / ".github/workflows/vps1-storage-backend-retirement-release.yml").read_text()
        self.assertIn("[vps1-storage-auto-clean-ui-release]", wf)
        self.assertIn('install $SOURCE_SHA vps-storage-manager', wf)
        self.assertIn('install $SOURCE_SHA scene-builder', wf)
        self.assertIn("CLEANUP_NOT_EXECUTED=YES", wf)
        adapter = (ROOT / "production/storage_backend_release.py").read_text()
        self.assertIn("require_preview=True", adapter)
        self.assertIn('"/v1/snapshot"', adapter)
        self.assertIn('preview.get("read_only") is not True', adapter)


    def test_monotonic_systemd_schedule_is_supported(self):
        module = self.manager
        def command(args, timeout=8):
            if args[1] == "show":
                return SimpleNamespace(stdout=(
                    "NextElapseUSecMonotonic=" + str(100_000_000) + "\n"
                    if "NextElapseUSecMonotonic" in args[3] else
                    "NextElapseUSecRealtime=n/a\n"))
            return SimpleNamespace(stdout="active\n" if args[1] == "is-active" else "enabled\n")
        with patch.object(module, "run", side_effect=command), \
             patch.object(module.time, "clock_gettime", return_value=90.0), \
             patch.object(module, "_public_deployment_gc_status", return_value=None):
            status = module.deployment_cleanup_status()
        self.assertIsNotNone(status["next_run_at"])
        self.assertEqual(status["status"], "active")

    def test_root_published_audit_and_verified_inventory_are_read_only(self):
        module = self.manager
        from datetime import datetime, timezone
        now = int(datetime.now(timezone.utc).timestamp())
        info = {
            "schema": 1, "generated_at": now, "eligible_measured_at": now,
            "eligible_bytes": 76543,
            "last_cleanup": {"timestamp": now - 300, "deleted_count": 7,
                "skipped_count": 0, "measured_freed_bytes": 409755648},
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(module, "STATE_ROOT", Path(tmp)), \
             patch.object(module, "_public_deployment_gc_status", return_value=info), \
             patch.object(module, "run", side_effect=OSError("systemd missing")):
            status = module.deployment_cleanup_status()
        self.assertEqual(status["last_cleanup"]["deleted_count"], 7)
        self.assertEqual(status["last_cleanup"]["measured_freed_bytes"], 409755648)
        self.assertEqual(status["eligible_bytes"], 76543)
        self.assertIsNotNone(status["eligible_measured_at"])

    def test_public_skip_reason_counts_are_bounded_and_timestamp_matched(self):
        module = self.manager
        from datetime import datetime, timezone
        now = int(datetime.now(timezone.utc).timestamp())
        public = {
            "schema": 1, "generated_at": now, "eligible_measured_at": now,
            "eligible_bytes": 98765, "eligible_candidate_count": 229,
            "last_cleanup": {
                "timestamp": now - 300, "deleted_count": 0,
                "skipped_count": 2, "measured_freed_bytes": 0,
                "skip_reason_counts": {
                    "process-in-use-or-unverifiable": 1,
                    "revalidation-changed": 1
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(module, "STATE_ROOT", Path(tmp)), \
             patch.object(module, "_public_deployment_gc_status", return_value=public), \
             patch.object(module, "run", side_effect=OSError("systemd unavailable")):
            result = module.deployment_cleanup_status()
            self.assertEqual(result["eligible_candidate_count"], 229)
            self.assertEqual(result["last_cleanup"]["skip_reason_counts"][
                "process-in-use-or-unverifiable"], 1)
            public["last_cleanup"]["skip_reason_counts"] = {
                "/root/private/secret": 2}
            bad = module.deployment_cleanup_status()
            self.assertNotIn("skip_reason_counts", bad["last_cleanup"])
            public["last_cleanup"]["skip_reason_counts"] = {
                "process-in-use-or-unverifiable": 2}
            public["last_cleanup"]["timestamp"] = now - 600
            original = {
                "mode": "apply", "timestamp": now - 300,
                "free_before_bytes": 1000, "free_after_bytes": 1000,
                "deleted": [], "skipped": ["control:deployment-active"]}
            (Path(tmp) / "deployment-gc-history.jsonl").write_text(
                json.dumps(original) + "\n")
            stale = module.deployment_cleanup_status()
            self.assertNotIn("skip_reason_counts", stale["last_cleanup"])

    def test_gc_public_export_excludes_sensitive_audit_paths(self):
        from production import storage_gc as gc
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "private"
            state.mkdir()
            public = Path(tmp) / "public" / "status.json"
            event = {"mode": "apply", "timestamp": 1792456676,
                     "deleted": ["/private/secret/release"], "skipped": [],
                     "free_before_bytes": 100000, "free_after_bytes": 140000}
            (state / "deployment-gc-history.jsonl").write_text(json.dumps(event) + "\n")
            with patch.object(gc, "execute", return_value={"inventory": {
                    "blocked": "", "estimated_bytes": 12345}}):
                result = gc.publish_readonly_status(
                    state=state, destination=public, test_mode=True)
            self.assertEqual(result["eligible_bytes"], 12345)
            self.assertEqual(result["last_cleanup"]["measured_freed_bytes"], 40000)
            exported = public.read_text()
            self.assertNotIn("/private/", exported)
            self.assertNotIn("secret", exported)
            self.assertEqual(public.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()

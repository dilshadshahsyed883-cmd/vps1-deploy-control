"""Regression tests for exact, fail-closed Storage dashboard web-only release.

No running VPS, systemd commands, worker queue reads or production deletions.
"""
from pathlib import Path
import ast
import fcntl
import os
import stat
import tempfile
import time
import unittest
from typing import Any

from production import storage_dashboard_web_release as overlay

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"


def reviewed_web() -> str:
    return (SCENE / "app.py").read_text(encoding="utf-8")


def simulated_pre_storage_web() -> str:
    source = reviewed_web()
    marker = 'MANUAL_STORAGE_GC_REQUEST_DIR = Path("/run/vps1-storage-gc-manual")'
    next_route = '@app.get("/api/admin/storage/history")'
    start = source.index(marker)
    end = source.index(next_route, start)
    source = source[:start] + source[end:]
    source = source.replace("    import secrets as shell_secrets\n", "", 1)
    source = source.replace(
        '"storage_csrf": request.session.setdefault("storage_csrf", shell_secrets.token_urlsafe(32)) if request.session.get("is_admin") else "",',
        '"storage_csrf": "",', 1)
    return source


class StorageWebOnlyRelease(unittest.TestCase):
    def test_patch_only_inserts_protected_route_and_admin_session_csrf(self):
        original = simulated_pre_storage_web()
        reviewed = reviewed_web()
        patched = overlay.patch_web(original, reviewed)
        self.assertEqual(patched.count('@app.post("/api/admin/storage/run-safe-cleanup")'), 1)
        self.assertIn('require_admin(request)', patched)
        self.assertIn('require_storage_csrf(request)', patched)
        self.assertIn('os.O_EXCL', patched)
        self.assertIn('import fcntl, stat, time', patched)
        self.assertIn('request.session.setdefault("storage_csrf", shell_secrets.token_urlsafe(32))', patched)
        self.assertIn("def scene", patched)
        self.assertEqual(overlay.patch_web(patched, reviewed), patched)
        self.assertIn(original.split("def persistent_workspace")[0][:250], patched)

    def test_existing_web_overlay_removes_only_old_candidate_veto(self):
        reviewed = reviewed_web()
        block_start = reviewed.index('MANUAL_STORAGE_GC_REQUEST_DIR = Path("/run/vps1-storage-gc-manual")')
        block_end = reviewed.index('@app.get("/api/admin/storage/history")', block_start)
        legacy = reviewed[:block_start] + reviewed[block_start:block_end].replace(
            'if os.getenv("FRESH_WEBSITE_SMOKE_TEST") == "1":',
            'if RUNTIME_CANDIDATE or os.getenv("FRESH_WEBSITE_SMOKE_TEST") == "1":'
        ) + reviewed[block_end:]
        patched = overlay.patch_web(legacy, reviewed)
        manual = patched[patched.index('MANUAL_STORAGE_GC_REQUEST_DIR'):patched.index(
            '@app.get("/api/admin/storage/history")')]
        self.assertNotIn('if RUNTIME_CANDIDATE or', manual)
        self.assertIn('if os.getenv("FRESH_WEBSITE_SMOKE_TEST") == "1":', manual)
        self.assertIn('require_admin(request)', manual)
        self.assertIn('require_storage_csrf(request)', manual)
        self.assertIn('os.O_EXCL', manual)
        self.assertEqual(patched, overlay.patch_web(patched, reviewed))

    def test_exact_candidate_gc_route_only_and_other_mutations_denied(self):
        from importlib.util import module_from_spec, spec_from_file_location
        candidate_file = SCENE / "precutover_policy.py"
        reviewed = candidate_file.read_text(encoding="utf-8")
        # Model the installed older candidate policy: no Storage Manager POST
        # allowlist, otherwise unchanged.
        exception = ('    if method == "POST" and path == '
                     '"/api/admin/storage/run-safe-cleanup":\n        return True\n')
        original = reviewed.replace(exception, "")
        self.assertNotEqual(original, reviewed)
        patched = overlay.patch_candidate_policy(original, reviewed)
        self.assertEqual(patched, overlay.patch_candidate_policy(patched, reviewed))
        self.assertEqual(patched, reviewed)
        with self.assertRaisesRegex(RuntimeError, "unexpected safety boundary"):
            overlay.patch_candidate_policy("def arbitrary(): pass", reviewed)
        namespace = {"__name__": "storage_policy_offline_test"}
        exec(compile(patched, str(candidate_file), "exec"), namespace)
        allowed = namespace["mutation_allowed"]
        self.assertTrue(allowed("POST", "/api/admin/storage/run-safe-cleanup", False))
        self.assertFalse(allowed("GET", "/api/admin/storage/run-safe-cleanup", False))
        self.assertFalse(allowed("POST", "/api/admin/storage/cleanup", False))
        self.assertFalse(allowed("POST", "/api/admin/storage/full-clean", False))
        self.assertFalse(allowed("POST", "/api/admin/storage/run-safe-cleanup/", False))
        self.assertFalse(allowed("POST", "/api/auto-script/tick", False,
                                 auto_script_enabled=False))

    def test_button_queues_exact_root_systemd_service_with_auditable_receipt(self):
        # No real systemd calls or production writes: execute only the reviewed
        # private queue function in an isolated 0700 tempfile spool.
        source = reviewed_web()
        code = ast.parse(source)
        queue = next(node for node in code.body
                     if isinstance(node, ast.FunctionDef)
                     and node.name == "queue_guarded_storage_gc")
        isolated = compile(ast.Module(body=[queue], type_ignores=[]),
                           str(SCENE / "app.py"), "exec")
        with tempfile.TemporaryDirectory() as folder:
            spool = Path(folder) / "private"
            spool.mkdir(mode=0o700)
            namespace = dict(Path=Path, MANUAL_STORAGE_GC_REQUEST_DIR=spool,
                             Any=Any, os=os, fcntl=fcntl, stat=stat, time=time)
            exec(isolated, namespace)
            submit = namespace["queue_guarded_storage_gc"]
            receipt = submit(now=1000.0)
            self.assertEqual(receipt["status"], "queued")
            self.assertEqual(receipt["requested_at"], 1000)
            self.assertEqual((spool / "request").read_text(), "1000\n")
            self.assertEqual(stat.S_IMODE((spool / "request").stat().st_mode), 0o600)
            duplicate = submit(now=1010.0)
            self.assertEqual(duplicate["status"], "queued")
            self.assertIsInstance(duplicate["requested_at"], int)
            (spool / "request").unlink()
            with self.assertRaisesRegex(RuntimeError, "two minutes"):
                submit(now=1060.0)
            spool.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "not safely provisioned"):
                submit(now=1200.0)
        path_unit = (ROOT / "infrastructure/systemd/vps1-storage-deployment-gc-manual.path").read_text()
        gc_unit = (ROOT / "infrastructure/systemd/vps1-storage-deployment-gc.service").read_text()
        self.assertIn("PathExists=/run/vps1-storage-gc-manual/request", path_unit)
        self.assertIn("Unit=vps1-storage-deployment-gc.service", path_unit)
        self.assertIn("--apply", gc_unit)
        self.assertIn("ExecStartPre=-/usr/bin/rm -f /run/vps1-storage-gc-manual/request", gc_unit)

    def test_button_does_not_confuse_queue_receipt_with_completed_audit(self):
        js = (SCENE / "static/storage_manager.js").read_text()
        self.assertIn("manualRunRequestedAt", js)
        self.assertIn("body.requested_at", js)
        self.assertIn("auditAt > (manualRunBaselineAuditAt || 0)", js)
        self.assertIn("stopManualPolling()", js)
        self.assertIn("Verified cleanup completed", js)
        self.assertNotIn("Queued safely. Refresh status", js)

    def test_web_overlay_copies_only_reviewed_guard_file_and_preserves_render_worker(self):
        script = (ROOT / "production/storage_dashboard_web_release.py").read_text()
        self.assertIn('files["part01/precutover_policy.py"] = policy.encode("utf-8")', script)
        self.assertIn('patch_candidate_policy(installed_policy.read_text', script)
        self.assertIn('require(worker_pid() == renderer_pid', script)
        self.assertIn('atomic_link(CURRENT, old)', script)

    def test_unknown_existing_web_shape_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "history route"):
            overlay.patch_web("def application(): pass", reviewed_web())
        with self.assertRaisesRegex(RuntimeError, "persistent shell"):
            overlay.patch_web('@app.get("/api/admin/storage/history")\ndef storage_history_api(): pass',
                              reviewed_web())
        with self.assertRaisesRegex(RuntimeError, "reviewed admin GC endpoint"):
            overlay.patch_web(simulated_pre_storage_web(), "def nobody(): pass")

    def test_asset_scope_is_admin_only_content_addressed(self):
        files = overlay.trusted_ui_files()
        self.assertIn("part01/app.py", overlay.BASE_FILES + ("part01/app.py",))
        self.assertIn("part01/templates/storage_manager.html", files)
        self.assertIn("part01/static/storage_manager.js", files)
        self.assertIn("part01/static/shell-v2/manifest.json", files)
        self.assertTrue(any(k.startswith("part01/static/shell-v2/host.") for k in files))
        self.assertTrue(any(k.startswith("part01/static/shell-v2/storage_manager.") for k in files))
        self.assertNotIn("part02", " ".join(files))
        self.assertNotIn("queue.sqlite3", " ".join(files))
        self.assertNotIn("auto_script", " ".join(files))

    def test_runtime_dispatch_has_exact_protected_tag(self):
        runtime = (ROOT / "production/runtime.py").read_text()
        self.assertIn("if '[vps1-storage-dashboard-web-only]' in intent:", runtime)
        self.assertIn("from production.storage_dashboard_web_release import install as install_storage_web", runtime)
        self.assertLess(runtime.index("if '[vps1-storage-dashboard-web-only]' in intent:"),
                        runtime.index("from production.scene_candidate_release import install as install_scene_candidate"))
        workflow = (ROOT / ".github/workflows/vps1-storage-dashboard-release.yml").read_text()
        self.assertIn("[vps1-storage-dashboard-release]", workflow)

    def test_worker_stays_untouched_and_rollback_is_mandatory(self):
        src = (ROOT / "production/storage_dashboard_web_release.py").read_text()
        self.assertIn("renderer_pid = worker_pid()", src)
        self.assertIn('require(worker_pid() == renderer_pid', src)
        self.assertIn('atomic_link(CURRENT, old)', src)
        self.assertIn('run(["systemctl", "restart", UNIT]', src)
        for dangerous in ("worker_source_refresh.refresh(", "worker.UNIT", "shutil.rmtree(old)",
                          "sqlite3.connect(", "vps1-scene-production-worker.service\",quiet"):
            self.assertNotIn(dangerous, src)


if __name__ == "__main__":
    unittest.main()

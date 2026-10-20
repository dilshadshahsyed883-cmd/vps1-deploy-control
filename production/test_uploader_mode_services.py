"""Isolated Auto Uploader app/worker installation contracts.

Only disposable temporary directories are touched. No live uploads, OAuth,
existing history, queues, scheduling data or host services are accessed.
"""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from production import workers
APP_SOURCE = Path(__file__).resolve().parents[1] / 'apps/local-upload-manager/part01'
sys.path.insert(0, str(APP_SOURCE))
from lum_engine.service_status import auto_engine_service_units


class ModeServicesTests(unittest.TestCase):
    def test_app_and_v6_workers_have_independent_units(self):
        names = workers.unit_names("local-upload-manager")
        self.assertEqual(len(names), len(set(names)))
        for mode in ("engine2", "shorts"):
            for task in ("web", "worker", "scheduler"):
                self.assertIn(f"vps1-production-lum-{mode}-{task}.service", names)

    def test_mode_has_own_worker_and_scheduler_status(self):
        with patch.dict(os.environ, {"FRESH_WEBSITE_INSTALL": "1"}):
            for mode, prefix in (
                ("engine1", "vps1-production-lum"),
                ("engine2", "vps1-production-lum-engine2"),
                ("shorts", "vps1-production-lum-shorts"),
            ):
                with self.subTest(mode=mode), patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": mode}):
                    units = auto_engine_service_units()
                    self.assertEqual(units["worker"], prefix + "-worker.service")
                    self.assertEqual(units["scheduler"], prefix + "-scheduler.service")

    def test_mode_exec_never_uses_primary_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(workers, "DATA", Path(tmp)):
                executable = Path("/venv/bin/python")
                for mode, leaf in (("engine2", "lum2"), ("shorts", "lum3")):
                    with self.subTest(mode=mode):
                        command = workers.mode_exec(
                            f"lum-{mode}-web", executable, "-m uvicorn app:app"
                        )
                        self.assertIn(f"LUM_ENGINE_INSTANCE={mode}", command)
                        self.assertIn(f"/local-upload-manager/{leaf}", command)
                        self.assertIn(f"LUM_BASE_PATH=/local-upload-manager/modes/{mode}", command)
                        self.assertIn(f"YOUTUBE_UPLOAD_MANAGER_DATA_DIR={tmp}/local-upload-manager/{leaf}/core", command)
                        self.assertNotIn(f"{tmp}/local-upload-manager/lum1/", command)
                self.assertEqual(
                    workers.mode_exec("lum-worker", executable, "-m app"),
                    "/venv/bin/python -m app",
                )

    def test_new_mode_roots_provision_without_existing_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(workers, "DATA", Path(tmp)), \
             patch("production.workers.os.chown"):
            workers.prepare_mode_roots()
            workers.prepare_mode_roots()
            for mode, leaf in (("engine2", "lum2"), ("shorts", "lum3")):
                p = Path(tmp) / "local-upload-manager" / leaf
                self.assertTrue(p.is_dir())
                self.assertEqual((p / workers.MODE_MARKER).read_text().strip(), mode)
                self.assertEqual(sorted(x.name for x in p.iterdir()), [workers.MODE_MARKER])
            self.assertFalse((Path(tmp) / "local-upload-manager" / "lum1").exists())

    def test_refuse_unowned_preexisting_mode_data(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(workers, "DATA", Path(tmp)), \
             patch("production.workers.os.chown"):
            prior = Path(tmp) / "local-upload-manager" / "lum2"
            prior.mkdir(parents=True)
            (prior / "uploader.db").write_text("OWNED_PREVIOUSLY")
            with self.assertRaisesRegex(RuntimeError, "unowned mode data"):
                workers.prepare_mode_roots()
            self.assertEqual((prior / "uploader.db").read_text(), "OWNED_PREVIOUSLY")

    def test_systemd_eventual_readiness_is_accepted(self):
        from subprocess import CompletedProcess
        results = iter([
            CompletedProcess([], 3, stdout="activating\n"),
            CompletedProcess([], 0, stdout="active\n"),
        ])
        with patch("production.workers.subprocess.run", side_effect=lambda *a, **k: next(results)) as execute, \
             patch("production.workers.time.sleep") as pause:
            workers.wait_for_active_units(["vps1-production-lum-engine2-web.service"], attempts=3)
        self.assertEqual(execute.call_count, 2)
        pause.assert_called_once()

    def test_systemd_failure_names_exact_unit(self):
        from subprocess import CompletedProcess
        def respond(cmd, **_kwargs):
            if cmd[1] == "show":
                return CompletedProcess([], 0, stdout="ActiveState=failed\nExecMainStatus=1\n")
            return CompletedProcess([], 3, stdout="failed\n")
        with patch("production.workers.subprocess.run", side_effect=respond), \
             patch("production.workers.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "lum-shorts-worker.service"):
                workers.wait_for_active_units(
                    ["vps1-production-lum-shorts-worker.service"],
                    attempts=2, interval=0,
                )

    def test_installer_must_verify_real_embedded_html_after_health(self):
        # Guards against regressions where green loopback health masks HTTP 500
        # in the independent app; a health-only installer may not pass release.
        source = (Path(__file__).resolve().parents[1] / "production/workers.py").read_text()
        self.assertIn('app?embedded=1&tab=auto_engine', source)
        self.assertIn('embedded_html="PASS"', source)
        self.assertIn('X-App-Access": "lum"', source)
        self.assertIn('b"Auto Engine" not in page', source)
        self.assertIn('Independent {mode} embedded HTML failed', source)

    def test_worker_unit_scope_does_not_write_parent_apps(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(workers, "DATA", Path(tmp)):
            text = workers.unit(
                "local-upload-manager", "a" * 40, "lum-engine2-web",
                "part01", "-m uvicorn app:app --port 8016",
            )
            self.assertIn("ExecStart=/usr/bin/env", text)
            self.assertIn("LUM_ENGINE_INSTANCE=engine2", text)
            self.assertIn("ReadWritePaths=", text)
            self.assertNotIn("/var/lib/fresh-website/auto-caption-studio", text)


if __name__ == "__main__":
    unittest.main()

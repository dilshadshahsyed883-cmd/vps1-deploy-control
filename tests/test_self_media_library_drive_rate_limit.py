"""SML Drive 403 quota backpressure tests; no network and no owner media."""
from __future__ import annotations

import ast
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps/self-media-library/part01"

class DriveQuota(unittest.TestCase):
    def _rclone_functions(self, responses, clock):
        tree = ast.parse((APP / "app.py").read_text())
        funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"run_rclone", "is_drive_rate_limit_error"}]
        self.assertEqual(len(funcs), 2)
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if responses:
                return responses.pop(0)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        env = {
            "shutil": SimpleNamespace(which=lambda _:"/usr/bin/rclone"),
            "runtime_rclone_config": lambda: Path("/tmp/test-sml-rclone.conf"),
            "os": SimpleNamespace(getenv=lambda _key, default="":default),
            "subprocess": SimpleNamespace(run=mock_run, PIPE=subprocess.PIPE),
            "time": SimpleNamespace(monotonic=lambda:clock[0]),
            "RCLONE_IO_LOCK": threading.Lock(),
            "RCLONE_THROTTLE_UNTIL": 0.0,
            "RCLONE_QUOTA_MESSAGE": "Drive rate limit: retry existing clips, originals retained",
        }
        future=ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *funcs], type_ignores=[])),
                     str(APP / "app.py"), "exec"), env)
        return env, calls

    def test_quota_fast_fails_during_cooldown_then_recovers(self):
        quota = ("Failed to create file system for destination gdrive:Self Media Library/metadata: "
                 "googleapi: Error 403: Quota exceeded for quota metric 'Queries' "
                 "and limit 'Requests per minute'; reason: RATE_LIMIT_EXCEEDED")
        responses = [
            subprocess.CompletedProcess(["rclone"], 1, "", quota),
            subprocess.CompletedProcess(["rclone"], 0, "[]", ""),
        ]
        clock = [1000.0]
        scope, calls = self._rclone_functions(responses, clock)
        with self.assertRaisesRegex(RuntimeError, "Drive rate limit"):
            scope["run_rclone"](["mkdir", "gdrive:Self Media Library/metadata"])
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(RuntimeError, "Drive rate limit"):
            scope["run_rclone"](["copyto", "a", "gdrive:test"])
        self.assertEqual(len(calls), 1, "A quota cooldown must not issue more API requests")
        clock[0] += 121.0
        result = scope["run_rclone"](["lsjson", "gdrive:test"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][1:5], ["--tpslimit", "2", "--tpslimit-burst", "1"])

    def test_permission_403_is_not_classified_as_query_rate_limit(self):
        permission = "googleapi: Error 403: The user does not have sufficient permissions"
        scope, calls = self._rclone_functions([subprocess.CompletedProcess(["rclone"], 1, "", permission)], [1000.0])
        self.assertFalse(scope["is_drive_rate_limit_error"](permission))
        with self.assertRaisesRegex(RuntimeError, "sufficient permissions"):
            scope["run_rclone"](["mkdir", "gdrive:x"])
        self.assertEqual(scope["RCLONE_THROTTLE_UNTIL"], 0.0)
        self.assertEqual(len(calls), 1)

    def test_error_never_exposes_bulk_raw_quota_trace_or_unsafe_cleanup(self):
        app = (APP / "app.py").read_text()
        js = (APP / "static/app.js").read_text()
        self.assertIn("not src[\"drive_source_path\"] or not src[\"drive_source_verified_at\"]", app)
        self.assertIn("if is_drive_rate_limit_error(msg):", app)
        self.assertIn("failures.slice(0,2)", js)
        self.assertIn("!!s.drive_source_verified_at", js)
        self.assertIn("Google Drive API rate-limited (403)", js)

    def test_release_waits_for_active_processing_without_stopping_jobs(self):
        wf=(ROOT / ".github/workflows/vps1-self-media-v2-release.yml").read_text()
        self.assertIn("SML_ACTIVE_PROCESSING=", wf)
        self.assertIn("SML_IDLE_PREFLIGHT=BUSY_TIMEOUT", wf)
        self.assertIn("SML_MEDIA_ACTIVE_WAIT", wf)
        self.assertIn("sleep 30", wf)
        self.assertIn("raise SystemExit(20)", wf)
        self.assertIn("if [[ \"$idle_status\" -ne 20 ]]", wf)
        self.assertLess(wf.index("READ_ONLY_IDLE_PREFLIGHT"),wf.index("SML_IMMUTABLE_APP_ONLY_INSTALL"))
        self.assertLess(wf.index("SML_IDLE_PREFLIGHT=BUSY_TIMEOUT"),wf.index("SML_IMMUTABLE_APP_ONLY_INSTALL"))

if __name__ == "__main__":
    unittest.main()

"""Auto Script owner-authorized activation tests; no VPS, Google or scheduler side effects."""
import ast
import importlib.util
import os
from pathlib import Path
from unittest import TestCase, mock

from production.scene_candidate_release import _auto_script_env_patch
from rebuild.stage_auto_script import validated_files

ROOT = Path(__file__).resolve().parents[1]
SCENE_STATE = "/var/lib/fresh-website/scene-builder"


class AutoScriptActivationTest(TestCase):
    def test_manifest_and_source_are_unchanged(self):
        files = validated_files()
        self.assertEqual(set(files), {
            "auto_script.py", "templates/auto_script.html",
            "static/auto_script.js", "static/auto_script.css"})

    def test_python_code_compiles(self):
        for path in (
            "apps/scene-builder/part01/app.py",
            "apps/scene-builder/part01/runtime_guard.py",
            "production/scene_candidate_release.py",
            "apps/auto-script/auto_script.py",
        ):
            with self.subTest(path=path):
                ast.parse((ROOT / path).read_text(), filename=path)

    def test_env_patch_is_precise_and_idempotent(self):
        start = "WEB_HOST=127.0.0.1\nFRESH_AUTO_SCRIPT_READY=0\n"
        new = _auto_script_env_patch(start)
        self.assertIn("FRESH_AUTO_SCRIPT_READY=1\n", new)
        self.assertIn("AUTO_SCRIPT_DB_PATH=" + SCENE_STATE + "/auto-script/auto_script.sqlite3\n", new)
        self.assertEqual(_auto_script_env_patch(new), new)
        self.assertIn("WEB_HOST=127.0.0.1\n", new)
        self.assertEqual(new.count("FRESH_AUTO_SCRIPT_READY="), 1)

    def test_conflicting_database_is_rejected(self):
        with self.assertRaises(RuntimeError):
            _auto_script_env_patch("AUTO_SCRIPT_DB_PATH=/var/tmp/unrelated.sqlite3\n")
        with self.assertRaises(RuntimeError):
            _auto_script_env_patch("FRESH_AUTO_SCRIPT_READY=0\nFRESH_AUTO_SCRIPT_READY=1\n")

    def test_candidate_fails_closed_without_owned_database(self):
        path = ROOT / "apps/scene-builder/part01/runtime_guard.py"
        spec = importlib.util.spec_from_file_location("runtime_guard_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        params = {
            "VPS1_RUNTIME_CANDIDATE": "1",
            "FRESH_WEBSITE_INSTALL": "1",
            "FRESH_AUTO_SCRIPT_READY": "1",
            "VPS1_SCENE_PRECUTOVER_EDITING": "1",
            "AUTO_SCRIPT_DB_PATH": SCENE_STATE + "/auto-script/auto_script.sqlite3",
            "WEB_HOST": "127.0.0.1",
            "WEB_PORT": "18080",
            "ATOMIC_FLOW_ENABLED": "0",
            "WEB_ACCESS_KEY": "a" * 40,
            "SESSION_SECRET": "b" * 40,
            "SCENE_RUNTIME_PROBE_TOKEN": "c" * 40,
        }
        for key in ("WEB_DATA_DIR", "SCENE_PERMANENT_ROOT", "SCRIPT_WORK_DATA_DIR",
                    "MASTER_CONTROLLER_DB_PATH", "ATOMIC_FLOW_DB_PATH",
                    "AUTOFLOW_FLOW_RESULT_ROOT", "SCENE_CHANNELS_FILE"):
            params[key] = SCENE_STATE + "/" + key.lower()
        with mock.patch.dict(os.environ, params, clear=True):
            with mock.patch("os.geteuid", return_value=1000):
                module.validate_candidate_environment()
                os.environ["AUTO_SCRIPT_DB_PATH"] = "/tmp/foreign.db"
                with self.assertRaises(RuntimeError):
                    module.validate_candidate_environment()

    def test_scheduler_is_not_activated_by_source_commit(self):
        code = (ROOT / "apps/scene-builder/part01/app.py").read_text()
        self.assertIn('os.getenv("FRESH_AUTO_SCRIPT_READY", "0") == "1"', code)
        installer = (ROOT / "production/scene_candidate_release.py").read_text()
        self.assertIn('[vps1-auto-script-online]', installer)
        self.assertIn("integrations[\"auto_script\"] = _verify_auto_script_online()", installer)


if __name__ == "__main__":
    import unittest
    unittest.main()

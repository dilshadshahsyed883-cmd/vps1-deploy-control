"""Scene candidate smoke: login, exact admin user creation and preset writes.

GitHub CI only. Never touches VPS_1. Uses isolated temporary CI state
beneath the guarded candidate root; unrelated writes remain denied.
"""
import json
import os
import re
from pathlib import Path
import secrets
import sys
import unittest

# Existing repository-wide release builder treats source __pycache__ as drift.
# Prevent this module from creating bytecode in apps/ during unittest discovery.
sys.dont_write_bytecode = True

ROOT = Path("/var/lib/fresh-website/scene-builder")
SOURCE = Path(__file__).resolve().parents[2] / "apps/scene-builder/part01"
sys.path.insert(0, str(SOURCE))
from precutover_policy import mutation_allowed


class WritePolicy(unittest.TestCase):
    def test_default_read_only_and_narrow_allowlist(self):
        allowed = [
            ("POST", "/login"),
            ("POST", "/logout"),
            ("POST", "/settings"),
            ("POST", "/api/internal-audio/settings"),
            ("POST", "/api/internal-audio/studio/process"),
            ("POST", "/api/presets"),
            ("POST", "/api/presets/x/duplicate"),
            ("POST", "/api/presets/x/rename"),
        ]
        for method, path in allowed:
            self.assertFalse(mutation_allowed(method, path, False))
            self.assertTrue(mutation_allowed(method, path, True))
        # Owner-authorized administrator account editing works in candidate
        # mode even when the unrelated general preset-editing gate is OFF.
        for editing in (False, True):
            self.assertTrue(mutation_allowed("POST", "/user-management", editing))
        # Experiment Studio is an independently authorized, CSRF-protected app.
        sample = "/api/experiment-browser/profiles/" + "a" * 24
        for editing in (False, True):
            self.assertTrue(mutation_allowed("POST", "/api/experiment-browser/profiles", editing))
            self.assertTrue(mutation_allowed("DELETE", sample, editing))
            for action in ("start", "stop", "navigate", "action"):
                self.assertTrue(mutation_allowed("POST", sample + "/" + action, editing))
        denied = [
            ("POST", "/api/experiment-browser/profiles/" + "a" * 23 + "/start"),
            ("PUT", "/api/experiment-browser/profiles/" + "a" * 24 + "/start"),
            ("DELETE", "/api/experiment-browser/profiles"),
            ("POST", "/api/experiment-browser/profiles/" + "a" * 24 + "/delete"),
            ("POST", "/api/experiment-browser/profiles/" + "a" * 24 + "/start/extra"),
            ("POST", "/queue/submit"),
            ("POST", "/queue/123/start"),
            ("POST", "/api/admin/storage/cleanup"),
            ("POST", "/api/admin/storage/services/foo/start"),
            ("POST", "/api/master-controller/chatgpt/projects"),
            ("POST", "/user-management/"),
            ("PUT", "/user-management"),
            ("DELETE", "/user-management"),
            ("POST", "/user-management/reset"),
            ("PUT", "/settings"),
            ("POST", "/api/internal-audio/settings/extra"),
            ("POST", "/api/scene-builder/channels/add"),
            ("POST", "/api/presets/evil/rename/another"),
            ("DELETE", "/api/presets/owner"),
            ("PUT", "/api/presets"),
            ("POST", "/api/presets/x/audio-engine"),
        ]
        for method, path in denied:
            self.assertFalse(mutation_allowed(method, path, True), (method, path))
            self.assertFalse(mutation_allowed(method, path, False), (method, path))


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "production pilot must run nonroot")
class RealProductionPreparationSmoke(unittest.TestCase):
    def test_real_https_cookie_login_presets_and_blocked_workers(self):
        if not ROOT.is_dir() or not os.access(ROOT, os.W_OK):
            self.skipTest("Requires dedicated writable CI-only guard root")
        access = secrets.token_urlsafe(40)
        probe = secrets.token_urlsafe(40)
        env = {
            "VPS1_RUNTIME_CANDIDATE": "1",
            "VPS1_SCENE_PRECUTOVER_EDITING": "1",
            "FRESH_WEBSITE_INSTALL": "1",
            "FRESH_AUTO_SCRIPT_READY": "0",
            "ATOMIC_FLOW_ENABLED": "0",
            "WEB_HOST": "127.0.0.1",
            "WEB_PORT": "18080",
            "WEB_USERNAME": "team",
            "WEB_ACCESS_KEY": access,
            "SESSION_SECRET": secrets.token_urlsafe(40),
            "SCENE_RUNTIME_PROBE_TOKEN": probe,
            "WEB_DATA_DIR": str(ROOT / "web"),
            "SCENE_PERMANENT_ROOT": str(ROOT / "permanent"),
            "SCRIPT_WORK_DATA_DIR": str(ROOT / "script-work"),
            "MASTER_CONTROLLER_DB_PATH": str(ROOT / "master-controller.sqlite3"),
            "ATOMIC_FLOW_DB_PATH": str(ROOT / "atomic-flow.sqlite3"),
            "AUTOFLOW_FLOW_RESULT_ROOT": str(ROOT / "flow-results"),
            "SCENE_CHANNELS_FILE": str(ROOT / "channels.json"),
            "WORKER_URL": "http://127.0.0.1:1",
        }
        from unittest.mock import patch
        with patch.dict(os.environ, env):
            # Installed pinned FastAPI + httpx only; import under guarded env.
            from fastapi.testclient import TestClient
            import app
            self.assertTrue(app.RUNTIME_CANDIDATE)
            self.assertTrue(app.SCENE_PRECUTOVER_EDITING)
            with TestClient(app.app, base_url="https://localhost", follow_redirects=False) as client:
                no_auth = client.post("/api/presets", json={"name": "unauthorized"})
                self.assertEqual(no_auth.status_code, 303)
                login = client.post("/login", data={"username": "team", "secret_key": access})
                self.assertEqual(login.status_code, 303)
                self.assertIn("session=", login.headers.get("set-cookie", ""))
                self.assertIn("secure", login.headers.get("set-cookie", "").lower())
                create = client.post("/api/presets", json={"name": "Precutover Demo Preset"})
                self.assertEqual(create.status_code, 200, create.text[:300])
                preset = create.json()["preset_id"]
                self.assertEqual(client.post("/api/presets/" + preset + "/rename",
                                json={"name": "Pilot Preset"}).status_code, 200)
                # A real administrator can create an account through the same
                # route as the public form, with the session-bound CSRF value.
                page = client.get("/user-management")
                self.assertEqual(page.status_code, 200)
                token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
                self.assertIsNotNone(token, "user management CSRF missing")
                denied_create = client.post("/user-management", data={
                    "action": "create", "username": "candidate_probe",
                    "secret_key": "candidate-secret-123", "role": "non_admin",
                    "csrf_token": "bad-token",
                })
                self.assertEqual(denied_create.status_code, 403)
                self.assertIn("Invalid user-management action token", denied_create.text)
                created = client.post("/user-management", data={
                    "action": "create", "username": "candidate_probe",
                    "secret_key": "candidate-secret-123", "role": "non_admin",
                    "csrf_token": token.group(1),
                })
                self.assertEqual(created.status_code, 303, created.text[:300])
                with app.db() as database:
                    row = database.execute(
                        "SELECT is_admin,is_active FROM users WHERE username=?",
                        ("candidate_probe",)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual((row["is_admin"], row["is_active"]), (0, 1))
                for endpoint in (
                    "/queue/submit", "/api/admin/storage/cleanup",
                    "/api/admin/storage/services/storage/start", "/api/master-controller/projects",
                    "/user-management/",
                ):
                    res = client.post(endpoint, json={})
                    self.assertEqual(res.status_code, 403, (endpoint, res.status_code))
                self.assertEqual(client.get("/internal/runtime-ready").status_code, 403)
                ready = client.get("/internal/runtime-ready", headers={"X-Runtime-Probe-Token": probe})
                self.assertEqual(ready.status_code, 200)
                self.assertEqual(ready.json()["side_effect_workers"], "disabled")
                self.assertTrue((ROOT / "web" / "web.sqlite3").is_file())
                logout = client.post("/logout")
                self.assertEqual(logout.status_code, 303)
                self.assertEqual(client.post("/api/presets", json={"name": "after"}).status_code, 303)


if __name__ == "__main__":
    unittest.main()

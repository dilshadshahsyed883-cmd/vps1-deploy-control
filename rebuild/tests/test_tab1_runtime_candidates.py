"""Offline Tab 1 candidate checks; no SSH, VPS or production modification."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
STORAGE = ROOT / "apps/vps-storage-manager/part01/storage_manager.py"
SCENE = ROOT / "apps/scene-builder/part01"


class RuntimeCandidates(unittest.TestCase):
    def test_source_syntax(self):
        for path in (STORAGE, SCENE / "app.py", SCENE / "runtime_guard.py",
                     SCENE / "script_extractor_service.py",
                     SCENE / "script_processor_service.py",
                     SCENE / "autoflow_google_sync.py"):
            with self.subTest(path=str(path)):
                compile(path.read_text(), str(path), "exec")

    def test_nonroot_fail_closed_unit_templates(self):
        for unit in (ROOT / "apps/vps-storage-manager/runtime/vps1-storage-candidate.service",
                     ROOT / "apps/scene-builder/runtime/vps1-scene-candidate.service"):
            text = unit.read_text()
            for required in ("NoNewPrivileges=yes", "ProtectSystem=strict", "CapabilityBoundingSet=", "127.0.0.1"):
                self.assertIn(required, text)
            self.assertNotIn("User=root", text)
        self.assertIn("FRESH_WEBSITE_SMOKE_TEST=1",
                      (ROOT / "apps/vps-storage-manager/runtime/vps1-storage-candidate.service").read_text())
        self.assertIn("FRESH_AUTO_SCRIPT_READY=0",
                      (ROOT / "apps/scene-builder/runtime/vps1-scene-candidate.service").read_text())
        groups = json.loads((ROOT / "rebuild/groups.json").read_text())
        for flag in ("production_deploy_enabled", "fresh_install_enabled",
                     "automatic_database_migrations", "legacy_database_import", "legacy_queue_import"):
            self.assertFalse(groups[flag])
        self.assertEqual(groups["offline_applications"], ["auto-script"])

    def test_eight_remaining_service_entrypoints_exist_only(self):
        profiles = json.loads((ROOT / "rebuild/install-profiles.json").read_text())["groups"]
        contracts = json.loads((ROOT / "rebuild/deploy-contracts.json").read_text())["apps"]
        others = set(profiles) - {"auto-script", "scene-builder", "vps-storage-manager"}
        self.assertEqual(len(others), 8)
        for name in sorted(others):
            with self.subTest(name=name):
                self.assertTrue((ROOT / "apps" / name).is_dir())
                for service in profiles[name].get("services", []):
                    part = ROOT / "apps" / name / service.get("part", "part01")
                    entry = service.get("entry") or service["module"].split(":")[0].replace(".", "/") + ".py"
                    self.assertTrue((part / entry).is_file(), (name, entry))
                self.assertEqual(contracts[name]["workers"], [])
                self.assertFalse(contracts[name]["runtime_verified"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "requires non-root runner")
    def test_real_storage_start_auth_and_forbidden_mutation(self):
        with tempfile.TemporaryDirectory(prefix="tab1-storage-") as temp:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            state = Path(temp) / "state"
            token = secrets.token_urlsafe(32)
            env = dict(os.environ, VPS1_RUNTIME_CANDIDATE="1",
                       FRESH_WEBSITE_SMOKE_TEST="1", STORAGE_MANAGER_HOST="127.0.0.1",
                       STORAGE_MANAGER_PORT=str(port), STORAGE_MANAGER_TOKEN=token,
                       STORAGE_MANAGER_STATE_ROOT=str(state))
            proc = subprocess.Popen([sys.executable, str(STORAGE), "serve"],
                                    env=env, cwd=temp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 9
                good = False
                while time.monotonic() < deadline:
                    self.assertIsNone(proc.poll(), "candidate exited before serving")
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as r:
                            good = json.load(r).get("ok") is True
                        if good:
                            break
                    except (OSError, TimeoutError):
                        time.sleep(0.1)
                self.assertTrue(good, "candidate not healthy within bound")
                def status(endpoint, authorized=False, method="GET"):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}{endpoint}", method=method,
                        headers={"X-Storage-Token": token} if authorized else {})
                    try:
                        with urllib.request.urlopen(req, timeout=2) as r:
                            return r.status
                    except urllib.error.HTTPError as e:
                        return e.code
                self.assertEqual(status("/v1/policy"), 403)
                self.assertEqual(status("/v1/policy", True), 200)
                self.assertEqual(status("/v1/cleanup", True, "POST"), 403)
                self.assertTrue((state / "storage-manager.sqlite3").is_file())
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)

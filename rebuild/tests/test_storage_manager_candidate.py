"""Offline first-app regression tests. No VPS credentials or deployment."""
import ast
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

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/"apps/vps-storage-manager/part01/storage_manager.py"
UNIT=ROOT/"apps/vps-storage-manager/candidate/vps1-storage-candidate.service"
INSTALLER=ROOT/"rebuild/pilots/install_storage_manager_candidate.py"

class StoragePilot(unittest.TestCase):
    def test_source_and_installer_syntax(self):
        for p in (SOURCE, INSTALLER):
            with self.subTest(path=str(p)):
                ast.parse(p.read_text())

    def test_unit_hardening_and_contract(self):
        unit=UNIT.read_text()
        for required in ("User=vps1storage","NoNewPrivileges=yes","ProtectSystem=strict",
            "Environment=VPS1_RUNTIME_CANDIDATE=1",
            "Environment=FRESH_WEBSITE_SMOKE_TEST=1",
            "Environment=STORAGE_MANAGER_HOST=127.0.0.1",
            "Environment=STORAGE_MANAGER_PORT=8149",
            "ReadWritePaths=/var/lib/fresh-website/vps-storage-manager"):
            self.assertIn(required,unit)
        groups=json.loads((ROOT/"rebuild/groups.json").read_text())
        self.assertFalse(groups["production_deploy_enabled"])
        self.assertFalse(groups["fresh_install_enabled"])
        self.assertEqual(groups["offline_applications"], ["auto-script"])

    def test_github_installer_no_remote_execution(self):
        text=INSTALLER.read_text()
        self.assertIn("raw.githubusercontent.com",text)
        self.assertIn("git_blob_digest",text)
        self.assertIn("ROLLBACK=APP_ONLY",text)
        self.assertNotIn("github-actions-runner",text)
        self.assertNotIn("ssh ubuntu@",text)

    @unittest.skipIf(hasattr(os,"geteuid") and os.geteuid()==0, "root runner cannot exercise nonroot guard")
    def test_real_guarded_http_auth_and_mutation_deny(self):
        with tempfile.TemporaryDirectory(prefix="storage-pilot-ci-") as td:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1",0))
                port=sock.getsockname()[1]
            token=secrets.token_urlsafe(32)
            state=Path(td)/"state"
            env=dict(os.environ,STORAGE_MANAGER_TOKEN=token,
                VPS1_RUNTIME_CANDIDATE="1",FRESH_WEBSITE_SMOKE_TEST="1",
                STORAGE_MANAGER_HOST="127.0.0.1",STORAGE_MANAGER_PORT=str(port),
                STORAGE_MANAGER_STATE_ROOT=str(state))
            process=subprocess.Popen([sys.executable,str(SOURCE),"serve"],
                env=env,cwd=td,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            def req(path,method="GET",auth=False):
                request=urllib.request.Request(
                    "http://127.0.0.1:"+str(port)+path,method=method,
                    headers=({"X-Storage-Token":token} if auth else {}))
                try:
                    with urllib.request.urlopen(request,timeout=1.0) as reply:
                        return reply.status
                except urllib.error.HTTPError as e:
                    return e.code
            try:
                for _ in range(40):
                    self.assertIsNone(process.poll(),"pilot exited unexpectedly")
                    try:
                        if req("/health")==200: break
                    except OSError:
                        time.sleep(.1)
                else: self.fail("HTTP health did not start")
                self.assertEqual(req("/v1/policy"),403)
                self.assertEqual(req("/v1/policy",auth=True),200)
                self.assertEqual(req("/v1/cleanup",method="POST",auth=True),403)
                self.assertEqual(req("/v1/services/start",method="POST",auth=True),403)
                self.assertTrue((state/"storage-manager.sqlite3").is_file())
            finally:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

if __name__=="__main__":
    unittest.main()

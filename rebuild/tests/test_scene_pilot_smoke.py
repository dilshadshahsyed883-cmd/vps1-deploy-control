"""CI-only real Scene Builder shadow startup and security smoke, no live VPS."""
from __future__ import annotations
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

SCENE=Path(__file__).resolve().parents[2]/"apps/scene-builder/part01"
ROOT=Path("/var/lib/fresh-website/scene-builder")

class ScenePilotSmoke(unittest.TestCase):
    @unittest.skipIf(hasattr(os,"geteuid") and os.geteuid()==0, "shadow runtime must not run root")
    def test_real_http_runtime(self):
        if not ROOT.is_dir() or not os.access(ROOT,os.W_OK):
            self.skipTest("CI creates isolated writable /var/lib state before smoke")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            # Guard requires literal 18080, not an arbitrary ephemeral port.
        if subprocess.run(["ss","-lntH"],text=True,capture_output=True).returncode == 0:
            listeners=subprocess.run(["ss","-lntH"],check=True,text=True,capture_output=True).stdout
            self.assertFalse(any(s.split()[3].rsplit(":",1)[-1]=="18080" for s in listeners.splitlines()))
        access=secrets.token_urlsafe(32)
        session=secrets.token_urlsafe(32)
        probe=secrets.token_urlsafe(32)
        env=dict(os.environ,
            VPS1_RUNTIME_CANDIDATE="1",FRESH_WEBSITE_INSTALL="1",
            FRESH_AUTO_SCRIPT_READY="0",ATOMIC_FLOW_ENABLED="0",
            WEB_HOST="127.0.0.1",WEB_PORT="18080",
            WEB_ACCESS_KEY=access,SESSION_SECRET=session,
            SCENE_RUNTIME_PROBE_TOKEN=probe,
            WEB_DATA_DIR=str(ROOT/"web"),
            SCENE_PERMANENT_ROOT=str(ROOT/"permanent"),
            SCRIPT_WORK_DATA_DIR=str(ROOT/"script-work"),
            MASTER_CONTROLLER_DB_PATH=str(ROOT/"master-controller.sqlite3"),
            ATOMIC_FLOW_DB_PATH=str(ROOT/"atomic-flow.sqlite3"),
            AUTOFLOW_FLOW_RESULT_ROOT=str(ROOT/"flow-results"),
            SCENE_CHANNELS_FILE=str(ROOT/"channels.json"),
            WORKER_URL="http://127.0.0.1:1",
            SCRIPT_WORK_GOOGLE_FOLDER_ID="")
        def query(path,method="GET",auth=False):
            req=urllib.request.Request("http://127.0.0.1:18080"+path,method=method,
                headers=({"X-Runtime-Probe-Token":probe} if auth else {}))
            try:
                with urllib.request.urlopen(req,timeout=1.4) as resp:
                    return resp.status,resp.read(16000)
            except urllib.error.HTTPError as exc:
                return exc.code,exc.read(16000)
        with tempfile.TemporaryFile(mode="w+b") as log:
            proc=subprocess.Popen([sys.executable,str(SCENE/"run.py")],
                cwd=SCENE,env=env,stdout=log,stderr=subprocess.STDOUT)
            try:
                for _ in range(180):
                    if proc.poll() is not None:
                        log.seek(0)
                        self.fail("scene candidate exited: "+log.read()[-3200:].decode(errors="replace"))
                    try:
                        status,data=query("/health")
                        if status==200 and json.loads(data).get("mode")=="shadow-read-only":
                            break
                    except (OSError,ValueError):
                        pass
                    time.sleep(.25)
                else:
                    log.seek(0)
                    self.fail("scene candidate health timed out: "+log.read()[-3200:].decode(errors="replace"))
                self.assertEqual(query("/internal/runtime-ready")[0],403)
                status,body=query("/internal/runtime-ready",auth=True)
                self.assertEqual(status,200)
                self.assertEqual(json.loads(body).get("side_effect_workers"),"disabled")
                self.assertEqual(query("/login",method="POST")[0],403)
                self.assertEqual(query("/api/job",method="POST")[0],403)
                self.assertTrue((ROOT/"web"/"web.sqlite3").exists())
            finally:
                proc.terminate()
                try:proc.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=4)
if __name__=="__main__":unittest.main()

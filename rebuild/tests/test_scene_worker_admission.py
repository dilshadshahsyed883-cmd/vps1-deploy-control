"""Real Scene worker API startup; admission-only and empty-queue auth smoke.

Import ACTUAL api_server module, initialize NEW isolated SQLite DB, call HTTP
routes through FastAPI TestClient. No existing queue, worker or live VPS touched.
"""
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
PART2 = Path(__file__).resolve().parents[2] / "apps/scene-builder/part02"

class SceneWorkerAdmission(unittest.TestCase):
    def test_real_api_startup_auth_empty_queue_no_mutations(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory(prefix="scene-worker-admission-ci-") as temp:
            root=Path(temp)/"scene-worker-admission"
            root.mkdir()
            token=secrets.token_urlsafe(48)
            env={
                "SCENE_WORKER_ADMISSION_ONLY":"1",
                "SCENE_WORKER_DATA":str(root),
                "SCENE_WORKER_TOKEN":token,
                "DRIVE_UPLOAD_ENABLED":"0",
                "AUTO_CLEAN_MEDIA_AFTER_RENDER":"0",
                "COMPRESSOR_ENABLED":"0",
                "SCENE_INTERNAL_AUDIO_ENGINE_ROOT":str(root/"internal_audio"),
                "MIN_FREE_SPACE_GB":"1"
            }
            pkg=types.ModuleType("vps_engine")
            pkg.__path__=[str(PART2)]
            with patch.dict(os.environ,env),patch.dict(sys.modules,{"vps_engine":pkg}):
                sys.path.insert(0,str(PART2))
                try:
                    from vps_engine import api_server
                    self.assertTrue(api_server.WORKER_ADMISSION_ONLY)
                    self.assertEqual(api_server.PIPELINE_WORKERS,3)
                    with TestClient(api_server.app,base_url="http://127.0.0.1") as client:
                        health=client.get("/health")
                        self.assertEqual(health.status_code,200)
                        state=health.json()
                        self.assertTrue(state.get("ok"))
                        self.assertEqual(state.get("queue_counts"),{})
                        self.assertFalse(state.get("drive_upload_enabled"))
                        self.assertEqual(client.get("/jobs").status_code,401)
                        self.assertEqual(client.get("/jobs",headers={"x-scene-worker-token":"wrong"}).status_code,401)
                        valid=client.get("/jobs",headers={"x-scene-worker-token":token})
                        self.assertEqual(valid.status_code,200)
                        self.assertEqual(valid.json().get("jobs"),[])
                        for method,path in [
                            ("POST","/jobs"),("POST","/queue/mode"),
                            ("POST","/pipeline/resume"),("POST","/jobs/fake/clean-now"),
                            ("DELETE","/jobs/fake"),("GET","/docs"),
                            ("GET","/openapi.json"),("GET","/media/private")
                        ]:
                            res=client.request(method,path)
                            self.assertEqual(res.status_code,403,(method,path,res.status_code))
                        self.assertFalse(any(
                            thread.name.startswith("project-pipeline-") or thread.name=="retention-sweeper"
                            for thread in threading.enumerate()))
                    db=root/"queue.sqlite3"
                    self.assertTrue(db.is_file())
                    import sqlite3
                    with sqlite3.connect(f"file:{db}?mode=ro",uri=True) as conn:
                        self.assertEqual(conn.execute("select count(*) from jobs").fetchone()[0],0)
                    print("REAL_WORKER_API=PASS AUTH_ALLOW_DENY=PASS EMPTY_QUEUE=PASS THREADS=OFF",flush=True)
                finally:
                    sys.path.remove(str(PART2))

if __name__=="__main__":
    unittest.main()

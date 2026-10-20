"""Real FastAPI, persistent SQLite and download tests; no live synthesis or Drive."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

APP_ROOT = Path(__file__).resolve().parents[1] / "apps/voice-studio/part01"


def load(name):
    spec = importlib.util.spec_from_file_location("tts_contract_" + name, APP_ROOT / (name + "_api.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RealQueueRoutesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        os.environ["KOKORO_STUDIO_ROOT"] = str(Path(self.temp.name) / "kokoro")
        os.environ["EDGE_STUDIO_ROOT"] = str(Path(self.temp.name) / "edge")
        if str(APP_ROOT) not in sys.path:
            sys.path.insert(0,str(APP_ROOT))
        self.ko = load("kokoro")
        self.ed = load("edge")
        self.ko.init_storage()
        self.ed.init_storage()
        app = FastAPI()
        app.include_router(self.ko.router)
        app.include_router(self.ed.router)
        self.client = TestClient(app)
        self.kid = self.ko.create_job("Hello production tests", "demo.txt", "en-us", "af_heart", 1.0, "test-batch")
        self.eid = self.ed.create_job("Hello production tests", "demo.txt", "en-US-JennyNeural", "en-US", 0, 0, 0, "test-batch")

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def get(self, engine):
        response = self.client.get(f"/tts/api/{engine}/jobs")
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()["jobs"]
        self.assertEqual(len(result),1)
        return result[0]

    def control(self, engine, jid, action):
        return self.client.post(f"/tts/api/{engine}/jobs/{jid}/{action}")

    def test_api_pause_resume_skip_and_engine_isolation(self):
        self.assertIn("pause", self.get("kokoro")["allowed_actions"])
        paused=self.control("kokoro",self.kid,"pause")
        self.assertEqual(paused.status_code,200)
        self.assertEqual(paused.json()["status"],"paused")
        self.assertEqual(self.get("edge")["status"],"queued")
        self.assertEqual(self.control("kokoro",self.kid,"pause").json()["idempotent"],True)
        self.assertEqual(self.control("kokoro",self.kid,"resume").json()["status"],"queued")
        self.assertEqual(self.control("kokoro",self.kid,"skip").json()["status"],"skipped")
        self.assertEqual(self.control("kokoro",self.kid,"resume").status_code,409)

    def test_processing_pause_and_cancel_uses_actual_worker_capabilities(self):
        with self.ko.connection() as c:c.execute("update jobs set status='processing'")
        with self.ed.db() as c:c.execute("update jobs set status='processing'")
        self.assertNotIn("pause",self.get("kokoro")["allowed_actions"])
        self.assertIn("pause",self.get("edge")["allowed_actions"])
        self.assertEqual(self.control("kokoro",self.kid,"pause").status_code,409)
        self.assertEqual(self.control("edge",self.eid,"pause").json()["status"],"pause_requested")
        self.assertEqual(self.control("edge",self.eid,"cancel").json()["status"],"cancel_requested")
        self.assertEqual(self.control("kokoro",self.kid,"cancel").json()["status"],"cancel_requested")

    def test_failed_retry_is_exhausted_and_delivery_retry_distinct(self):
        with self.ed.db() as c:c.execute("update jobs set status='failed',attempt=1")
        r=self.control("edge",self.eid,"retry")
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()["status"],"retry_wait")
        with self.ko.connection() as c:c.execute("update jobs set status='failed',attempt=3")
        self.assertEqual(self.control("kokoro",self.kid,"retry").status_code,409)
        with self.ed.db() as c:c.execute("update jobs set status='completed',delivery_status='retry_pending'")
        self.assertEqual(self.get("edge")["queue_status"],"completed")
        self.assertNotIn("retry",self.get("edge")["allowed_actions"])

    def test_history_hiding_leaves_audio_and_db_row_intact(self):
        import wave
        output=Path(self.temp.name)/"finished.wav"
        with wave.open(str(output),"wb") as w:
            w.setnchannels(1)
            w.setframerate(24000)
            w.setsampwidth(2)
            w.writeframes(b"\x00\x00"*5000)
        with self.ko.connection() as c:
            c.execute("update jobs set status='completed',delivery_status='delivered',wav_file=?",(str(output),))
        url=f"/tts/api/kokoro/audio/{self.kid}"
        before=self.client.get(url)
        self.assertEqual(before.status_code,200)
        self.assertGreater(len(before.content),1000)
        self.assertEqual(self.control("kokoro",self.kid,"delete").status_code,200)
        self.assertEqual(self.get("kokoro")["status"] if False else self.client.get("/tts/api/kokoro/jobs").json()["jobs"],[])
        self.assertTrue(output.exists())
        self.assertEqual(self.client.get(url).status_code,200)
        with self.ko.connection() as c:self.assertEqual(c.execute("select count(*) from jobs").fetchone()[0],1)

    def test_invalid_engine_ids_are_rejected(self):
        self.assertEqual(self.control("edge",self.kid,"cancel").status_code,404)
        self.assertEqual(self.control("kokoro",self.kid,"drive_delete").status_code,404)


if __name__=="__main__":
    unittest.main()

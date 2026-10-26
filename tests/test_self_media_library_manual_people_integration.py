"""End-to-end SML v2 person search, retaining legacy search and approval gates."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"


@unittest.skipUnless(importlib.util.find_spec("fastapi") and
                     importlib.util.find_spec("PIL") and shutil.which("ffmpeg"),
                     "Dedicated SML CI provides FastAPI, Pillow and FFmpeg")
class ManualPersonSearchIntegration(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.temp = tempfile.TemporaryDirectory()
        self.env = {key: os.environ.get(key) for key in
                    ("SML_DATA_ROOT", "SML_AI_WORKER", "SML_EDITOR_API_TOKEN")}
        os.environ.update(SML_DATA_ROOT=self.temp.name, SML_AI_WORKER="0",
                          SML_EDITOR_API_TOKEN="manual-people-ci-token")
        sys.path.insert(0, str(PROJECT))
        sys.modules.pop("app", None)
        import app
        self.module = app
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        sys.modules.pop("app", None)
        sys.path.remove(str(PROJECT))
        for key, original in self.env.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
        self.temp.cleanup()

    def image(self, ident, filename, created):
        """Only media existence matters in search; original bytes must remain untouched."""
        original = b"non-destructive original fixture " + ident.encode()
        (Path(self.temp.name) / "images" / (ident + ".png")).write_bytes(original)
        with self.module.db() as conn:
            conn.execute("""INSERT INTO image_assets
                (id,original_name,stored_name,size_bytes,sha256,width,height,
                 keywords_json,description,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'[]','','approved',?,?)""",
                (ident, filename, ident+".png", len(original), "sha-"+ident, 1280, 720, created, created))
        return original

    def person(self, name, alias):
        response = self.client.post("/api/v1/manual-people",
                                    json={"name": name, "aliases": [alias]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def test_person_search_reaches_older_media_and_excludes_other_people(self):
        old_original = self.image("IMG-OLDERONE", "old-photo.png", "2026-01-01")
        for n in range(505):
            self.image("IMG-NEW%05d" % n, "anonymous-%d.png" % n, "2026-10-26")
        messi = self.person("Lionel Messi", "Messi")
        ronaldo = self.person("Cristiano Ronaldo", "Ronaldo")
        for pid, ident in ((ronaldo, "IMG-OLDERONE"), (messi, "IMG-NEW00000")):
            r = self.client.post("/api/v1/manual-people/"+pid+"/assign",
                                 json={"media_type": "image", "ids": [ident],
                                       "action_tags": ["goal", "playing"]})
            self.assertEqual(r.status_code, 200, r.text)

        # Existing Video Clips list also recognizes virtual manual person tags.
        (Path(self.temp.name) / "clips" / "CLP-OLDER.mp4").write_bytes(b"clip remains untouched")
        with self.module.db() as conn:
            conn.execute("""INSERT INTO clips
                (id,filename,status,created_at,updated_at)
                VALUES ('CLP-OLDER','CLP-OLDER.mp4','approved','2026-01-01','2026-01-01')""")
        r = self.client.post("/api/v1/manual-people/"+ronaldo+"/assign",
                             json={"media_type": "video", "ids": ["CLP-OLDER"],
                                   "action_tags": ["training"]})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get("/api/clips", params={"q": "Ronaldo"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([x["id"] for x in r.json()["items"]], ["CLP-OLDER"])

        headers = {"X-SML-Editor-Token": "manual-people-ci-token"}
        r = self.client.get("/api/v1/media/search",
                            params={"query": "Ronaldo goal", "media_type": "image"},
                            headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([x["id"] for x in r.json()["items"]], ["IMG-OLDERONE"])
        self.assertIn("Cristiano Ronaldo", r.json()["items"][0]["keywords"])

        r = self.client.get("/api/v1/media/search",
                            params={"query": "Messi goal", "media_type": "image"},
                            headers=headers)
        self.assertEqual([x["id"] for x in r.json()["items"]], ["IMG-NEW00000"])

        # Old user search also finds person names without rewriting keywords_json.
        r = self.client.get("/api/v1/images", params={"q": "Ronaldo"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["total"], 1)
        self.assertEqual(r.json()["items"][0]["id"], "IMG-OLDERONE")
        with self.module.db() as conn:
            self.assertEqual(json.loads(conn.execute(
                "SELECT keywords_json FROM image_assets WHERE id='IMG-OLDERONE'").fetchone()[0]), [])
        self.assertEqual((Path(self.temp.name) / "images" / "IMG-OLDERONE.png").read_bytes(), old_original)

        r = self.client.get("/api/v1/media/search",
                            params={"query": "Ronaldo goal", "media_type": "image"})
        self.assertIn(r.status_code, (401, 503))

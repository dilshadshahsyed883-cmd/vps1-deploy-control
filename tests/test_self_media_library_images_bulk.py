"""Image Library parity: accurate totals, bulk review, keywords, safe Trash lifecycle."""
import importlib.util
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"


class ImageBulkUI(unittest.TestCase):
    def test_controls_and_wiring(self):
        html = (APP / "static/index.html").read_text()
        script = (APP / "static/app.js").read_text()
        local = (APP / "static/local_library.js").read_text()
        for field in ("imageBulkBar", "imageSelectedCount", "imageCountSummary",
                      "imageBulkKeyword", "imageBulkTrash", "clearImageSelection",
                      "imageTrashGrid", "loadMoreImages", "imageKeywordModal"):
            self.assertIn('id="' + field + '"', html)
        for needle in ("/api/v1/images/bulk/status", "/api/v1/images/bulk/keywords",
                       "/api/v1/images/bulk/trash", "/api/v1/images/trash",
                       "function updateImageBulk", "function loadImageTrash"):
            self.assertIn(needle, script)
        self.assertIn("imageSelected.add(row.id)", local)
        self.assertIn("ids=[...imageSelected]", local)


@unittest.skipUnless(importlib.util.find_spec("fastapi") and
                     importlib.util.find_spec("PIL") and shutil.which("ffmpeg"),
                     "FastAPI/Pillow/FFmpeg required")
class ImageBulkAPI(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.temp = tempfile.TemporaryDirectory()
        self.before = {k: os.environ.get(k) for k in ("SML_DATA_ROOT", "SML_AI_WORKER",
                                                       "SML_EDITOR_API_TOKEN")}
        os.environ.update(SML_DATA_ROOT=self.temp.name, SML_AI_WORKER="0",
                          SML_EDITOR_API_TOKEN="image-bulk-test")
        sys.path.insert(0, str(APP))
        sys.modules.pop("app", None)
        import app
        self.module = app
        self.client = TestClient(app.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        sys.modules.pop("app", None)
        sys.path.remove(str(APP))
        for k, value in self.before.items():
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value
        self.temp.cleanup()

    def image(self, name, color):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (96, 64), color).save(output, format="PNG")
        response = self.client.post("/api/v1/images/upload",
                                    files={"file": (name, output.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def test_bulk_actions_counts_trash_restore_and_permanent_delete(self):
        ids = [self.image("one.png", "red"), self.image("two.png", "blue"),
               self.image("three.png", "yellow")]
        self.assertEqual(self.client.get("/api/v1/images?limit=2").json()["total"], 3)
        self.assertEqual(self.client.get("/api/v1/images?limit=2").json()["total_images"], 3)
        self.assertEqual(len(self.client.get("/api/v1/images?limit=2&offset=2").json()["items"]), 1)

        # Atomic validation: missing image must not partially update an earlier one.
        result = self.client.post("/api/v1/images/bulk/status",
                                  json={"ids": [ids[0], "missing"], "status": "approved"})
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.client.get("/api/v1/images?status=candidate").json()["total"], 3)
        result = self.client.post("/api/v1/images/bulk/status",
                                  json={"ids": ids[:2], "status": "approved"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["updated"], 2)
        self.assertEqual(self.client.get("/api/v1/images?status=approved").json()["total"], 2)

        path = "/api/v1/images/bulk/keywords"
        self.assertEqual(self.client.post(path, json={"ids": ids[:2],
                                                       "keyword": "Tennis", "action": "add"}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/images?q=tennis").json()["total"], 2)
        self.assertEqual(self.client.post(path, json={"ids": [ids[0]],
                                                       "keyword": "tennis", "action": "remove"}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/images?q=tennis").json()["total"], 1)
        self.assertEqual(self.client.post(path, json={"ids": ids,
                                                       "keyword": "Tennis", "action": "replace"}).status_code, 400)

        # Reserved content cannot be trashed or permanently removed.
        h = {"X-SML-Editor-Token": "image-bulk-test"}
        lease = self.client.post("/api/v1/media/reservations", headers=h,
                                 json={"project_id": "project", "scene_id": "scene",
                                       "media_type": "image", "preferred_ids": [ids[0]]})
        self.assertEqual(lease.status_code, 200, lease.text)
        reservation = lease.json()["items"][0]["reservation_id"]
        self.assertEqual(self.client.post("/api/v1/images/bulk/trash",
                                          json={"ids": ids[:2]}).status_code, 409)
        self.assertEqual(self.client.get("/api/v1/images").json()["total"], 3)
        self.client.post("/api/v1/media/reservations/" + reservation + "/release", headers=h)

        moved = self.client.post("/api/v1/images/bulk/trash", json={"ids": ids[:2]})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["trashed"], 2)
        self.assertEqual(self.client.get("/api/v1/images").json()["total"], 1)
        self.assertEqual(self.client.get("/api/v1/images").json()["total_images"], 1)
        self.assertEqual(self.client.get("/api/v1/images/trash").json()["total"], 2)
        self.assertEqual(self.client.get("/api/v1/images/" + ids[0] + "/content").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/images/" + ids[0] + "/preview").status_code, 200)
        self.assertEqual(self.client.post("/api/v1/images/" + ids[0] + "/restore").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/images").json()["total"], 2)
        self.assertEqual(self.client.post("/api/local/delete",
                                          json={"media_type": "image", "ids": [ids[1]]}).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/images/trash").json()["total"], 0)

    def test_total_is_not_limited_to_first_500(self):
        ident = self.image("seed.png", "green")
        with self.module.db() as conn:
            row = conn.execute("SELECT * FROM image_assets WHERE id=?", (ident,)).fetchone()
            for n in range(501):
                conn.execute("""INSERT INTO image_assets
                    (id,original_name,stored_name,thumb_name,size_bytes,sha256,width,height,
                     keywords_json,description,status,drive_state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("IMG-SYNTH-" + str(n), "bulk_"+str(n)+".png", row["stored_name"],
                     row["thumb_name"], row["size_bytes"], str(n), row["width"], row["height"],
                     "[]", "", "candidate", "local_only", row["created_at"], row["updated_at"]))
        summary = self.client.get("/api/v1/images?limit=200").json()
        self.assertEqual(summary["total"], 502)
        self.assertEqual(summary["total_images"], 502)
        self.assertEqual(len(summary["items"]), 200)
        self.assertEqual(len(self.client.get("/api/v1/images?limit=200&offset=400").json()["items"]), 102)
        matching = self.client.get("/api/v1/images?q=bulk_500&limit=10").json()
        self.assertEqual(matching["total"], 1)
        self.assertEqual(matching["items"][0]["id"], "IMG-SYNTH-500")
        self.assertEqual(self.client.get("/api/v1/images?orientation=landscape").json()["total"], 502)
        self.assertEqual(self.client.get("/api/v1/images?orientation=portrait").json()["total"], 0)

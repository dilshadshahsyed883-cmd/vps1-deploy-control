"""SML face matching: opt-in, multi-face selection and approval-only naming."""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException

PROJECT = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"
spec = importlib.util.spec_from_file_location("sml_face_people_test_module", PROJECT / "face_people.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class FacePeopleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "images").mkdir()
        self.database = self.root / "library.db"

        def connect():
            conn = sqlite3.connect(self.database, timeout=20)
            conn.row_factory = sqlite3.Row
            return conn

        self.db = connect
        with self.db() as conn:
            conn.executescript("""
                CREATE TABLE image_assets (
                  id TEXT PRIMARY KEY, original_name TEXT NOT NULL,
                  stored_name TEXT NOT NULL, sha256 TEXT NOT NULL,
                  deleted_at TEXT, keywords_json TEXT NOT NULL DEFAULT '[]',
                  updated_at TEXT NOT NULL DEFAULT ''
                );
            """)
        self.app = FastAPI()
        module.register_face_people(self.app, data_dir=self.root, db=connect, now_iso=lambda: "2026-10-22T19:00:00Z")
        for startup in self.app.router.on_startup:
            startup()
        self.routes = {(r.path, next(iter(r.methods))) : r.endpoint for r in self.app.routes
                       if getattr(r, "methods", None)}
        self.add_image("first", [])
        self.add_image("second", ["tennis"])
        self.add_image("third", [])
        self.add_image("deleted", [], deleted=True)
        self.fake_faces = {
            "first": [self.face(0, [1, 0, 0])],
            "second": [self.face(0, [0, 1, 0]), self.face(1, [0.99, 0.1, 0])],
            "third": [self.face(0, [0, 0, 1])],
            "deleted": [self.face(0, [1, 0, 0])],
        }

    @staticmethod
    def face(index, vec):
        length = sum(x*x for x in vec)**0.5
        return {"face_index": index,"bbox": [0.05+index*.15,0.1,0.2,0.2],
                "embedding": [x/length for x in vec]}

    def add_image(self, name, tags, deleted=False):
        (self.root/"images"/(name+".png")).write_bytes(b"fixture")
        with self.db() as conn:
            conn.execute("INSERT INTO image_assets(id,original_name,stored_name,sha256,deleted_at,keywords_json) "
                         "VALUES (?,?,?,?,?,?)",
                         (name, name+".png",name+".png",name+"-sha",
                          "2026-10-22" if deleted else None,json.dumps(tags)))

    def endpoint(self, path, method):
        return self.routes[(path,method)]

    def tags(self, image_id):
        with self.db() as conn:
            return json.loads(conn.execute("SELECT keywords_json FROM image_assets WHERE id=?",
                                           (image_id,)).fetchone()[0])

    def wait_finished(self, person_id):
        status = self.endpoint("/api/v1/people/{person_id}/scan","GET")
        for _ in range(100):
            result = status(person_id)
            if result["status"] in ("completed","failed"):
                return result
            time.sleep(.03)
        self.fail("Face scan job did not complete")

    def test_reference_multiple_faces_review_approval_and_undo(self):
        def detect(root, path):
            return self.fake_faces[path.stem]
        with patch.object(module, "_extract_faces",side_effect=detect):
            inspect = self.endpoint("/api/v1/faces/images/{image_id}","GET")
            self.assertEqual(len(inspect("second")["faces"]),2)
            create = self.endpoint("/api/v1/people","POST")
            # Face #2, not face #1, is the reference identity.
            person = create(module.CreatePerson(name="Alexandra Eala",image_id="second",face_index=1))
            pid = person["id"]
            self.endpoint("/api/v1/people/{person_id}/scan","POST")(pid)
            self.assertEqual(self.wait_finished(pid)["status"],"completed")
            pending = self.endpoint("/api/v1/people/{person_id}/matches","GET")(pid)
            self.assertEqual(set(m["image_id"] for m in pending["items"]),{"first","second"})
            self.assertEqual(self.tags("first"),[])
            self.assertEqual(self.tags("second"),["tennis"])
            self.endpoint("/api/v1/people/{person_id}/approve","POST")(
                pid,module.MatchBatch(image_ids=["second"]))
            self.assertEqual(self.tags("second"),["tennis","Alexandra Eala"])
            self.endpoint("/api/v1/people/{person_id}/undo","POST")(
                pid,module.MatchBatch(image_ids=["second"]))
            self.assertEqual(self.tags("second"),["tennis"])
            self.endpoint("/api/v1/people/{person_id}/reject","POST")(
                pid,module.MatchBatch(image_ids=["first"]))
            # Rescan does not override previously rejected images.
            self.endpoint("/api/v1/people/{person_id}/scan","POST")(pid)
            self.assertEqual(self.wait_finished(pid)["status"],"completed")
            rejected = self.endpoint("/api/v1/people/{person_id}/matches","GET")(pid,"rejected")
            self.assertEqual([item["image_id"] for item in rejected["items"]],["first"])
            self.assertNotIn("Alexandra Eala",self.tags("first"))

    def test_atomic_validation_for_batch_and_existing_tags(self):
        with patch.object(module, "_extract_faces",return_value=[self.face(0,[1,0,0])]):
            create = self.endpoint("/api/v1/people","POST")
            pid = create(module.CreatePerson(name="A Person",image_id="first",face_index=0))["id"]
            self.endpoint("/api/v1/people/{person_id}/scan","POST")(pid)
            self.assertEqual(self.wait_finished(pid)["status"],"completed")
            approve=self.endpoint("/api/v1/people/{person_id}/approve","POST")
            with self.assertRaises(HTTPException):
                approve(pid,module.MatchBatch(image_ids=["first","deleted"]))
            self.assertEqual(self.tags("first"),[])
            # Pre-existing manually added keywords should never be removed by undo.
            with self.db() as conn:
                conn.execute("UPDATE image_assets SET keywords_json=? WHERE id=?",
                             (json.dumps(["A Person"]), "first"))
            approve(pid,module.MatchBatch(image_ids=["first"]))
            self.endpoint("/api/v1/people/{person_id}/undo","POST")(
                pid,module.MatchBatch(image_ids=["first"]))
            self.assertEqual(self.tags("first"),["A Person"])

    def test_no_matching_trash_media_or_implicit_model_download(self):
        with patch.object(module, "_extract_faces",return_value=[self.face(0,[1,0,0])]) as mocked:
            inspect=self.endpoint("/api/v1/faces/images/{image_id}","GET")
            with self.assertRaises(HTTPException) as caught:
                inspect("deleted")
            self.assertEqual(caught.exception.status_code,404)
            self.assertEqual(len(inspect("first")["faces"]),1)
            self.assertEqual(len(inspect("first")["faces"]),1)
            self.assertEqual(mocked.call_count,1)  # cached embedding


if __name__=="__main__":
    unittest.main()

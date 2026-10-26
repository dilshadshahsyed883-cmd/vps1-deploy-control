"""Regression: manual person groups must never change legacy tags or media bytes."""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException
except ImportError:
    FastAPI = None
    HTTPException = Exception

PROJECT = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"
MODULE_PATH = PROJECT / "manual_people.py"
if FastAPI is not None:
    spec = importlib.util.spec_from_file_location("sml_manual_people_tests", MODULE_PATH)
    manual = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = manual
    spec.loader.exec_module(manual)


@unittest.skipUnless(FastAPI is not None, "FastAPI required")
class ManualPersonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.dbfile = Path(self.temporary.name) / "library.db"

        def connect():
            conn = sqlite3.connect(self.dbfile)
            conn.row_factory = sqlite3.Row
            return conn

        self.connect = connect
        with connect() as conn:
            conn.executescript("""
                CREATE TABLE image_assets (
                    id TEXT PRIMARY KEY, original_name TEXT, status TEXT,
                    deleted_at TEXT, keywords_json TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE clips (
                    id TEXT PRIMARY KEY, filename TEXT, status TEXT,
                    deleted_at TEXT);
                INSERT INTO image_assets VALUES ('IMG-1','Ronaldo.jpg','approved',NULL,'["legacy tag"]');
                INSERT INTO image_assets VALUES ('IMG-2','Messi.jpg','approved',NULL,'[]');
                INSERT INTO image_assets VALUES ('IMG-3','deleted.png','approved','2026-10-26','[]');
                INSERT INTO clips VALUES ('CLP-1','goal.mp4','approved',NULL);
                INSERT INTO clips VALUES ('CLP-2','removed.mp4','approved','2026-10-26');
            """)
        self.app = FastAPI()
        manual.register_manual_people(self.app, db=connect, now_iso=lambda: "2026-10-26T12:00:00Z")
        for callback in self.app.router.on_startup:
            callback()
        self.routes = {(route.path, method): route.endpoint
                       for route in self.app.routes for method in (route.methods or [])}

    def endpoint(self, path, method):
        return self.routes[(path, method)]

    def make_person(self, name="Cristiano Ronaldo", aliases=None):
        return self.endpoint("/api/v1/manual-people", "POST")(
            manual.PersonInput(name=name, aliases=aliases or ["Ronaldo", "CR7"]))

    def test_create_assign_search_unassign_keeps_original_keywords(self):
        person = self.make_person()
        pid = person["id"]
        assign = self.endpoint("/api/v1/manual-people/{person_id}/assign", "POST")
        assign(pid, manual.AssignAssets(
            media_type="image", ids=["IMG-1"], action_tags=["Goal", "Celebration"]))
        assign(pid, manual.AssignAssets(
            media_type="video", ids=["CLP-1"], action_tags=["Training"]))
        # Repeated assignment merges actions idempotently.
        assign(pid, manual.AssignAssets(
            media_type="image", ids=["IMG-1"], action_tags=["Interview"]))
        groups = self.endpoint("/api/v1/manual-people", "GET")()["items"]
        self.assertEqual(groups[0]["counts"], {"image": 1, "video": 1})
        with self.connect() as conn:
            self.assertEqual(manual.matching_collections(conn, "Ronaldo scored a goal"), {pid})
            self.assertEqual(manual.matching_collections(conn, "CR7 celebration"), {pid})
            tag_index, members = manual.virtual_tags(conn, "image", ["IMG-1", "IMG-2"])
            self.assertEqual(members["IMG-1"], {pid})
            self.assertIn("Cristiano Ronaldo", tag_index["IMG-1"])
            self.assertTrue({"goal", "celebration", "interview"}.issubset(set(tag_index["IMG-1"])))
            self.assertNotIn("IMG-2", members)
            self.assertEqual(json.loads(conn.execute(
                "SELECT keywords_json FROM image_assets WHERE id='IMG-1'").fetchone()[0]),
                ["legacy tag"])
        unlink = self.endpoint("/api/v1/manual-people/{person_id}/unassign", "POST")
        self.assertEqual(unlink(pid, manual.RemoveAssets(
            media_type="image", ids=["IMG-1"]))["unassigned"], 1)
        with self.connect() as conn:
            self.assertNotIn("IMG-1", manual.virtual_tags(conn, "image", ["IMG-1"])[1])
            self.assertEqual(json.loads(conn.execute(
                "SELECT keywords_json FROM image_assets WHERE id='IMG-1'").fetchone()[0]),
                ["legacy tag"])

    def test_batch_is_atomic_and_trashed_items_cannot_be_linked(self):
        pid = self.make_person()["id"]
        assign = self.endpoint("/api/v1/manual-people/{person_id}/assign", "POST")
        with self.assertRaises(HTTPException):
            assign(pid, manual.AssignAssets(
                media_type="image", ids=["IMG-1", "IMG-3"], action_tags=[]))
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sml_person_assets").fetchone()[0], 0)
        with self.assertRaises(HTTPException):
            assign(pid, manual.AssignAssets(
                media_type="video", ids=["CLP-1", "CLP-2"], action_tags=[]))
        with self.assertRaises(HTTPException):
            assign(pid, manual.AssignAssets(media_type="invalid", ids=["IMG-1"]))
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sml_person_assets").fetchone()[0], 0)

    def test_multiple_people_and_aliases_belong_to_correct_collections(self):
        ronaldo = self.make_person()
        messi = self.make_person("Lionel Messi", ["Messi"])
        assign = self.endpoint("/api/v1/manual-people/{person_id}/assign", "POST")
        assign(ronaldo["id"], manual.AssignAssets(media_type="image", ids=["IMG-1"]))
        assign(messi["id"], manual.AssignAssets(media_type="image", ids=["IMG-2"]))
        with self.connect() as conn:
            self.assertEqual(manual.matching_collections(conn, "Messi training"), {messi["id"]})
            self.assertEqual(manual.matching_collections(conn, "Ronaldo match"), {ronaldo["id"]})
            self.assertEqual(manual.associated_ids(conn, "image", {ronaldo["id"]}), ["IMG-1"])
            self.assertEqual(manual.associated_ids(conn, "image", {messi["id"]}), ["IMG-2"])
        with self.assertRaises(HTTPException):
            self.make_person(name="cristiano ronaldo")

    def test_update_person_and_asset_listing_preserve_metadata(self):
        pid = self.make_person()["id"]
        update = self.endpoint("/api/v1/manual-people/{person_id}", "PATCH")
        update(pid, manual.PersonPatch(name="Cristiano Ronaldo", aliases=["Ronaldo", "CR7", "R7"]))
        self.assertEqual(self.endpoint("/api/v1/manual-people", "GET")()["items"][0]["aliases"],
                         ["Ronaldo", "CR7", "R7"])
        self.endpoint("/api/v1/manual-people/{person_id}/assign", "POST")(
            pid, manual.AssignAssets(media_type="image", ids=["IMG-1"], action_tags=["trophy"]))
        assets = self.endpoint("/api/v1/manual-people/{person_id}/assets", "GET")(pid, media_type="all", limit=100, offset=0)
        self.assertEqual(assets["items"][0]["action_tags"], ["trophy"])
        self.assertIn("IMG-1", assets["items"][0]["thumb_url"])
        self.assertEqual(assets["items"][0]["status"], "approved")


class ManualPersonUI(unittest.TestCase):
    def test_additive_ui_keeps_old_image_and_face_controls(self):
        html = (PROJECT / "static/index.html").read_text()
        script = (PROJECT / "static/manual_people.js").read_text()
        media = (PROJECT / "media_extensions.py").read_text()
        for key in ("manualPeoplePanel", "manualPersonPicker", "manualPersonSave",
                    "manualAssignImages", "manualAssignClips", "peoplePanel",
                    "imageBulkBar", "imageBulkKeyword", "identifyImage"):
            self.assertIn('id="' + key + '"', html)
        self.assertIn("manual_people.js", html)
        self.assertIn("manual_people.css", html)
        self.assertIn("imageSelected", script)
        self.assertIn("state.selected", script)
        self.assertIn("matching_collections", media)
        self.assertIn("virtual_tags", media)
        self.assertIn('"/api/v1/media/search"', media)


if __name__ == "__main__":
    unittest.main()

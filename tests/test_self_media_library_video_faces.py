"""Video face recognition regression: sampled faces, opt-in, approval and undo."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi import FastAPI, HTTPException
    import numpy as np
except ImportError:
    FastAPI = None
    HTTPException = Exception

PROJECT = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"
sys.path.insert(0, str(PROJECT))
if FastAPI is not None:
    import face_people
    import video_face_people


@unittest.skipUnless(FastAPI is not None, "SML CI supplies FastAPI/NumPy")
class VideoFaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "clips").mkdir()
        (self.root / "images").mkdir()
        database = self.root / "library.db"

        def db():
            conn = sqlite3.connect(database, timeout=20)
            conn.row_factory = sqlite3.Row
            return conn

        self.db = db
        with db() as conn:
            conn.executescript("""
                CREATE TABLE clips (
                  id TEXT PRIMARY KEY, filename TEXT, thumb_filename TEXT, sha256 TEXT,
                  duration REAL, deleted_at TEXT, updated_at TEXT DEFAULT ''
                );
                CREATE TABLE clip_keywords (
                  clip_id TEXT NOT NULL, keyword TEXT NOT NULL,
                  source TEXT NOT NULL DEFAULT 'ai',confidence REAL,created_at TEXT NOT NULL,
                  PRIMARY KEY(clip_id,keyword)
                );
                CREATE TABLE image_assets (
                  id TEXT PRIMARY KEY,stored_name TEXT,sha256 TEXT,deleted_at TEXT
                );
            """)
        app = FastAPI()
        clock = lambda: "2026-10-26T19:00:00Z"
        face_people.register_face_people(app, data_dir=self.root, db=db, now_iso=clock)
        video_face_people.register_video_face_people(app, data_dir=self.root, db=db, now_iso=clock)
        for start in app.router.on_startup:
            start()
        self.routes = {(route.path, method):route.endpoint for route in app.routes
                       for method in getattr(route, "methods", [])}
        self.add("tennis", ["wuhan"])
        self.add("other", [])
        self.add("trash", [], deleted=True)
        self.mock_faces = {
            "tennis":[self.face(0, [0,1,0],0.7,0),self.face(1, [1,0,0],1.6,1)],
            "other":[self.face(0, [.99,.1,0],0.5,0)],
            "trash":[self.face(0, [1,0,0],.4,0)]
        }

    def endpoint(self, path, method):
        return self.routes[(path, method)]

    def face(self, index, vector, timestamp, frame):
        v=np.array(vector,dtype=float)
        return {"face_index":index,"frame_index":frame,"time_seconds":timestamp,
                "bbox":[.15,.20,.30,.30],"embedding":(v/np.linalg.norm(v)).tolist()}

    def add(self, ident, keywords, deleted=False):
        (self.root/"clips"/(ident+".mp4")).write_bytes(b"test-clip")
        with self.db() as conn:
            conn.execute("INSERT INTO clips(id,filename,sha256,duration,deleted_at) VALUES (?,?,?,?,?)",
                         (ident,ident+".mp4",ident+"-sha",4,"2026-10-25" if deleted else None))
            for keyword in keywords:
                conn.execute("INSERT INTO clip_keywords VALUES (?,?,?,?,?)",
                             (ident,keyword,"manual",1,"test"))

    def keywords(self, ident):
        with self.db() as conn:
            return [tuple(row) for row in conn.execute(
                "SELECT keyword,source FROM clip_keywords WHERE clip_id=? ORDER BY keyword",
                (ident,))]

    def wait(self, person):
        read=self.endpoint("/api/v1/people/{person_id}/video-scan","GET")
        for _ in range(120):
            status=read(person)
            if status["status"] in ("completed","failed"):
                return status
            time.sleep(.04)
        self.fail("Scan did not finish")

    def test_video_face_reference_review_tags_rescan_undo(self):
        def fake_extract(_root,path,_duration):
            return self.mock_faces[path.stem]
        with patch.object(video_face_people,"_extract_video_faces",side_effect=fake_extract) as extractor:
            inspected=self.endpoint("/api/v1/faces/clips/{clip_id}","GET")("tennis")
            self.assertEqual(len(inspected["faces"]),2)
            create=self.endpoint("/api/v1/people/video-reference","POST")
            person=create(video_face_people.VideoPerson(
                name="Alexandra Eala",clip_id="tennis",face_index=1))
            pid=person["id"]
            self.assertEqual(person["reference_clip_id"],"tennis")
            self.assertEqual(extractor.call_count,1)  # cached by content signature
            scan=self.endpoint("/api/v1/people/{person_id}/video-scan","POST")
            scan(pid)
            self.assertEqual(self.wait(pid)["status"],"completed")
            pending=self.endpoint("/api/v1/people/{person_id}/video-matches","GET")(pid)
            self.assertEqual({x["clip_id"] for x in pending["items"]},{"tennis","other"})
            self.assertEqual(self.keywords("other"),[])  # never auto-approve
            self.endpoint("/api/v1/people/{person_id}/video-approve","POST")(
                pid,video_face_people.VideoBatch(clip_ids=["tennis","other"]))
            self.assertIn(("Alexandra Eala","face-review"),self.keywords("other"))
            self.assertIn(("wuhan","manual"),self.keywords("tennis"))
            self.endpoint("/api/v1/people/{person_id}/video-reject","POST")(
                pid,video_face_people.VideoBatch(clip_ids=["other"]))
            self.assertNotIn(("Alexandra Eala","face-review"),self.keywords("other"))
            scan(pid)
            self.assertEqual(self.wait(pid)["status"],"completed")
            rejected=self.endpoint("/api/v1/people/{person_id}/video-matches","GET")(pid,"rejected")
            self.assertEqual([x["clip_id"] for x in rejected["items"]],["other"])
            self.endpoint("/api/v1/people/{person_id}/video-undo","POST")(
                pid,video_face_people.VideoBatch(clip_ids=["tennis"]))
            self.assertNotIn(("Alexandra Eala","face-review"),self.keywords("tennis"))
            self.assertEqual(extractor.call_count,2) # one extraction per non-trashed clip

    def test_atomic_validation_and_manual_name_is_preserved(self):
        with patch.object(video_face_people,"_extract_video_faces",
                          side_effect=lambda _root,path,_duration:self.mock_faces[path.stem]):
            create=self.endpoint("/api/v1/people/video-reference","POST")
            pid=create(video_face_people.VideoPerson(
                name="Alexandra Eala",clip_id="tennis",face_index=1))["id"]
            self.endpoint("/api/v1/people/{person_id}/video-scan","POST")(pid)
            self.assertEqual(self.wait(pid)["status"],"completed")
            approve=self.endpoint("/api/v1/people/{person_id}/video-approve","POST")
            with self.assertRaises(HTTPException):
                approve(pid,video_face_people.VideoBatch(clip_ids=["other","trash"]))
            self.assertEqual(self.keywords("other"),[])
            with self.db() as conn:
                conn.execute("INSERT INTO clip_keywords VALUES (?,?,?,?,?)",
                             ("other","Alexandra Eala","manual",1,"test"))
            approve(pid,video_face_people.VideoBatch(clip_ids=["other"]))
            self.endpoint("/api/v1/people/{person_id}/video-undo","POST")(
                pid,video_face_people.VideoBatch(clip_ids=["other"]))
            self.assertIn(("Alexandra Eala","manual"),self.keywords("other"))

    def test_clip_missing_trash_and_no_implicit_scan(self):
        with patch.object(video_face_people,"_extract_video_faces",return_value=[]) as fake:
            with self.assertRaises(HTTPException) as caught:
                self.endpoint("/api/v1/faces/clips/{clip_id}","GET")("trash")
            self.assertEqual(caught.exception.status_code,404)
            self.assertEqual(self.endpoint("/api/v1/faces/clips/{clip_id}","GET")("tennis")["faces"],[])
            self.assertEqual(self.endpoint("/api/v1/faces/clips/{clip_id}","GET")("tennis")["faces"],[])
            self.assertEqual(fake.call_count,1)

    def test_shared_people_can_be_scanned_across_media_types(self):
        with self.db() as conn:
            conn.execute("INSERT INTO people "
                         "(id,name,reference_image_id,reference_face_index,reference_embedding,created_at)"
                         "VALUES (?,?,?,?,?,?)",
                         ("image-person","Alexandra Eala","ref",0,json.dumps([1.,0.,0.]),"test"))
        with patch.object(video_face_people,"_extract_video_faces",
                          side_effect=lambda _root,path,_duration:self.mock_faces[path.stem]):
            self.endpoint("/api/v1/people/{person_id}/video-scan","POST")("image-person")
            self.assertEqual(self.wait("image-person")["status"],"completed")
            found=self.endpoint("/api/v1/people/{person_id}/video-matches","GET")("image-person")
            self.assertEqual(len(found["items"]),2)


class VideoFaceMarkup(unittest.TestCase):
    def test_clip_identify_action_and_video_review_controls_exist(self):
        html=(PROJECT/"static/index.html").read_text()
        js=(PROJECT/"static/video_face_people.js").read_text()
        app=(PROJECT/"app.py").read_text()
        for ident in ("identifyClipFace","videoFaceReferencePlayer","videoFaceCreate",
                      "videoFaceScan","videoFacePicker","videoFaceMatches",
                      "videoFaceApprove","videoFaceReject","videoFaceUndo"):
            self.assertIn('id="'+ident+'"',html)
        self.assertIn("register_video_face_people(app",app)
        self.assertIn("closeDrawer();gotoPage('library');setLibraryTab('face')",js)


if __name__=="__main__":
    unittest.main()

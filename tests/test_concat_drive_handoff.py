"""Private Concat MP4 handoff: local and canonical Drive mock fixtures.

No real Google Drive, network, worker, OAuth or YouTube calls.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/copyright-uploader/part01/lum_engine"))
from concat_source import (
    ConcatSourceError, _canonical_drive_source, stage_concat_link,
)

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 108
SHA = hashlib.sha256(MP4).hexdigest()
URL = "https://app.scenebuilder.online/api/cloud-render/media/media-42/file"


class OwnerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "cloud-render"
        self.root.mkdir()
        self.media = self.root / "media"
        self.media.mkdir()
        self.stage = Path(self.tmp.name) / "concat-private"
        self.db = self.root / "cloud_render.sqlite3"
        with sqlite3.connect(self.db) as con:
            con.execute("""CREATE TABLE files(
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                state TEXT NOT NULL, owner_username TEXT NOT NULL,
                drive_path TEXT NOT NULL DEFAULT '', md5 TEXT NOT NULL DEFAULT ''
            )""")
            con.execute("""INSERT INTO files(id,name,size,sha256,state,owner_username)
                           VALUES(?,?,?,?,?,?)""",
                        ("media-42", "Owned.mp4", len(MP4), SHA, "ready", "alice"))

    def set_drive(self, state="delivered", path="gdrive:Cloud Render/alice/media-42_Owned.mp4"):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE files SET state=?, drive_path=? WHERE id='media-42'",
                        (state, path))

    def test_local_owner_handoff_preserves_original(self):
        original = self.media / "media-42.mp4"
        original.write_bytes(MP4)
        staged, name, size, sha = stage_concat_link(URL, "alice", self.stage, cloud_root=self.root)
        self.assertEqual(Path(staged).read_bytes(), MP4)
        self.assertEqual(original.read_bytes(), MP4)
        self.assertEqual((name, size, sha), ("Owned.mp4", len(MP4), SHA))

    def test_bob_and_admin_cannot_take_alice_mp4(self):
        (self.media / "media-42.mp4").write_bytes(MP4)
        for actor in ("bob", "admin"):
            with self.subTest(actor=actor), self.assertRaises(ConcatSourceError):
                stage_concat_link(URL, actor, self.stage, cloud_root=self.root)

    def test_ownerless_record_does_not_become_admin_data(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE files SET owner_username='' WHERE id='media-42'")
        (self.media / "media-42.mp4").write_bytes(MP4)
        for actor in ("alice", "admin"):
            with self.assertRaises(ConcatSourceError):
                stage_concat_link(URL, actor, self.stage, cloud_root=self.root)

    def test_drive_backed_handoff_verified_after_local_cleanup(self):
        self.set_drive()
        def fake_drive(remote, destination):
            self.assertEqual(remote, "gdrive:Cloud Render/alice/media-42_Owned.mp4")
            destination.write_bytes(MP4)
        with patch("concat_source._stage_drive", side_effect=fake_drive) as bridge:
            staged, _, size, sha = stage_concat_link(URL, "alice", self.stage, cloud_root=self.root)
        bridge.assert_called_once()
        self.assertEqual(Path(staged).read_bytes(), MP4)
        self.assertEqual((size, sha), (len(MP4), SHA))

    def test_drive_tamper_fails_closed_and_cleans_staging(self):
        self.set_drive()
        with patch("concat_source._stage_drive", side_effect=lambda r, p: p.write_bytes(MP4[:-1] + b"x")):
            with self.assertRaises(ConcatSourceError):
                stage_concat_link(URL, "alice", self.stage, cloud_root=self.root)
        self.assertEqual(list(self.stage.glob("*.mp4")), [])

    def test_reject_arbitrary_foreign_remote(self):
        with self.assertRaises(ConcatSourceError):
            _canonical_drive_source("gdrive:Other/user/video.mp4", "alice", "media-42")
        with self.assertRaises(ConcatSourceError):
            _canonical_drive_source("evil:Cloud Render/alice/media-42_Owned.mp4", "alice", "media-42")
        with self.assertRaises(ConcatSourceError):
            _canonical_drive_source("gdrive:Cloud Render/bob/media-42_Owned.mp4", "alice", "media-42")


if __name__ == "__main__":
    unittest.main()

"""Song-only guarded release: prove truly parked network waits without interrupting uploads."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production import song_uploader_group_summary_release as release


SCHEMA = """CREATE TABLE videos (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    auto_engine_profile_id INTEGER,
    claim_worker_id TEXT,
    recovery_state TEXT,
    youtube_video_id TEXT,
    youtube_url TEXT,
    resumable_uri TEXT,
    media_stage_state TEXT,
    pipeline_stage TEXT,
    upload_bytes_confirmed INTEGER,
    progress INTEGER,
    channel_db_id INTEGER,
    upload_fingerprint TEXT
)"""


class SongReleaseParkedNetworkProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        folder=self.root/"core/data"
        folder.mkdir(parents=True)
        self.path=folder/"uploader.db"
        with sqlite3.connect(self.path) as conn:
            conn.execute(SCHEMA)
            conn.execute("""CREATE TABLE upload_ledger(
                channel_db_id INTEGER, upload_fingerprint TEXT, state TEXT
            )""")

    def job(self, status="WAITING_NETWORK", **kw):
        values=dict(
            status=status,auto_engine_profile_id=9,claim_worker_id=None,
            recovery_state="NONE",youtube_video_id=None,youtube_url=None,
            resumable_uri=None,media_stage_state="PENDING",pipeline_stage="PREPARE",
            upload_bytes_confirmed=0,progress=0,channel_db_id=7,
            upload_fingerprint=None,
        )
        values.update(kw)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO videos ("+",".join(values.keys())+") VALUES ("+
                ",".join("?" for _ in values)+")",
                tuple(values.values())
            )

    def idle(self, active=None):
        with patch.object(release,"STATE",self.root),patch.object(
            release,"health",return_value={"active_project_id":active,"ok":True}
        ):
            return release.idle()

    def test_pre_media_network_wait_can_be_parked_without_changing_job(self):
        self.job()
        self.idle()
        with sqlite3.connect(self.path) as db:
            record=db.execute("SELECT status,claim_worker_id,progress FROM videos").fetchone()
        self.assertEqual(record,("WAITING_NETWORK",None,0))

    def test_no_wait_is_idle_and_queued_job_is_retained(self):
        self.job(status="QUEUED")
        self.idle()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM videos").fetchone()[0],"QUEUED")

    def test_active_uploads_block_even_when_other_jobs_are_parked(self):
        self.job()
        self.job(status="UPLOADING")
        with self.assertRaisesRegex(RuntimeError,"upload in progress"):
            self.idle()
        self.assertEqual(sqlite3.connect(self.path).execute("SELECT COUNT(*) FROM videos").fetchone()[0],2)

    def test_claimed_and_ambiguous_network_waits_fail_closed(self):
        overrides=(
            {"claim_worker_id":"active-worker"},
            {"resumable_uri":"saved-session"},
            {"youtube_video_id":"remote-video"},
            {"progress":15},
            {"upload_bytes_confirmed":1024},
            {"media_stage_state":"RUNNING"},
            {"pipeline_stage":"MEDIA"},
            {"recovery_state":"REVIEW_REQUIRED"},
        )
        for data in overrides:
            with self.subTest(data=data):
                with sqlite3.connect(self.path) as db: db.execute("DELETE FROM videos")
                self.job(**data)
                with self.assertRaisesRegex(RuntimeError,"not proven idle"):
                    self.idle()

    def test_uploaded_ledger_evidence_blocks_release(self):
        self.job(upload_fingerprint="sha-safe")
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO upload_ledger VALUES (?,?,?)",(7,"sha-safe","UPLOADED"))
        with self.assertRaisesRegex(RuntimeError,"not proven idle"):
            self.idle()

    def test_web_upload_guard_is_kept(self):
        with self.assertRaisesRegex(RuntimeError,"web upload in progress"):
            self.idle(active="web-upload-in-flight")


if __name__=="__main__":
    unittest.main()

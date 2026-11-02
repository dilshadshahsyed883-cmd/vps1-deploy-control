"""V1.1 explicitly approved 7 Kokoro + 6 Edge deletion safety (offline)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "production/voice_v11_legacy_delete.py"
spec = importlib.util.spec_from_file_location("voice_v11_legacy_delete", SOURCE)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class VoiceV11LegacyDeletion(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for engine, details in cleanup.EXPECTED.items():
            path = self.root / details["db"]
            path.parent.mkdir(parents=True)
            (path.parent / "jobs").mkdir()
            with sqlite3.connect(path) as con:
                con.execute(
                    "CREATE TABLE jobs(job_id TEXT PRIMARY KEY, status TEXT, "
                    "delivery_status TEXT, input_file TEXT, wav_file TEXT, "
                    "mp3_file TEXT, srt_file TEXT)"
                )
                for index in range(details["count"]):
                    job = details["prefix"] + f"{index:016X}"
                    folder = path.parent / "jobs" / job
                    folder.mkdir()
                    source = folder / "source.txt"
                    source.write_text("old source")
                    status = "failed" if engine == "Edge" and index == 5 else "completed"
                    con.execute(
                        "INSERT INTO jobs(job_id,status,delivery_status,input_file) "
                        "VALUES(?,?,?,?)",
                        (job, status, "not_applicable" if status == "failed" else "cleaned", str(source)),
                    )
        (self.root / "preserve.txt").write_text("unrelated data")

    def test_read_only_preview_does_not_change_database(self):
        result = cleanup.scan(self.root)
        self.assertEqual(result["Kokoro"]["count"], 7)
        self.assertEqual(result["Edge"]["count"], 6)
        for spec in cleanup.EXPECTED.values():
            with sqlite3.connect(self.root / spec["db"]) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], spec["count"])

    def test_exact_approved_legacy_job_removal_keeps_other_files(self):
        summary = cleanup.scan(self.root)
        self.assertEqual(cleanup.apply(self.root, summary), {"Kokoro": 7, "Edge": 6})
        self.assertEqual((self.root / "preserve.txt").read_text(), "unrelated data")
        for spec in cleanup.EXPECTED.values():
            with sqlite3.connect(self.root / spec["db"]) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_pending_delivery_blocks_all_deletion(self):
        path = self.root / cleanup.EXPECTED["Edge"]["db"]
        with sqlite3.connect(path) as con:
            con.execute("UPDATE jobs SET delivery_status='retry_pending' "
                        "WHERE job_id='EDJ-0000000000000000'")
        with self.assertRaises(cleanup.GuardError):
            cleanup.scan(self.root)
        with sqlite3.connect(path) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 6)

    def test_symlinked_media_blocks_all_deletion(self):
        path = self.root / cleanup.EXPECTED["Kokoro"]["db"]
        (path.parent / "jobs" / "KOJ-0000000000000000" / "foreign").symlink_to(
            self.root / "preserve.txt"
        )
        with self.assertRaises(cleanup.GuardError):
            cleanup.scan(self.root)


if __name__ == "__main__":
    unittest.main()

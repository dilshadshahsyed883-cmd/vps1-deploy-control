"""Voice legacy ownership audit is read-only and blocks unassigned rows."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from production.voice_owner_preflight import report


class VoiceLegacyOwnerPreflight(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.paths = {name: Path(self.root.name) / (name + ".db") for name in ("clone","kokoro","edge")}
        for name,path in self.paths.items():
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE jobs (job_id TEXT, owner_username TEXT NOT NULL DEFAULT '')")
                if name == "clone":
                    conn.execute("CREATE TABLE profiles (profile_id TEXT, owner_username TEXT NOT NULL DEFAULT '')")

    def test_unassigned_rows_are_reported_but_not_mutated(self):
        with sqlite3.connect(self.paths["clone"]) as conn:
            conn.execute("INSERT INTO jobs VALUES('old','')")
            conn.execute("INSERT INTO profiles VALUES('old','')")
        result,blocked = report(self.paths)
        self.assertTrue(blocked)
        self.assertEqual(result["voice_owner_preflight"],"BLOCKED")
        self.assertEqual(result["engines"][0]["tables"]["jobs"]["unassigned"],1)
        with sqlite3.connect(self.paths["clone"]) as conn:
            self.assertEqual(conn.execute("SELECT owner_username FROM jobs").fetchone()[0],"")

    def test_all_owners_assigned_is_ready(self):
        for name,path in self.paths.items():
            with sqlite3.connect(path) as conn:
                conn.execute("INSERT INTO jobs VALUES('test','alpha')")
                if name == "clone":
                    conn.execute("INSERT INTO profiles VALUES('test','alpha')")
        result,blocked = report(self.paths)
        self.assertFalse(blocked)
        self.assertEqual(result["voice_owner_preflight"],"READY")

    def test_missing_owner_column_is_blocked_even_if_empty(self):
        with sqlite3.connect(self.paths["edge"]) as conn:
            conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
            conn.execute("CREATE TABLE jobs(job_id TEXT)")
        result,blocked = report(self.paths)
        self.assertTrue(blocked)
        self.assertFalse(result["engines"][-1]["tables"]["jobs"]["owner_column_present"])


if __name__=="__main__":
    unittest.main()

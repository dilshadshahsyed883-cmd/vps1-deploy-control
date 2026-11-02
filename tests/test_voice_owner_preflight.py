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
                if name == "clone":
                    conn.execute("CREATE TABLE jobs (job_id TEXT, owner_username TEXT NOT NULL DEFAULT '', profile_id TEXT)")
                    conn.execute("CREATE TABLE profiles (profile_id TEXT, owner_username TEXT NOT NULL DEFAULT '')")
                    conn.execute("CREATE TABLE categories (name TEXT)")
                    conn.execute("INSERT INTO categories(name) VALUES('General')")
                else:
                    conn.execute("CREATE TABLE jobs (job_id TEXT, owner_username TEXT NOT NULL DEFAULT '')")

    def test_unassigned_rows_are_reported_but_not_mutated(self):
        with sqlite3.connect(self.paths["clone"]) as conn:
            conn.execute("INSERT INTO jobs(job_id,owner_username,profile_id) VALUES('old','','old')")
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
                if name == "clone":
                    conn.execute("INSERT INTO profiles VALUES('test','alpha')")
                    conn.execute("INSERT INTO jobs(job_id,owner_username,profile_id) VALUES('test','alpha','test')")
                else:
                    conn.execute("INSERT INTO jobs VALUES('test','alpha')")
        result,blocked = report(self.paths)
        self.assertFalse(blocked)
        self.assertEqual(result["voice_owner_preflight"],"READY")

    def test_legacy_categories_and_foreign_profile_links_block_read_only(self):
        with sqlite3.connect(self.paths["clone"]) as conn:
            conn.execute("INSERT INTO categories(name) VALUES('Legacy Series')")
            conn.execute("INSERT INTO profiles VALUES('profile1','alpha')")
            conn.execute("INSERT INTO jobs(job_id,owner_username,profile_id) VALUES('foreign','beta','profile1')")
        result, blocked = report(self.paths)
        self.assertTrue(blocked)
        clone = result["engines"][0]
        self.assertEqual(clone["unassigned_legacy_categories"], 1)
        self.assertEqual(clone["job_profile_owner_conflicts"], 1)
        with sqlite3.connect(self.paths["clone"]) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0], 2)

    def test_orphan_profile_reference_blocks_even_if_owner_present(self):
        with sqlite3.connect(self.paths["clone"]) as conn:
            conn.execute("INSERT INTO jobs(job_id,owner_username,profile_id) VALUES('orphan','alpha','missing')")
        result, blocked = report(self.paths)
        self.assertTrue(blocked)
        self.assertEqual(result["engines"][0]["job_profile_owner_conflicts"], 1)

    def test_v11_empty_legacy_ownerless_db_does_not_block(self):
        # The owner explicitly deleted the 13 legacy jobs, not migrated them.
        with sqlite3.connect(self.paths["edge"]) as conn:
            conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
            conn.execute("CREATE TABLE jobs(job_id TEXT)")
        result,blocked = report(self.paths)
        self.assertFalse(blocked)
        self.assertEqual(result["voice_owner_preflight"], "READY")
        self.assertEqual(result["engines"][-1]["tables"]["jobs"]["total"], 0)
        self.assertFalse(result["engines"][-1]["tables"]["jobs"]["owner_column_present"])

    def test_nonempty_legacy_table_with_missing_owner_column_still_blocks(self):
        with sqlite3.connect(self.paths["edge"]) as conn:
            conn.execute("ALTER TABLE jobs RENAME TO jobs_old")
            conn.execute("CREATE TABLE jobs(job_id TEXT)")
            conn.execute("INSERT INTO jobs(job_id) VALUES('remaining-legacy')")
        result,blocked = report(self.paths)
        self.assertTrue(blocked)
        self.assertEqual(result["engines"][-1]["tables"]["jobs"]["unassigned"], 1)

    def test_v11_empty_clone_schema_without_owner_columns_is_ready(self):
        with sqlite3.connect(self.paths["clone"]) as conn:
            conn.execute("ALTER TABLE jobs RENAME TO old_jobs")
            conn.execute("ALTER TABLE profiles RENAME TO old_profiles")
            conn.execute("CREATE TABLE jobs(job_id TEXT, profile_id TEXT)")
            conn.execute("CREATE TABLE profiles(profile_id TEXT)")
        result,blocked = report(self.paths)
        self.assertFalse(blocked)
        self.assertIsNone(result["engines"][0]["job_profile_owner_conflicts"])

    def test_clone_legacy_profile_without_owner_column_still_blocks(self):
        with sqlite3.connect(self.paths["clone"]) as conn:
            conn.execute("ALTER TABLE profiles RENAME TO old_profiles")
            conn.execute("CREATE TABLE profiles(profile_id TEXT)")
            conn.execute("INSERT INTO profiles(profile_id) VALUES('orphan')")
        result,blocked = report(self.paths)
        self.assertTrue(blocked)


if __name__=="__main__":
    unittest.main()

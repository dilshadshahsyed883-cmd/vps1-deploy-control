"""Integration fixtures for guarded owner-only Short Maker legacy claim."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from production import runtime
from production import studio_haseeb1_shortmaker_assignment as migration
from production.studio_haseeb1_assignment import MigrationBlocked


class ShortMakerFirstClaim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        scene = self.root / "scene-builder/web/web.sqlite3"
        scene.parent.mkdir(parents=True)
        with sqlite3.connect(scene) as db:
            db.execute("CREATE TABLE users(username TEXT,is_active INT,studio_access TEXT,is_admin INT)")
            db.execute("INSERT INTO users VALUES('haseeb1',1,'song,short',0)")
        self.jobs = self.root / "short-maker/jobs"
        self.folders = self.root / "short-maker/vps_short_folders"
        self.jobs.mkdir(parents=True)
        self.folders.mkdir(parents=True)
        for i in range(6):
            job = self.jobs / f"job{i}"
            job.mkdir()
            (job / "state.json").write_text(json.dumps(
                {"job_id":f"job{i}","status":"completed","owner":"","outputs":[f"video{i}.mp4"]}))
            (job / f"video{i}.mp4").write_bytes(b"valuable media " + bytes([i]))
        (self.jobs / "retry.lock").write_text("staging file is not a job")
        for i in range(3):
            folder = self.folders / f"Folder{i}"
            folder.mkdir()
            (folder / f"output{i}.mp4").write_bytes(b"rendered " + bytes([i]))

    def run_claim(self, revision):
        with patch.object(runtime, "DATA", self.root), \
             patch.object(runtime, "BASE", self.root), \
             patch("shutil.disk_usage", return_value=SimpleNamespace(free=20_000_000_000)):
            migration.install_shortmaker_claim(revision)

    def test_real_job_and_folder_firstclaim_media_unchanged_and_idempotent(self):
        originals = [(self.jobs / f"job{i}" / f"video{i}.mp4").read_bytes()
                     for i in range(6)]
        media = [(self.folders / f"Folder{i}" / f"output{i}.mp4").read_bytes()
                 for i in range(3)]
        self.run_claim("a"*40)
        for i in range(6):
            state = json.loads((self.jobs / f"job{i}" / "state.json").read_text())
            self.assertEqual(state["owner"], "haseeb1")
            self.assertEqual(state["status"], "completed")
            self.assertEqual((self.jobs / f"job{i}" / f"video{i}.mp4").read_bytes(), originals[i])
        for i in range(3):
            folder = self.folders / f"Folder{i}"
            self.assertEqual((folder / ".studio_owner").read_text(), "haseeb1")
            self.assertEqual((folder / f"output{i}.mp4").read_bytes(), media[i])
        receipt = self.root / "transactions/haseeb1-shortmaker-firstclaim" / ("a"*40) / "receipt.json"
        self.assertEqual(json.loads(receipt.read_text())["state"], "SHORTMAKER_ASSIGNED")
        self.run_claim("b"*40)
        self.assertEqual(migration.collect(self.root / "short-maker")[2], [])

    def test_nonterminal_and_foreign_owner_fail_closed(self):
        job = self.jobs / "job0/state.json"
        state = json.loads(job.read_text())
        state["status"] = "running"
        job.write_text(json.dumps(state))
        with self.assertRaisesRegex(MigrationBlocked, "NONTERMINAL"):
            self.run_claim("c"*40)
        self.assertFalse((self.folders / "Folder0/.studio_owner").exists())
        state["status"] = "failed"
        state["owner"] = "other-user"
        job.write_text(json.dumps(state))
        with self.assertRaisesRegex(MigrationBlocked, "FOREIGN_JOB_OWNER"):
            self.run_claim("d"*40)

    def test_guarded_compensation_restores_exact_metadata(self):
        original = (self.jobs / "job0/state.json").read_bytes()
        real_replace = migration.safe_replace
        attempts = [0]
        def fail_second(*args):
            attempts[0] += 1
            if attempts[0] == 2:
                raise RuntimeError("fault injection")
            return real_replace(*args)
        with patch.object(migration, "safe_replace", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "fault injection"):
                self.run_claim("e"*40)
        self.assertEqual((self.jobs / "job0/state.json").read_bytes(), original)
        self.assertFalse((self.folders / "Folder0/.studio_owner").exists())
        self.assertEqual(json.loads((self.root/"transactions/haseeb1-shortmaker-firstclaim"/("e"*40)/"receipt.json").read_text())["state"],
                         "COMPENSATED")


if __name__ == "__main__":
    unittest.main()

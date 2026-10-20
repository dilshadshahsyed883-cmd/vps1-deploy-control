"""Voice TTS controls guarded installer: isolated idle gates and safe backups."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from production import voice_tts_controls_release as release


class VoiceTTSReleaseTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.paths = [Path(self.folder.name) / (e+".sqlite") for e in ("kokoro","edge")]
        for path in self.paths:
            with sqlite3.connect(path) as c:
                c.execute("create table jobs(job_id text,status text,delivery_status text)")
                c.execute("insert into jobs values('TEST','completed','cleaned')")

    def mark(self, path, status, delivery):
        with sqlite3.connect(path) as c:
            c.execute("update jobs set status=?, delivery_status=?", (status, delivery))

    def test_idle_gate_fences_both_databases_and_releases_locks(self):
        locks=release.check_idle_and_lock(self.paths)
        self.assertEqual(len(locks),2)
        release.unlock(locks)
        with sqlite3.connect(self.paths[0]) as c:
            c.execute("update jobs set status='paused'")  # locks really released

    def test_processing_and_queued_jobs_are_never_interrupted(self):
        for state in release.BUSY:
            with self.subTest(state=state):
                self.mark(self.paths[0],state,"waiting_generation")
                with self.assertRaisesRegex(RuntimeError,"active/queued synthesis"):
                    release.check_idle_and_lock(self.paths)
                self.mark(self.paths[0],"completed","cleaned")

    def test_active_drive_delivery_blocks_worker_restart(self):
        for state in release.DELIVERY_BUSY:
            with self.subTest(state=state):
                self.mark(self.paths[1],"completed",state)
                with self.assertRaisesRegex(RuntimeError,"active/queued synthesis"):
                    release.check_idle_and_lock(self.paths)
                self.mark(self.paths[1],"completed","cleaned")

    def test_additive_db_snapshots_are_real_sqlite(self):
        output=Path(self.folder.name)/"backup"/"original.sqlite"
        release.backup_sqlite(self.paths[0],output)
        self.assertTrue(output.is_file())
        with sqlite3.connect(output) as c:
            self.assertEqual(c.execute("select status from jobs").fetchone()[0],"completed")
            self.assertEqual(c.execute("pragma integrity_check").fetchone()[0],"ok")

    def test_only_three_intended_units_and_source_paths(self):
        self.assertEqual(len(release.UNITS),3)
        self.assertIn("vps1-production-kokoro-worker.service",release.UNITS)
        self.assertIn("vps1-production-edge-worker.service",release.UNITS)
        self.assertNotIn("vps1-production-voice-dispatcher.service",release.UNITS)
        self.assertNotIn("vps1-production-voice-drive-spooler.service",release.UNITS)
        files={str(p) for p in release.FILES}
        self.assertIn("part01/queue_actions.py",files)
        self.assertIn("part01/templates/voice_studio.html",files)
        self.assertIn("part02/kokoro_worker.py",files)
        self.assertIn("part03/edge_worker.py",files)

    def test_runtime_guard_and_no_default_auto_script_dispatch(self):
        root=Path(__file__).resolve().parents[1]
        runtime=(root/"production/runtime.py").read_text()
        self.assertIn("[vps1-voice-tts-controls-only]",runtime)
        script=(root/"production/voice_tts_controls_release.py").read_text()
        self.assertIn("if stopped or switched:",script)
        self.assertIn("verify_queues()",script)
        self.assertIn("backup_sqlite(source, journal",script)
        self.assertNotIn("workers.stop(APP)",script)
        self.assertNotIn("shutil.rmtree(",script)


if __name__=="__main__":
    unittest.main()

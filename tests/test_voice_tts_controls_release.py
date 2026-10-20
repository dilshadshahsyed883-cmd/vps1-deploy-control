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

    def test_active_synthesis_jobs_are_never_interrupted(self):
        for state in release.BUSY:
            with self.subTest(state=state):
                self.mark(self.paths[0],state,"waiting_generation")
                with self.assertRaisesRegex(RuntimeError,"active synthesis/transfer"):
                    release.check_idle_and_lock(self.paths)
                self.mark(self.paths[0],"completed","cleaned")

    def test_durable_waiting_jobs_admitted_without_mutation(self):
        for state in ("queued","retry_wait","paused"):
            with self.subTest(state=state):
                self.mark(self.paths[0],state,"waiting_generation")
                fences=release.check_idle_and_lock(self.paths)
                release.unlock(fences)
                with sqlite3.connect(self.paths[0]) as c:
                    result=c.execute("select status from jobs").fetchone()[0]
                self.assertEqual(result,state)
                self.mark(self.paths[0],"completed","cleaned")
        for delivery in ("pending","retry_pending"):
            with self.subTest(delivery=delivery):
                self.mark(self.paths[1],"completed",delivery)
                fences=release.check_idle_and_lock(self.paths)
                release.unlock(fences)
                with sqlite3.connect(self.paths[1]) as c:
                    self.assertEqual(c.execute("select delivery_status from jobs").fetchone()[0],delivery)
                self.mark(self.paths[1],"completed","cleaned")

    def test_active_drive_delivery_blocks_worker_restart(self):
        for state in release.DELIVERY_BUSY:
            with self.subTest(state=state):
                self.mark(self.paths[1],"completed",state)
                with self.assertRaisesRegex(RuntimeError,"active/queued synthesis"):
                    release.check_idle_and_lock(self.paths)
                self.mark(self.paths[1],"completed","cleaned")

    def test_omnivoice_active_work_is_blocked_and_idle_is_fenced(self):
        clone=Path(self.folder.name)/"clone.sqlite"
        with sqlite3.connect(clone) as c:
            c.execute("create table jobs(job_id text,status text,delivery_status text)")
            c.execute("insert into jobs values('OMNI','processing','waiting_generation')")
        with self.assertRaisesRegex(RuntimeError,"OmniVoice generation"):
            release.check_omnivoice_idle(clone)
        with sqlite3.connect(clone) as c:
            c.execute("update jobs set status='completed',delivery_status='cleaned'")
        fence=release.check_omnivoice_idle(clone)
        self.assertTrue(fence.in_transaction)
        release.unlock([fence])
        with sqlite3.connect(clone) as c:
            c.execute("update jobs set status='completed'")
        with sqlite3.connect(clone) as c:
            c.execute("update jobs set delivery_status='uploading'")
        with self.assertRaisesRegex(RuntimeError,"OmniVoice generation"):
            release.check_omnivoice_idle(clone)
        # Queued Drive upload is not in-flight; SQLite fence still prevents claim.
        with sqlite3.connect(clone) as c:
            c.execute("update jobs set delivery_status='pending'")
        connection=release.check_omnivoice_idle(clone)
        release.unlock([connection])

    def test_aux_units_were_included_in_release_guard(self):
        self.assertEqual(len(release.AUX_WORKERS),2)
        self.assertTrue(all("voice-" in unit for unit in release.AUX_WORKERS))
        script=(Path(__file__).resolve().parents[1]/"production/voice_tts_controls_release.py").read_text()
        self.assertIn("restore_aux_workers(auxiliary_states)",script)
        self.assertIn("fences.append(check_omnivoice_idle(OMNI_DB))",script)
        self.assertLess(script.index('local_endpoint(engine, "jobs?limit=1")'),
                        script.index('for unit in WORKERS:', script.index("def start_all()")))

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
        self.assertIn("delivered",release.DELIVERY_BUSY)

    def test_runtime_guard_and_no_default_auto_script_dispatch(self):
        root=Path(__file__).resolve().parents[1]
        runtime=(root/"production/runtime.py").read_text()
        self.assertIn("[vps1-voice-tts-controls-only]",runtime)
        script=(root/"production/voice_tts_controls_release.py").read_text()
        self.assertIn("if stopping or switched:",script)
        self.assertIn("verify_queues()",script)
        self.assertIn("backup_sqlite(source, journal",script)
        self.assertNotIn("workers.stop(APP)",script)
        self.assertNotIn("shutil.rmtree(",script)
        workflow=(root/".github/workflows/vps1-voice-queue-api-release.yml").read_text()
        self.assertIn("[vps1-voice-tts-controls-release]",workflow)
        self.assertIn("[vps1-voice-tts-controls-only]",workflow)
        self.assertIn("PASS_VOICE_TTS_CONTROLS",workflow)
        self.assertIn("SOURCE_RELEASE_MODE=controls",workflow)
        edge=(root/"apps/voice-studio/part03/edge_worker.py").read_text()
        self.assertIn("WHERE job_id=? AND status='processing'",edge)


if __name__=="__main__":
    unittest.main()

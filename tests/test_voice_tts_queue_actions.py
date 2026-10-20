"""Isolated SQLite, concurrency and source-contract regressions for Voice queues."""
import importlib.util
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "apps/voice-studio/part01/queue_actions.py"
SPEC = importlib.util.spec_from_file_location("voice_queue_actions", PATH)
queue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue)


class QueueActionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = {}
        for engine, ident in (("kokoro", "KOJ-" + "A"*16), ("edge", "EDJ-" + "B"*16)):
            path = Path(self.temp.name) / (engine + ".sqlite")
            self.paths[engine] = (path, ident)
            with self.connect(engine) as db:
                db.execute("""create table jobs(job_id text primary key,status text not null,
                    delivery_status text not null,created_at text,error text,
                    wav_file text,mp3_file text)""")
                db.execute("insert into jobs(job_id,status,delivery_status,created_at) values(?,?,?,?)",
                           (ident, "queued", "waiting_generation", "2026-10-20"))
            with self.connect(engine) as db:
                queue.migrate(db, engine)
                queue.migrate(db, engine)

    def connect(self, engine):
        db = sqlite3.connect(str(self.paths[engine][0]), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("pragma busy_timeout=10000")
        return db

    def change(self, engine, state=None, delivery=None, attempts=None):
        with self.connect(engine) as db:
            updates = []
            vals = []
            if state is not None:updates.append("status=?");vals.append(state)
            if delivery is not None:updates.append("delivery_status=?");vals.append(delivery)
            if attempts is not None:updates.append("attempt=?");vals.append(attempts)
            if updates:db.execute("update jobs set "+",".join(updates),vals)

    def action(self, engine, command):
        with self.connect(engine) as db:
            return queue.apply(db, engine, self.paths[engine][1], command)

    def test_queue_pause_resume_cancel_isolation(self):
        self.assertEqual(self.action("kokoro","pause")["status"],"paused")
        self.assertEqual(self.action("kokoro","pause")["idempotent"],True)
        self.assertEqual(self.action("kokoro","resume")["status"],"queued")
        self.assertEqual(self.action("kokoro","cancel")["status"],"cancelled")
        self.assertEqual(self.action("kokoro","cancel")["idempotent"],True)
        with self.connect("edge") as db:
            self.assertEqual(db.execute("select status from jobs").fetchone()["status"],"queued")

    def test_processing_pause_engine_specific(self):
        self.change("kokoro",state="processing")
        with self.assertRaises(queue.QueueActionError) as ctx:
            self.action("kokoro","pause")
        self.assertEqual(ctx.exception.code,409)
        self.assertEqual(self.action("kokoro","cancel")["status"],"cancel_requested")
        self.change("edge",state="processing")
        self.assertEqual(self.action("edge","pause")["status"],"pause_requested")
        self.assertEqual(self.action("edge","cancel")["status"],"cancel_requested")

    def test_skip_only_queued(self):
        self.assertEqual(self.action("edge","skip")["status"],"skipped")
        self.assertTrue(self.action("edge","skip")["idempotent"])
        with self.assertRaisesRegex(queue.QueueActionError,"Cannot resume"):
            self.action("edge","resume")

    def test_manual_retry_is_durable_and_bounded(self):
        self.change("edge",state="failed",attempts=1)
        result=self.action("edge","retry")
        self.assertEqual(result["status"],"retry_wait")
        self.assertTrue(self.action("edge","retry")["idempotent"])
        with self.connect("edge") as db:
            r=db.execute("select retry_at,error from jobs").fetchone()
            self.assertGreater(r["retry_at"],0)
        self.change("kokoro",state="failed",attempts=3)
        with self.assertRaises(queue.QueueActionError) as ctx:
            self.action("kokoro","retry")
        self.assertEqual(ctx.exception.code,409)

    def test_history_only_delete_requires_delivered_completion(self):
        self.change("kokoro",state="completed",delivery="retry_pending")
        with self.assertRaises(queue.QueueActionError):
            self.action("kokoro","delete")
        self.change("kokoro",delivery="delivered")
        result=self.action("kokoro","delete")
        self.assertTrue(result["hidden"])
        with self.connect("kokoro") as db:
            self.assertIsNotNone(db.execute("select hidden_at from jobs").fetchone()["hidden_at"])
            self.assertEqual(db.execute("select count(*) from jobs").fetchone()[0],1)
        with self.assertRaises(queue.QueueActionError) as ctx:self.action("kokoro","delete")
        self.assertEqual(ctx.exception.code,404)

    def test_delivery_retry_is_not_synthesis_retry(self):
        self.change("edge",state="completed",delivery="retry_pending")
        with self.connect("edge") as db:
            view=queue.job_view(db.execute("select * from jobs").fetchone(),"edge")
        self.assertEqual(view["queue_status"],"completed")
        self.assertNotIn("retry",view["allowed_actions"])

    def test_invalid_or_cross_engine_identifiers(self):
        with self.connect("edge") as db:
            with self.assertRaises(queue.QueueActionError) as ctx:
                queue.apply(db,"edge",self.paths["kokoro"][1],"cancel")
            self.assertEqual(ctx.exception.code,404)
        with self.assertRaises(queue.QueueActionError):
            self.action("kokoro","gdrive_delete")

    def test_parallel_actions_transactionally_serialized(self):
        results=[]
        barrier=threading.Barrier(2)
        def worker():
            barrier.wait()
            try:results.append(self.action("edge","pause"))
            except BaseException as e:results.append(e)
        threads=[threading.Thread(target=worker) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=10)
        self.assertEqual(len(results),2)
        self.assertFalse(any(isinstance(x,Exception) for x in results))
        self.assertEqual(sorted(x["idempotent"] for x in results),[False,True])

    def test_source_is_scoped_and_workers_cooperate(self):
        k=(ROOT/"apps/voice-studio/part02/kokoro_worker.py").read_text()
        e=(ROOT/"apps/voice-studio/part03/edge_worker.py").read_text()
        for filename,engine in (("kokoro_api.py","kokoro"),("edge_api.py","edge")):
            src=(ROOT/"apps/voice-studio/part01"/filename).read_text()
            self.assertIn("apply_queue(c, '"+engine+"'",src)
            self.assertIn("migrate_queue(c, '"+engine+"'",src)
            self.assertIn("hidden_at is null",src)
        self.assertIn("cancel_requested",k)
        self.assertIn("staging=out.with_name(out.name+'.part')",k)
        self.assertIn("status='paused' where status='pause_requested'",e)
        self.assertIn("status='cancelled'",e)

    def test_ui_exposes_only_backend_authorized_buttons(self):
        html=(ROOT/"apps/voice-studio/part01/templates/voice_studio.html").read_text()
        self.assertIn("j.allowed_actions||[]",html)
        self.assertIn("Audio and Drive files are NOT deleted.",html)
        self.assertIn("Drive: ",html)
        # Guard Nginx sub_filter issue only in the two engine scripts;
        # Audio Downloader is independently owned and intentionally untouched.
        k=html.split('id="voiceStudioDualEngineV1Script"',1)[1].split('</script>',1)[0]
        e=html.split('id="voiceStudioEdgeEngineV1Script"',1)[1].split('</script>',1)[0]
        self.assertNotIn("apiRoot+'/jobs'",k)
        self.assertNotIn("root+'/jobs'",e)


if __name__ == "__main__":
    unittest.main()

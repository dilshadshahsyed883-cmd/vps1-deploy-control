"""Offline safety tests for one-shot Scene FD incident cancellation."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from production import scene_exact_job_cancel_20261027 as incident

ROOT = Path(__file__).resolve().parents[1]


class SceneTargetedCancel(unittest.TestCase):
    def _setup_files(self, directory):
        root = Path(directory)
        record = root / "owner.json"
        config = root / "private.env"
        record.write_text(json.dumps({
            "owner": "scene-production-worker-v1",
            "unit": incident.worker.UNIT,
            "state": str(incident.worker.STATE),
            "endpoint": incident.worker.URL,
        }), encoding="utf-8")
        config.write_text(
            "SCENE_WORKER_TOKEN=" + "A"*48 + "\nSCENE_WORKER_ADMISSION_ONLY=0\n",
            encoding="utf-8",
        )
        return record, config

    def test_exact_job_ack_then_confirmed_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, config = self._setup_files(tmp)
            job = {"id": incident.TARGET_JOB, "project_name": incident.TARGET_PROJECT}
            mock_results = [
                {"ok": True, "admission_only": False},
                {**job, "status": "rendering"},
                {**job, "status": "rendering", "cancel_requested": 1},
                {**job, "status": "cancelled"},
            ]
            with (patch.object(incident.worker, "RECORD", record),
                  patch.object(incident.worker, "ENV", config),
                  patch.object(incident.os, "geteuid", return_value=0),
                  patch.object(incident.socket, "gethostname", return_value="vps-01a23801"),
                  patch.object(incident.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)),
                  patch.object(incident, "_fetch", side_effect=mock_results) as fetch):
                self.assertEqual(incident.terminate_exact_job(), "cancelled")
                self.assertEqual([a.args[0] for a in fetch.call_args_list],
                                 ["GET", "GET", "POST", "GET"])
                self.assertEqual(fetch.call_args_list[2].args[1],
                                 "/jobs/" + incident.TARGET_JOB + "/cancel")

    def test_mismatched_project_never_posted(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, config = self._setup_files(tmp)
            results = [
                {"ok": True, "admission_only": False},
                {"id": incident.TARGET_JOB, "project_name": "ANOTHER_PROJECT", "status": "rendering"},
            ]
            with (patch.object(incident.worker, "RECORD", record),
                  patch.object(incident.worker, "ENV", config),
                  patch.object(incident.os, "geteuid", return_value=0),
                  patch.object(incident.socket, "gethostname", return_value="vps-01a23801"),
                  patch.object(incident.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)),
                  patch.object(incident, "_fetch", side_effect=results) as fetch):
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    incident.terminate_exact_job()
                self.assertTrue(all(c.args[0] != "POST" for c in fetch.call_args_list))

    def test_pending_cancel_is_not_falsely_reported_as_terminated(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, config = self._setup_files(tmp)
            job = {"id": incident.TARGET_JOB, "project_name": incident.TARGET_PROJECT}
            results = (
                [{"ok": True, "admission_only": False}, {**job, "status": "rendering"},
                 {**job, "status": "rendering", "cancel_requested": 1}]
                + [{**job, "status": "rendering", "cancel_requested": 1} for _ in range(26)]
            )
            with (patch.object(incident.worker, "RECORD", record),
                  patch.object(incident.worker, "ENV", config),
                  patch.object(incident.os, "geteuid", return_value=0),
                  patch.object(incident.socket, "gethostname", return_value="vps-01a23801"),
                  patch.object(incident.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)),
                  patch.object(incident.time, "sleep"),
                  patch.object(incident, "_fetch", side_effect=results)):
                with self.assertRaisesRegex(RuntimeError, "CANCEL_PENDING"):
                    incident.terminate_exact_job()

    def test_source_dispatch_is_narrow_and_gateway_skips(self):
        runtime = (ROOT / "production/runtime.py").read_text(encoding="utf-8")
        gateway = (ROOT / "production/gateway.py").read_text(encoding="utf-8")
        for source in (runtime, gateway):
            self.assertIn(incident.MARKER, source)
        self.assertIn("args.apps != ['scene-builder']", runtime)
        self.assertIn("SCENE_TARGET_CANCEL_GATEWAY_MUTATIONS=NONE", gateway)


class SceneExactResetQueueContract(unittest.TestCase):
    def _queue(self, tmp, *, project="Eala_Tokyo_V25", status="rendering",
               drive_destination="", other_status=None):
        import sqlite3
        db = Path(tmp) / "queue.sqlite3"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY,project_name TEXT,status TEXT,drive_destination TEXT)")
            conn.execute("INSERT INTO jobs VALUES(?,?,?,?)",
                         (incident.TARGET_JOB,project,status,drive_destination))
            if other_status is not None:
                conn.execute("INSERT INTO jobs VALUES(?,?,?,?)",
                             ("b"*32,"Another Scene Project",other_status,""))
        return db

    def test_target_and_unrelated_terminal_history_allowed(self):
        from production.scene_exact_reset_3327_20261031 import inspect_queue
        with tempfile.TemporaryDirectory() as tmp:
            db = self._queue(tmp, other_status="completed")
            result = inspect_queue(db)
            self.assertEqual(result["id"], incident.TARGET_JOB)
            self.assertEqual(result["status"], "rendering")

    def test_other_active_job_blocks_all_target_mutation(self):
        from production.scene_exact_reset_3327_20261031 import inspect_queue
        with tempfile.TemporaryDirectory() as tmp:
            db = self._queue(tmp, other_status="queued")
            with self.assertRaisesRegex(RuntimeError, "another project"):
                inspect_queue(db)

    def test_never_delete_remote_output(self):
        from production.scene_exact_reset_3327_20261031 import inspect_queue
        with tempfile.TemporaryDirectory() as tmp:
            db = self._queue(tmp, drive_destination="gdrive:SceneBuilder-Renders/movie.mp4")
            with self.assertRaisesRegex(RuntimeError, "Drive destination"):
                inspect_queue(db)

    def test_wrong_identity_or_completed_target_refused(self):
        from production.scene_exact_reset_3327_20261031 import inspect_queue
        with tempfile.TemporaryDirectory() as tmp:
            db = self._queue(tmp, project="Another Person")
            with self.assertRaisesRegex(RuntimeError, "identity"):
                inspect_queue(db)
        with tempfile.TemporaryDirectory() as tmp:
            db = self._queue(tmp, status="completed")
            with self.assertRaisesRegex(RuntimeError, "target completed"):
                inspect_queue(db)

    def test_exact_reset_no_shared_gateway_or_generic_install(self):
        root = Path(__file__).resolve().parents[1]
        runtime = (root / "production/runtime.py").read_text()
        gateway = (root / "production/gateway.py").read_text()
        from production.scene_exact_reset_3327_20261031 import MARKER
        self.assertIn(MARKER, runtime)
        self.assertIn(MARKER, gateway)
        self.assertIn("args.apps != ['scene-builder']", runtime)
        self.assertIn("SCENE_EXACT_RESET_GATEWAY_MUTATIONS=NONE", gateway)
        self.assertIn("if 'scene-builder' not in args.apps:", gateway)


if __name__ == "__main__":
    unittest.main()

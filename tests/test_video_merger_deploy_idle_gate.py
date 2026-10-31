"""Regression for Video Merger's slow /api/state production idle check.

The live UI endpoint can wait for a recursive Google Drive rclone listing.
Deployment must use the running service's local SQLite file, strictly read-only,
and refuse all queued/in-progress merge and Library work.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import hashlib
import sqlite3

from production import video_merger_import_release as imported
from production import video_template_release as gate


class VideoMergerDeployIdle(TestCase):
    def make_db(self, root, jobs=("complete",), library=("ready",)):
        path = root / "data" / "video_merger.db"
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE jobs(status TEXT NOT NULL)")
            connection.execute("CREATE TABLE library_items(status TEXT NOT NULL)")
            connection.executemany("INSERT INTO jobs VALUES(?)", ((v,) for v in jobs))
            connection.executemany("INSERT INTO library_items VALUES(?)", ((v,) for v in library))
        return path

    def probe(self, directory):
        root = Path(directory)
        with patch.object(gate, "DATA", root.parent), \
             patch.object(imported, "live_paths", return_value=(root, root / "config/proxy_profiles.json")) as resolver, \
             patch.object(imported, "event"), \
             patch.object(gate, "read_json", side_effect=AssertionError("Drive-backed UI endpoint must never be queried")):
            gate.idle("video-merger")
            resolver.assert_called_once_with(root.parent / "video-merger/video-merger")

    def test_completed_jobs_and_ready_library_are_read_only_and_no_drive_call(self):
        with TemporaryDirectory() as temp:
            root = Path(temp) / "video-merger"
            file = self.make_db(root, jobs=("complete", "failed", "cancelled"), library=("ready", "failed"))
            before = hashlib.sha256(file.read_bytes()).hexdigest()
            self.probe(root)
            self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), before)

    def test_active_or_waiting_merge_and_library_import_block_release(self):
        for kind, jobs, library in (
            ("waiting", ("waiting",), ("ready",)),
            ("processing", ("processing",), ("ready",)),
            ("merging", ("merging",), ("ready",)),
            ("uploading", ("uploading",), ("ready",)),
            ("importing", ("complete",), ("importing",)),
        ):
            with self.subTest(kind=kind), TemporaryDirectory() as temp:
                root = Path(temp) / "video-merger"
                self.make_db(root, jobs=jobs, library=library)
                with self.assertRaisesRegex(RuntimeError, "unfinished"):
                    self.probe(root)

    def test_missing_database_and_missing_table_fail_closed(self):
        with TemporaryDirectory() as temp:
            root = Path(temp) / "video-merger"
            with self.assertRaisesRegex(RuntimeError, "DB missing"):
                self.probe(root)
            path = root / "data" / "video_merger.db"
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as con:
                con.execute("CREATE TABLE jobs(status TEXT)")
            with self.assertRaisesRegex(RuntimeError, "schema invalid"):
                self.probe(root)

    def test_other_studio_idle_path_is_unchanged(self):
        with patch.object(gate, "read_json", return_value={"jobs":[]}) as api, \
             patch.object(imported, "idle_database", side_effect=AssertionError("short maker should use its own API")):
            gate.idle("short-maker")
            api.assert_called_once_with("http://127.0.0.1:8146/api/jobs")

    def test_generic_runtime_keeps_strict_idle_gate_before_stopping_service(self):
        source = (Path(__file__).resolve().parents[1] / "production/runtime.py").read_text()
        self.assertEqual(source.count("require_video_merger_idle('video-merger')"), 2)
        for occurrence in source.split("require_video_merger_idle('video-merger')")[1:]:
            self.assertIn("if active:", occurrence[:1800])
            self.assertIn("run(['systemctl', 'stop', unit_name]", occurrence[:1800])


if __name__ == "__main__":
    import unittest
    unittest.main()

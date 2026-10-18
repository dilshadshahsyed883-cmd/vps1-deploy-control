"""Security and mode-isolation tests: no real jobs, tokens, or uploads."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "apps" / "local-upload-manager" / "part01"
sys.path.insert(0, str(SOURCE))
from lum_engine.four_modes import registry, validate_mode


def seed(root, filename="video.mp4", channel="UC_example"):
    pc = root / "core" / "data" / "uploader.db"
    v6 = root / "auto_engine_v6" / "auto_engine_v6.sqlite3"
    pc.parent.mkdir(parents=True, exist_ok=True)
    v6.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(pc) as c:
        for table in ("auto_engine_profiles", "channels", "videos", "auto_engine_runs"):
            c.execute(f"CREATE TABLE {table} (id INTEGER)")
            c.execute(f"INSERT INTO {table} (id) VALUES (1)")
    with sqlite3.connect(v6) as c:
        c.execute("CREATE TABLE upload_records(channel_id TEXT, normalized_filename TEXT, script_key TEXT)")
        c.execute("INSERT INTO upload_records VALUES(?,?,?)", (channel, filename, "video"))
    return pc, v6


class FourModesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="four-mode-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = {
            "LUM_DATA_DIR": str(self.root / "lum1"),
            "LUM_MODE_ENGINE2_ROOT": str(self.root / "lum2"),
            "LUM_MODE_SHORTS_ROOT": str(self.root / "lum3"),
            "LUM_MODE_SHORT_STREAM_ROOT": str(self.root / "stream"),
        }
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_exact_mode_allowlist(self):
        self.assertEqual(validate_mode("engine1"), "engine1")
        self.assertEqual(validate_mode("short_stream"), "short_stream")
        for invalid in ("../../etc", "engine4", "engine2/../../", "longs"):
            with self.assertRaises(ValueError):
                validate_mode(invalid)

    def test_unprovisioned_modes_never_create_databases(self):
        modes = registry()["selected_modes"]
        self.assertEqual(set(modes), {"engine1", "engine2", "shorts", "short_stream"})
        for name in ("engine1", "engine2", "shorts"):
            self.assertEqual(modes[name]["status"], "not_provisioned")
            self.assertFalse(modes[name]["write_enabled"])
        self.assertFalse((self.root / "lum2").exists())

    def test_isolated_profiles_queues_schedules_channels_and_history(self):
        first = seed(self.root / "lum1")
        second = seed(self.root / "lum2", "other.mp4", "different_channel")
        prior_sizes = {str(path): path.stat().st_size for path in first + second}
        info = registry()
        self.assertEqual(info["cross_mode_record_collisions"], 0)
        for name in ("engine1", "engine2"):
            stats = info["selected_modes"][name]["stats"]
            self.assertEqual(stats, {"profiles": 1, "channels": 1, "queue": 1, "schedules": 1, "upload_records": 1})
        self.assertEqual(info["selected_modes"]["shorts"]["stats"]["queue"], 0)
        self.assertTrue(info["selected_modes"]["engine1"]["write_enabled"])
        self.assertFalse(info["selected_modes"]["engine2"]["write_enabled"])
        self.assertEqual(prior_sizes, {str(path): path.stat().st_size for path in first + second})

    def test_cross_mode_duplicate_receipts_trigger_warning(self):
        seed(self.root / "lum1")
        seed(self.root / "lum2")
        info = registry()
        self.assertGreater(info["cross_mode_record_collisions"], 0)
        self.assertFalse(info["cross_mode_uploads_enabled"])

    def test_mode_roots_cannot_alias_primary(self):
        with patch.dict(os.environ, {"LUM_MODE_ENGINE2_ROOT": str(self.root / "lum1")}):
            with self.assertRaises(RuntimeError):
                registry()

    def test_short_stream_key_never_returned(self):
        stream = self.root / "stream"
        stream.mkdir()
        (stream / "sessions.json").write_text('{"example":{"stream_key":"PRIVATE_STREAM_KEY"}}')
        serialized = json.dumps(registry())
        self.assertNotIn("PRIVATE_STREAM_KEY", serialized)
        self.assertEqual(registry()["selected_modes"]["short_stream"]["sessions"], 1)


if __name__ == "__main__":
    unittest.main()

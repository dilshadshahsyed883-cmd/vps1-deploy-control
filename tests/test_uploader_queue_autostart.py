"""New work in Song, Short and Copyright queues starts without a second click.

Tests are offline: no Youtube publish, production DB, or worker service required.
"""
import ast
import os
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LUM = ROOT / "apps/local-upload-manager/part01/lum_engine"
COPYRIGHT = ROOT / "apps/copyright-uploader/part01/lum_engine"


def load_autostart():
    source = (LUM / "auto_engine_v6.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "auto_start_new_jobs")
    env = {"os": os, "V6Store": object, "Dict": dict, "Any": object}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(LUM / "auto_engine_v6.py"), "exec"), env)
    return env["auto_start_new_jobs"]


class FakeStore:
    def __init__(self, *, manual=None, pause=0):
        self.state = {"manual_profile_ids": manual, "pause_after_current": pause, "running": 0}
        self.engine_changes = []
        self.scope_changes = []

    def engine(self):
        return dict(self.state)

    def set_manual_profile_ids(self, ids):
        self.scope_changes.append(ids)
        self.state["manual_profile_ids"] = ",".join(map(str, ids))

    def set_engine(self, **kwargs):
        self.engine_changes.append(kwargs)
        self.state.update(kwargs)


class QueueAutostartTests(unittest.TestCase):
    def test_new_jobs_arm_only_song_and_short(self):
        fn = load_autostart()
        for mode in ("song", "shortstudio", "copyright", "engine1"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": mode}):
                store = FakeStore()
                result = fn(store, {"id": 7, "enabled": 1}, 2)
                self.assertEqual(result, mode in {"song", "shortstudio"})
                self.assertEqual(bool(store.engine_changes), result)

    def test_manual_scope_adds_only_new_profile_without_losing_existing_scope(self):
        fn = load_autostart()
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "shortstudio"}):
            store = FakeStore(manual="2,4")
            self.assertTrue(fn(store, {"id": 7, "enabled": 1}, 1))
            self.assertEqual(store.scope_changes, [[2, 4, 7]])
            self.assertEqual(store.engine_changes, [{"running": True, "pause_after_current": False}])
            all_enabled = FakeStore(manual=None)
            self.assertTrue(fn(all_enabled, {"id": 7, "enabled": 1}, 1))
            self.assertEqual(all_enabled.scope_changes, [])

    def test_no_replay_on_duplicate_disabled_profile_or_explicit_pause(self):
        fn = load_autostart()
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            for created, enabled, pause in ((0, 1, 0), (1, 0, 0), (1, 1, 1)):
                with self.subTest(created=created, enabled=enabled, pause=pause):
                    store = FakeStore(manual="4", pause=pause)
                    self.assertFalse(fn(store, {"id": 7, "enabled": enabled}, created))
                    self.assertFalse(store.scope_changes)
                    self.assertFalse(store.engine_changes)

    def test_all_auto_engine_admission_paths_delegate_to_autostart(self):
        source = (LUM / "auto_engine_v6.py").read_text(encoding="utf-8")
        source = source[source.index("def auto_start_new_jobs("):]
        self.assertIn("auto_start_new_jobs(store, profile, queued[\"created\"])", source)
        self.assertIn("auto_start_new_jobs(store, profile, int(result.get(\"created\") or 0))", source)
        self.assertIn("return result", source)
        worker = (LUM / "auto_engine_v6_worker.py").read_text(encoding="utf-8")
        self.assertIn("db.activate_due_scheduled_jobs(auto_engine_only=True)", worker)
        self.assertIn("allowed_auto_engine_profile_ids", worker)
        self.assertIn("claim_available_jobs(", worker)

    def test_serial_add_video_autostarts_without_disabling_explicit_pause(self):
        for app, mode in ((LUM, "song"), (LUM, "shortstudio"), (COPYRIGHT, "copyright")):
            with self.subTest(mode=mode):
                src = (app / "standalone_app.py").read_text(encoding="utf-8")
                section = src.split('@app.post(route("/actions/add-video"))', 1)[1].split('@app.post(', 1)[0]
                self.assertIn('project_id = store.create_project({', section)
                self.assertIn('store.setting("pause_after_current", "0") != "1"', section)
                self.assertIn('store.set_setting("engine_enabled", "1")', section)
                self.assertLess(section.index('project_id = store.create_project({'),
                                section.index('store.set_setting("engine_enabled", "1")'))
                if mode == "copyright":
                    self.assertIn('upload_instance == "copyright"', section)
                else:
                    self.assertIn('upload_instance in {"song", "shortstudio"}', section)

    def test_copyright_remains_manual_serial_queue_not_auto_engine(self):
        worker = (COPYRIGHT / "worker.py").read_text(encoding="utf-8")
        self.assertIn('self.store.claim_next_eligible()', worker)
        self.assertIn('self.store.setting("engine_enabled", "1")', worker)
        self.assertIn('if self.store.setting("pause_after_current", "0") == "1":', worker)


if __name__ == "__main__":
    unittest.main()

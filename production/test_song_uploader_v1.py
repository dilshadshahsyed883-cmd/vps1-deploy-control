"""Song Uploader V1: offline source isolation and authenticated UI contracts.

No external uploads, production files, OAuth, credentials, queues or networks.
"""
from pathlib import Path
import ast
import unittest
from unittest.mock import patch

from production import workers

ROOT = Path(__file__).resolve().parents[1]
LUM = ROOT / "apps/local-upload-manager/part01"
SCENE = ROOT / "apps/scene-builder/part01"


class SongUploaderContracts(unittest.TestCase):
    def test_dedicated_three_processes_and_owned_root(self):
        specs = workers.SPECS["local-upload-manager"]
        names = [name for name, _, _ in specs]
        self.assertEqual([name for name in names if name.startswith("lum-song-")],
                         ["lum-song-web", "lum-song-worker", "lum-song-scheduler"])
        self.assertEqual(workers.MODE_ROOTS["song"], "lum-song")
        with patch.object(workers, "DATA", Path("/tmp/vps1-contract-only")):
            command = workers.mode_exec("lum-song-web", Path("/venv/bin/python"),
                                        "-m uvicorn lum_engine.standalone_app:app --port 8018")
        for expected in ("LUM_ENGINE_INSTANCE=song",
                         "LUM_BASE_PATH=/local-upload-manager/modes/song",
                         "LUM_DATA_DIR=/tmp/vps1-contract-only/local-upload-manager/lum-song",
                         "YOUTUBE_UPLOAD_MANAGER_DATA_DIR=/tmp/vps1-contract-only/local-upload-manager/lum-song/core"):
            self.assertIn(expected, command)
        self.assertNotIn("lum2/core", command)
        self.assertNotIn("lum3/core", command)

    def test_mode_is_internal_to_existing_authenticated_gateway(self):
        source = (LUM / "lum_engine/mode_gateway.py").read_text()
        self.assertIn('"song": 8018', source)
        self.assertIn('headers = {', source)
        self.assertIn('"x-auth-user": str(user["username"])', source)
        self.assertIn('if engine_mode not in ENGINE_PORTS:', (LUM / "lum_engine/standalone_app.py").read_text())

    def test_only_five_main_tabs_and_four_engine_panels(self):
        template = (LUM / "templates/app.html").read_text()
        self.assertIn("engine_instance not in ['song', 'shortstudio']", template)
        self.assertIn("engine_instance not in ['song', 'shortstudio']", template)
        self.assertIn('"dashboard", "channels", "sheets", "auto_engine", "logs"',
                      (LUM / "lum_engine/standalone_app.py").read_text())
        for panel in ("automation", "profiles", "queue", "records"):
            self.assertIn('data-ae-panel-target="' + panel + '"', template)
        self.assertIn('Song Uploader · Short Uploader Auto Engine', template)
        self.assertIn('Short Uploader Auto Engine workflow:', template)
        self.assertIn('Matching JPG/PNG thumbnails are required for Song Uploader.', template)
        self.assertNotIn('Song Uploader · Engine 2', template)
        try:
            from jinja2 import Environment
        except ImportError:
            pass
        else:
            Environment().parse(template)

    def test_unchanged_three_modes_for_original_auto_uploader(self):
        template = (LUM / "templates/app.html").read_text()
        for name in ("engine1", "engine2"):
            self.assertIn("('" + name + "', '", template)
        self.assertNotIn("('shorts', 'Short Uploader')", template)
        self.assertIn('aria-label="Auto Uploader modes"', template)
        self.assertIn("engine_instance not in ['song', 'shortstudio']", template)
        self.assertIn("if engine_instance in {'engine2', 'shorts', 'song', 'shortstudio'}",
                      (LUM / "lum_engine/standalone_app.py").read_text().replace('"', "'"))

    def test_thumbnail_required_at_ui_api_and_preflight(self):
        source = (LUM / "lum_engine/standalone_app.py").read_text()
        preflight = (LUM / "desktop_core/modules/preflight.py").read_text()
        template = (LUM / "templates/app.html").read_text()
        self.assertIn('upload_instance == "song" and not has_thumbnail', source)
        self.assertIn('== "song" and not thumb_raw', preflight)
        self.assertIn("['engine2', 'song']", template)
        self.assertIn('not in {"shorts", "shortstudio"}', (LUM / "lum_engine/auto_engine_v6.py").read_text())


    def test_song_reuses_short_uploader_channel_workflow_without_sharing_data(self):
        """Shared V6 channel queue/worker/scheduler, with Song-only thumbnail policy."""
        specs = {name: command for name, _, command in workers.SPECS["local-upload-manager"]}
        self.assertEqual(specs["lum-shorts-worker"], specs["lum-song-worker"])
        self.assertEqual(specs["lum-shorts-scheduler"], specs["lum-song-scheduler"])
        self.assertEqual(workers.MODE_ROOTS["shorts"], "lum3")
        self.assertEqual(workers.MODE_ROOTS["song"], "lum-song")
        engine_source = (LUM / "lum_engine/auto_engine_v6.py").read_text()
        engine_tree = ast.parse(engine_source)
        thumbnail_fn = next(
            node for node in engine_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "thumbnail_required_for_mode"
        )
        import os
        namespace = {"os": os}
        exec(compile(ast.Module(body=[thumbnail_fn], type_ignores=[]), "<thumbnail-policy>", "exec"), namespace)
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "shorts"}):
            self.assertFalse(namespace["thumbnail_required_for_mode"]())
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            self.assertTrue(namespace["thumbnail_required_for_mode"]())
        self.assertIn("Song shares the Short Uploader Auto Engine flow", engine_source)
        queue_worker = (LUM / "lum_engine/auto_engine_v6_worker.py").read_text()
        self.assertIn("strict_thumbnail", queue_worker)
        ui = (LUM / "templates/app.html").read_text()
        self.assertIn("Thumbnail Drive Folder URL / ID (required)", ui)
        self.assertIn("engine_instance not in ['shorts', 'shortstudio']", ui)
        self.assertIn('data-ae-panel-target="profiles"', ui)
        self.assertIn('data-ae-panel-target="queue"', ui)

    def test_song_thumbnail_incomplete_never_archives_sources(self):
        """Exercise the actual reconciliation function without provider or filesystem access."""
        worker = (LUM / "lum_engine/auto_engine_v6_worker.py").read_text()
        tree = ast.parse(worker)
        fn = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef) and node.name == "reconcile_completed")
        ns = {"Database": object, "V6Store": object,
              "clean": lambda value: str(value or "").strip()}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<song-reconcile>", "exec"), ns)

        class Db:
            def __init__(self, stage):
                self.stage = stage

            def get_video(self, video_id):
                assert video_id == 73
                return {"status": "UPLOADED", "youtube_video_id": "verified-id",
                        "thumbnail_stage_state": self.stage}

        class Store:
            def __init__(self):
                self.states = []

            def job_source(self, video_id):
                return {"filename": "safe.mp4", "source_folder_id": "unmodified"}

            def channel_job_meta(self, video_id):
                return {"strict_thumbnail": 1, "thumbnail_filename": "safe.jpg"}

            def set_job_state(self, video_id, state, **kwargs):
                self.states.append((video_id, state, kwargs))

        for stage in ("", "PENDING", "WARNING", "SKIPPED", "FAILED"):
            with self.subTest(thumbnail_stage=stage):
                store = Store()
                ns["reconcile_completed"](Db(stage), store, 73)
                self.assertEqual(len(store.states), 1)
                self.assertEqual(store.states[0][:2], (73, "POST_UPLOAD_REVIEW"))
                self.assertIn("sources retained", store.states[0][2]["error"])

        # The worker fallback cannot call reconciliation for a channel job
        # whose thumbnail stage has not been accepted by _channel_result_handled.
        fallback = worker.split('if channel_meta and _channel_result_handled(db,store,vid):', 1)[1]
        fallback = fallback.split('elif clean(row.get("status")).upper()=="FAILED":', 1)[0]
        self.assertIn('if channel_meta:', fallback)
        self.assertIn('Thumbnail publishing status pending; sources retained.', fallback)

    def test_song_studio_authenticated_link_and_no_auto_enable(self):
        scene = (SCENE / "app.py").read_text()
        shell = (SCENE / "static/vps1_native_site_shell.js").read_text()
        self.assertIn('if studio_app == "song-uploader":', scene)
        self.assertIn('return RedirectResponse("/song-uploader", status_code=303)', scene)
        self.assertIn('pathname.startsWith("/local-upload-manager/modes/song/")', shell)
        config = (LUM / "lum_engine/config.py").read_text()
        self.assertIn('{"engine2", "shorts", "song", "shortstudio"} else "1"', config)
        for path in (SCENE / "app.py", LUM / "lum_engine/standalone_app.py",
                     LUM / "desktop_core/modules/preflight.py", ROOT / "production/workers.py"):
            ast.parse(path.read_text(), filename=str(path))


if __name__ == "__main__":
    unittest.main()

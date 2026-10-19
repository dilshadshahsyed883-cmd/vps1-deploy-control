"""Offline ISS-09 regression: three uploading modes, independent Auto Stream, thumbnails.
No user databases, OAuth, upload jobs, streams or network are touched.
"""
from __future__ import annotations
import ast
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "apps/local-upload-manager/part01"


class ThreeModeSplitTests(TestCase):
    def test_jinja_template_parses_in_real_uploader_runtime(self):
        try:
            from jinja2 import Environment
        except ModuleNotFoundError:
            self.skipTest("Uploader Jinja dependency is installed in the Local Upload Manager CI lane")
        Environment().parse((ROOT / "templates/app.html").read_text())

    def test_ui_has_two_primary_tabs_and_only_three_uploader_modes(self):
        ui = (ROOT / "templates/app.html").read_text()
        self.assertIn('aria-label="Auto Uploader main subtabs"', ui)
        self.assertIn('href="/auto-stream/"', ui)
        self.assertIn("['engine1', 'engine2', 'shorts']", ui)
        self.assertNotIn("['engine1', 'engine2', 'shorts', 'short_stream']", ui)
        self.assertIn("No stream is started or changed by opening this tab", ui)
        self.assertIn('href="/short-stream/"', ui)

    def test_real_http_smoke_checks_new_navigation(self):
        smoke = (Path(__file__).resolve().parents[1] / "production/smoke.py").read_text()
        self.assertIn("for mode in ('engine1', 'engine2', 'shorts'):", smoke)
        self.assertIn("stream_page = client.get(spec['ui'] + '?tab=auto_stream')", smoke)
        self.assertIn("legacy stream bookmark did not redirect", smoke)

    def test_independent_services_preserved_and_stream_not_a_fourth_upload_mode(self):
        source = (ROOT / "lum_engine/standalone_app.py").read_text()
        self.assertIn('"auto_stream"}', source)
        self.assertIn('if selected == "auto_stream":', source)
        self.assertIn('elif selected_auto_mode == "short_stream":', source)
        self.assertIn('?tab=auto_stream', source)

    def test_mode_specific_thumbnail_contract(self):
        source = (ROOT / "lum_engine/auto_engine_v6.py").read_text()
        parsed = ast.parse(source)
        f = next(n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name == "thumbnail_required_for_mode")
        mod = ast.Module(body=[f], type_ignores=[])
        namespace = {"os": os}
        exec(compile(mod, str(ROOT / "lum_engine/auto_engine_v6.py"), "exec"), namespace)
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "engine1"}):
            self.assertTrue(namespace["thumbnail_required_for_mode"]())
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "engine2"}):
            self.assertTrue(namespace["thumbnail_required_for_mode"]())
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "shorts"}):
            self.assertFalse(namespace["thumbnail_required_for_mode"]())
        self.assertIn("if thumbnail_required_for_mode():\n        store.scan_drive_thumbnails(pid)", source)
        self.assertIn("int(thumbnail_required_for_mode())", source)
        self.assertIn("thumbnail_required_for_mode() and not t", source)
        worker = (ROOT / "lum_engine/auto_engine_v6_worker.py").read_text()
        self.assertIn('int(channel_meta.get("strict_thumbnail") or 0)', worker)
        self.assertIn('if clean(channel_meta.get("thumbnail_drive_file_id")):', worker)

    def test_http_and_pipeline_enforce_engine2_and_shorts(self):
        server = (ROOT / "lum_engine/standalone_app.py").read_text()
        self.assertIn('upload_instance == "engine2" and not has_thumbnail', server)
        self.assertIn('upload_instance == "shorts" and has_thumbnail', server)
        preflight = (ROOT / "desktop_core/modules/preflight.py").read_text()
        self.assertIn('== "engine2" and not thumb_raw', preflight)
        for path in (
            "lum_engine/standalone_app.py",
            "lum_engine/auto_engine_v6.py",
            "lum_engine/auto_engine_v6_worker.py",
            "desktop_core/modules/preflight.py",
        ):
            with self.subTest(path=path):
                ast.parse((ROOT / path).read_text())

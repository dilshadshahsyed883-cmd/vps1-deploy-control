"""Song Uploader settings are isolated from legacy bulk mode and pass the candidate gate."""
from __future__ import annotations
import ast
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/app.html"
BACKEND = ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py"
POLICY = ROOT / "apps/scene-builder/part01/precutover_policy.py"

class SongUploaderSettingsContract(TestCase):
    def test_dashboard_omits_bulk_toggle_only_for_song(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        start = source.index('<div class="lum-grid lum-two">', source.index("{% elif tab == 'dashboard' %}"))
        end = source.index("{{ reports_panel() }}", start)
        fragment = Environment(autoescape=True).from_string(source[start:end])
        for instance, expected_bulk in (("song", False), ("engine1", True), ("shortstudio", True)):
            data = {
                "engine_instance": instance,
                "base_path": "/local-upload-manager",
                "csrf": "test-csrf",
                "stats": {
                    "engine_enabled": False,
                    "settings": {
                        "sheets_sync_enabled": "1", "auto_engine_enabled": "1",
                        "bulk_import_enabled": "1",
                    },
                },
            }
            rendered = fragment.render(**data)
            self.assertEqual('name="bulk_import_enabled"' in rendered, expected_bulk)
            self.assertIn('name="sheets_sync_enabled"', rendered)
            self.assertIn('name="auto_engine_enabled"', rendered)
            self.assertIn('name="csrf_token"', rendered)
            self.assertIn("/actions/settings", rendered)
            if instance == "song":
                self.assertNotIn("Bulk Import", rendered)

    def test_song_save_forces_disabled_bulk_without_cross_instance_mutation(self):
        tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
        method = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "save_settings")
        method.decorator_list = []
        code = compile(ast.Module(body=[method], type_ignores=[]), str(BACKEND), "exec")
        class Store:
            def __init__(self):
                self.saved = {}
            def set_setting(self, key, value):
                self.saved[key] = value

        for instance, expected in (("song", "0"), ("engine1", "1")):
            store = Store()
            env = {
                "Request": object, "Form": lambda val: val, "os": os,
                "current_user": lambda request: object(),
                "require_csrf": lambda request, token: self.assertEqual(token, "valid"),
                "require_heavy": lambda user: None,
                "store": store, "redirect": lambda path, **kwargs: (path, kwargs),
            }
            with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": instance}):
                exec(code, env)
                response = env["save_settings"](object(), "valid", "1", "1", "1", "1")
            self.assertEqual(store.saved["bulk_import_enabled"], expected)
            self.assertEqual(store.saved["sheets_sync_enabled"], "1")
            self.assertEqual(store.saved["auto_engine_enabled"], "1")
            self.assertEqual(store.saved["max_active_uploads"], "1")
            self.assertEqual(store.saved["drive_delete_source_after_verified"], "0")
            self.assertEqual(response[0], "/app?tab=dashboard")

    def test_scene_candidate_permits_only_real_song_settings_post(self):
        tree = ast.parse(POLICY.read_text(encoding="utf-8"))
        namespace = {}
        exec(compile(tree, str(POLICY), "exec"), namespace)
        allowed = namespace["mutation_allowed"]
        self.assertTrue(allowed("POST", "/song-uploader/actions/settings", False))
        self.assertFalse(allowed("PUT", "/song-uploader/actions/settings", False))
        self.assertFalse(allowed("POST", "/song-maker/api/queue", False))
        self.assertFalse(allowed("POST", "/song-uploader-evil/actions/settings", False))

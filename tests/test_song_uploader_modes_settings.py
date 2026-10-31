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
    def test_song_dashboard_hides_modes_for_admin_and_nonadmin(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        start = source.index('<div class="lum-grid {{', source.index("{% elif tab == 'dashboard' %}"))
        end = source.index("{% if engine_instance not in ['song', 'shortstudio'] %}{{ reports_panel() }}{% endif %}", start)
        fragment = Environment(autoescape=True).from_string(source[start:end])
        cases = (("song", True, False, False), ("song", False, False, False),
                 ("engine1", True, True, True), ("shortstudio", True, True, False))
        for instance, admin, modes, bulk in cases:
            data = {
                "engine_instance": instance, "user": {"is_admin": admin},
                "base_path": "/local-upload-manager", "csrf": "test-csrf",
                "stats": {"engine_enabled": False, "settings": {
                    "sheets_sync_enabled": "1", "auto_engine_enabled": "1", "bulk_import_enabled": "1"}},
            }
            rendered = fragment.render(**data)
            self.assertIn("Serial engine", rendered)
            self.assertEqual('name="bulk_import_enabled"' in rendered, bulk)
            self.assertEqual('name="sheets_sync_enabled"' in rendered, modes)
            self.assertEqual('name="auto_engine_enabled"' in rendered, modes)
            self.assertEqual("/actions/settings" in rendered, modes)
            if instance == "song":
                self.assertNotIn("Uploader modes", rendered)
                self.assertNotIn("Save modes", rendered)

    def test_song_rejects_stale_settings_post_without_affecting_other_apps(self):
        tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
        method = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "save_settings")
        method.decorator_list = []
        code = compile(ast.Module(body=[method], type_ignores=[]), str(BACKEND), "exec")
        class Rejected(Exception):
            def __init__(self, status_code, detail):
                super().__init__(detail)
                self.status_code = status_code
        class Store:
            def __init__(self):
                self.saved = {}
            def set_setting(self, key, value):
                self.saved[key] = value

        for instance in ("song", "engine1", "shortstudio"):
            store = Store()
            env = {
                "Request": object, "Form": lambda val: val, "os": os,
                "HTTPException": Rejected,
                "current_user": lambda request: object(),
                "require_csrf": lambda request, token: self.assertEqual(token, "valid"),
                "require_heavy": lambda user: None,
                "store": store, "redirect": lambda path, **kwargs: (path, kwargs),
            }
            with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": instance}):
                exec(code, env)
                if instance == "song":
                    with self.assertRaises(Rejected) as error:
                        env["save_settings"](object(), "valid", "0", "0", "0", "1")
                    self.assertEqual(error.exception.status_code, 404)
                    self.assertEqual(store.saved, {})
                    continue
                response = env["save_settings"](object(), "valid", "1", "1", "1", "1")
            self.assertEqual(store.saved["bulk_import_enabled"], "1")
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
        self.assertTrue(allowed("POST", "/song-uploader/actions/auto-v6/engine", False))
        self.assertFalse(allowed("DELETE", "/song-uploader/actions/auto-v6/engine", False))
        self.assertFalse(allowed("PUT", "/song-uploader/actions/settings", False))
        self.assertFalse(allowed("POST", "/song-maker/api/queue", False))
        self.assertFalse(allowed("POST", "/song-uploader-evil/actions/settings", False))

    def test_targeted_overlay_preserves_every_other_scene_policy(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "song_uploader_candidate_policy_release",
            ROOT / "production/song_uploader_candidate_policy_release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        old = 'def mutation_allowed(method, path, editing_enabled):\n    if not editing_enabled:\n        return False\n    return False\n'
        patched = module.patch_song_alias(old)
        self.assertIn('path.startswith("/song-uploader/")', patched)
        self.assertEqual(module.patch_song_alias(patched), patched)
        self.assertTrue(module.policy_allows_code(patched))
        self.assertIn("if not editing_enabled", patched)
        self.assertNotIn("/song-maker/api/queue", patched)

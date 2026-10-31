"""Short-only channel-source and fixed-mode contracts; no live writes."""
from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/app.html"
BACKEND = ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py"


class ShortUploaderFixedModes(unittest.TestCase):
    def test_short_dashboard_has_no_modes_for_both_roles(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        fragment = source.split("{% elif tab == 'dashboard' %}", 1)[1].split(
            "{% elif tab == 'channels' %}", 1
        )[0]
        for admin in (False, True):
            html = Environment().from_string(fragment).render(
                engine_instance="shortstudio",
                user={"is_admin": admin},
                base_path="/short-uploader",
                stats={"counts": {}, "channels": 0, "engine_enabled": False,
                       "settings": {"sheets_sync_enabled": "1", "auto_engine_enabled": "1"}},
            )
            self.assertIn("Serial engine", html)
            self.assertNotIn("Future modes", html)
            self.assertNotIn("Uploader modes", html)
            self.assertNotIn("Save modes", html)
            self.assertNotIn("/actions/settings", html)
        # Song and legacy modes continue using their existing separate guard.
        self.assertIn("engine_instance != 'shortstudio'", source)

    def test_short_manual_channel_group_form_removed_both_roles(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        fragment = source.split(
            "{% if not user.is_admin and engine_instance in ['copyright'] %}", 1
        )[1].split('<section class="lum-channel-summary">', 1)[0]
        fragment = "{% if not user.is_admin and engine_instance in ['copyright'] %}" + fragment
        template = Environment().from_string(fragment)
        for admin in (False, True):
            rendered = template.render(
                engine_instance="shortstudio", user={"is_admin": admin}, channel_rows=[],
                base_path="/short-uploader", csrf="test",
            )
            self.assertNotIn("Your YouTube channels", rendered)
            self.assertNotIn("/actions/studio-tenant/channel-create", rendered)
            self.assertNotIn("/actions/studio-tenant/group-create", rendered)
        self.assertIn('action="{{ base_path }}/actions/sheets/save"', source)
        self.assertIn('action="{{ base_path }}/actions/sheets/sync"', source)
        self.assertIn('action="{{ base_path }}/actions/auto-v6/profile/save"', source)

    def test_short_backend_always_on_even_with_saved_off_flags(self):
        source = BACKEND.read_text(encoding="utf-8")
        tree = ast.parse(source)
        method = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                      and n.name == "mode_is_enabled")
        namespace = {
            "os": os,
            "store": type("Store", (), {
                "setting": lambda self, key, fallback: "0"
            })(),
            "SHORT_UPLOADER_FIXED_MODES": ("sheets_sync_enabled", "auto_engine_enabled"),
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(BACKEND), "exec"),
             namespace)
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "shortstudio"}):
            self.assertTrue(namespace["mode_is_enabled"]("sheets_sync_enabled"))
            self.assertTrue(namespace["mode_is_enabled"]("auto_engine_enabled"))
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "song"}):
            self.assertFalse(namespace["mode_is_enabled"]("sheets_sync_enabled"))
            self.assertFalse(namespace["mode_is_enabled"]("auto_engine_enabled"))
        # Startup normalization persists ON only for the isolated shortstudio DB.
        self.assertIn('if os.getenv("LUM_ENGINE_INSTANCE") == "shortstudio":', source)
        self.assertIn('store.set_setting(_mode_key, "1")', source)
        self.assertIn('if not mode_is_enabled("sheets_sync_enabled"):', source)

    def test_short_stale_settings_post_cannot_disable_modes(self):
        source = BACKEND.read_text(encoding="utf-8")
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                    and n.name == "save_settings")
        node.decorator_list = []
        class Gone(Exception):
            pass
        namespace = {
            "Request": object,
            "Form": lambda value: value,
            "HTTPException": lambda code, msg: Gone((code, msg)),
            "current_user": lambda request: {"username": "test"},
            "require_csrf": lambda request, token: None,
            "require_heavy": lambda user: None,
            "os": os,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(BACKEND), "exec"),
             namespace)
        with patch.dict(os.environ, {"LUM_ENGINE_INSTANCE": "shortstudio"}):
            with self.assertRaises(Gone):
                namespace["save_settings"](object(), "valid", "0", "0", "0", "1")

    def test_scoped_release_contract(self):
        runtime = (ROOT / "production/runtime.py").read_text()
        release = (ROOT / "production/short_uploader_group_release.py").read_text()
        workflow = (ROOT / ".github/workflows/vps1-short-studio-release.yml").read_text()
        self.assertIn("[vps1-shortstudio-fixed-modes]", runtime)
        self.assertIn("install_short_fixed_modes(args.revision, fixed_modes_only=True)", runtime)
        self.assertIn("('templates/app.html', 'lum_engine/standalone_app.py')", release)
        self.assertIn("verify_live(fixed_modes_only=True)", release)
        self.assertIn("[vps1-shortstudio-fixed-modes]", workflow)
        self.assertNotIn("AUTO_SCRIPT=ONLINE", workflow)


if __name__ == "__main__":
    unittest.main()

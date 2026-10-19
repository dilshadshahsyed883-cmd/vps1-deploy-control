"""Regression contracts for the single full Auto Uploader workspace."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / "apps/local-upload-manager"


class HeavyOnlyUploaderTests(unittest.TestCase):
    def test_both_parts_keep_full_workspace_without_mode_switching(self):
        for part in ("part01", "part02"):
            with self.subTest(part=part):
                root = ROOT / part
                app = (root / "templates/app.html").read_text(encoding="utf-8")
                base = (root / "templates/base.html").read_text(encoding="utf-8")
                users = (root / "templates/users.html").read_text(encoding="utf-8")
                source = (root / "lum_engine/standalone_app.py").read_text(encoding="utf-8")
                self.assertNotIn("lum-mode-toggle", app)
                self.assertNotIn("admin-mode-toggle", base)
                self.assertNotIn('name="mode"', app + base)
                self.assertNotIn('value="simple"', users)
                self.assertNotIn("mode == 'simple'", app)
                for tab in ("channels", "auto_engine", "queue", "logs"):
                    self.assertIn(f"tab=='{tab}'", app)
                self.assertIn('allowed = {"dashboard", "channels"', source)
                self.assertIn('default_tab = "dashboard"', source)
                self.assertIn('request.session.pop("lum_admin_mode", None)', source)
                self.assertNotIn('request.session["lum_admin_mode"] =', source)
                self.assertNotIn('{"simple", "heavy", "administrator"}', source)
                module = ast.parse(source)
                fn = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "user_mode")
                self.assertEqual(ast.literal_eval(fn.body[-1].value), "heavy")


if __name__ == "__main__":
    unittest.main()

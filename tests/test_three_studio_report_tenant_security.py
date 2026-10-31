"""PR #544 non-admin uploader report metadata isolation regression."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APPS = (
    ROOT / "apps/local-upload-manager/part01/lum_engine/standalone_app.py",
    ROOT / "apps/copyright-uploader/part01/lum_engine/standalone_app.py",
)


class ReportTenantSecurityTests(unittest.TestCase):
    def test_dashboard_report_inventory_is_admin_only(self):
        for path in APPS:
            with self.subTest(path=path):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                app_page = next(
                    node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "app_page"
                )
                report_contexts = [
                    node.values[index]
                    for node in ast.walk(app_page)
                    if isinstance(node, ast.Dict)
                    for index, key in enumerate(node.keys)
                    if isinstance(key, ast.Constant) and key.value == "reports"
                ]
                self.assertEqual(len(report_contexts), 1)
                expression = report_contexts[0]
                self.assertIsInstance(expression, ast.IfExp)
                self.assertIn("is_admin", ast.unparse(expression.test))
                self.assertIsInstance(expression.orelse, ast.List)
                self.assertEqual(expression.orelse.elts, [])
                self.assertIn("list_reports", ast.unparse(expression.body))

    def test_report_downloads_deny_non_admin(self):
        for path in APPS:
            with self.subTest(path=path):
                source = path.read_text(encoding="utf-8")
                self.assertIn('path.startswith(route("/reports/"))', source)
                self.assertIn('"Owner-scoped resource access required"', source)


if __name__ == "__main__":
    unittest.main()

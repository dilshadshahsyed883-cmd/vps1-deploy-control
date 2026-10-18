import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rebuild import dependency_budget as b


class DependencyBudgetTests(unittest.TestCase):
    def test_ten_active_and_auto_script_offline(self):
        c = b.config()
        self.assertEqual(len(c["install_groups"]), 10)
        self.assertNotIn("auto-script", [g["app"] for g in c["install_groups"]])
        self.assertEqual(c["application_budget_bytes"], 30_000_000_000)
        self.assertEqual(c["python_minor"], "3.12")

    def test_reject_live_roots(self):
        for root in ("/", "/srv/fresh-website", "/opt/fresh-website", "/var/lib/fresh-website"):
            with self.assertRaises(ValueError):
                b.dirs(root)

    def test_inode_accounting_deduplicates_hardlinks(self):
        with tempfile.TemporaryDirectory(prefix="fresh-budget-", dir="/tmp") as t:
            root = Path(t)
            first = root / "one.whl"
            second = root / "two.whl"
            first.write_bytes(b"example" * 1024)
            os.link(first, second)
            single = b.disk_bytes(root)
            second.unlink()
            self.assertEqual(single, b.disk_bytes(root))

    def test_missing_capacity_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="fresh-budget-", dir="/tmp") as t:
            with patch.object(b, "snapshot", return_value={
                "within_budget": False, "host_reserve_ok": True
            }):
                with self.assertRaises(ValueError):
                    b.require_capacity(t)

    def test_dedicated_venvs_one_shared_uv_cache(self):
        with tempfile.TemporaryDirectory(prefix="fresh-budget-", dir="/tmp") as t:
            recorded = []
            def fake_run(cmd, **kwargs):
                recorded.append((cmd, kwargs))
                if cmd[1:3] == ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"]:
                    return type("Result", (), {"stdout": "3.12\n"})()
                return type("Result", (), {"stdout": ""})()
            with patch.object(b.subprocess, "run", side_effect=fake_run), \
                 patch.object(b, "require_capacity", return_value={
                    "remaining_budget_bytes": 10_000_000, "host_reserve_ok": True
                 }):
                result = b.install(t, "/usr/bin/python3", uv_exe="uv",
                                   apps=["short-maker", "short-stream"])
            self.assertEqual(len(result["applications"]), 2)
            uv_calls = [(cmd, kw["env"]) for cmd, kw in recorded if cmd[0] == "uv"]
            self.assertEqual(sum(cmd[1] == "venv" for cmd, _ in uv_calls), 2)
            self.assertEqual(sum(cmd[1] == "pip" for cmd, _ in uv_calls), 2)
            caches = {env["UV_CACHE_DIR"] for _, env in uv_calls}
            self.assertEqual(len(caches), 1)
            self.assertTrue(all(env["UV_LINK_MODE"] == "hardlink" for _, env in uv_calls))
            self.assertTrue(all(env["UV_NO_MANAGED_PYTHON"] == "1" for _, env in uv_calls))

    def test_offline_install_rejected(self):
        with tempfile.TemporaryDirectory(prefix="fresh-budget-", dir="/tmp") as t:
            with self.assertRaises(ValueError):
                b.install(t, "/usr/bin/python3", apps=["auto-script"])


if __name__ == "__main__":
    unittest.main()

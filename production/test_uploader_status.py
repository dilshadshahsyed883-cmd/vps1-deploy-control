"""Read-only compatibility tests for fresh and legacy Auto Uploader service status."""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from production.runtime import environment

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "apps/local-upload-manager/part01/lum_engine/service_status.py"
)
get_units = runpy.run_path(str(SOURCE))["auto_engine_service_units"]


class UploaderStatusTests(unittest.TestCase):
    def test_legacy_uses_original_unit_names(self):
        with patch.dict(os.environ, {"FRESH_WEBSITE_INSTALL": "0"}):
            self.assertEqual(get_units(), {
                "worker": "lum-auto-engine-v6-worker.service",
                "scheduler": "lum-auto-engine-v6-scheduler.service",
            })

    def test_fresh_production_uses_installed_unit_names(self):
        with patch.dict(os.environ, {"FRESH_WEBSITE_INSTALL": "1"}):
            self.assertEqual(get_units(), {
                "worker": "vps1-production-lum-worker.service",
                "scheduler": "vps1-production-lum-scheduler.service",
            })

    def test_fresh_environment_routes_to_isolated_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = environment("local-upload-manager", Path(tmp))
            self.assertEqual(env["FRESH_WEBSITE_INSTALL"], "1")
            self.assertEqual(env["LUM_AUTO_ENGINE_V6_ROOT"], str(Path(tmp) / "lum1/auto_engine_v6"))
            self.assertEqual(env["YOUTUBE_UPLOAD_MANAGER_DATA_DIR"], str(Path(tmp) / "lum1/core"))
            self.assertEqual(env["LUM_GOOGLE_OAUTH_ROOT"], str(Path(tmp) / "lum1/secure"))


if __name__ == "__main__":
    unittest.main()

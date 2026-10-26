"""Guard against restoring the retired Scene Builder ↔ SML media selector."""
from pathlib import Path
import ast
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder"

class SceneSourceIsolationTests(unittest.TestCase):
    def test_python_modules_parse_and_have_no_sml_selector(self):
        app = (SCENE / "part01/app.py").read_text(encoding="utf-8")
        worker = (SCENE / "part02/api_server.py").read_text(encoding="utf-8")
        ast.parse(app)
        ast.parse(worker)
        self.assertNotIn("from sml_scene_bridge", app)
        self.assertNotIn("SMLWorkerClient", worker)
        for endpoint in ("/api/sml/clips", "/api/settings/sml-status", "/sml-select", "/sml-auto-match"):
            self.assertNotIn(endpoint, app)
        for retired in ("_try_sml_first_scene", "_parallel_sml_scene_download", "_sml_fallback", "_sml_first"):
            self.assertNotIn(retired, worker)
        self.assertFalse((SCENE / "part01/sml_scene_bridge.py").exists())
        self.assertFalse((SCENE / "part02/sml_worker_bridge.py").exists())

    def test_frontend_keeps_only_local_scene_media_library(self):
        settings = (SCENE / "part01/templates/settings.html").read_text(encoding="utf-8")
        audit = (SCENE / "part01/templates/audit_media.html").read_text(encoding="utf-8")
        self.assertIn("scene_library_enabled", settings)
        self.assertIn("Scene Library", audit)
        self.assertNotIn("Browse SML", audit)
        self.assertNotIn("Self Media Library (SML)", settings)
        self.assertNotIn("sml-modal", audit)
        self.assertNotIn("sml_controls_present", settings)
        self.assertIn("Pexels", settings)

    def test_standalone_sml_is_preserved(self):
        self.assertTrue((ROOT / "apps/self-media-library").is_dir())
        self.assertTrue((SCENE / "part01/scene_media_library.py").is_file())

if __name__ == "__main__":
    unittest.main()

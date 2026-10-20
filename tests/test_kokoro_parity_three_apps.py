"""No-deployment, app-scoped Kokoro-reference UI governance.

These are static checks; authenticated browser geometry must be verified
separately before calling the three-app UI parity visually accepted.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
OMNI = ROOT / "apps/voice-studio/part01/templates/voice_studio.html"


class ThreeAppUiParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.omni = OMNI.read_text(encoding="utf-8")
        cls.base = (SCENE / "templates/base.html").read_text(encoding="utf-8")
        cls.experiment = (SCENE / "templates/experiment_studio.html").read_text(encoding="utf-8")
        cls.scene_css = (SCENE / "static/scene_builder_kokoro_parity_20261020.css").read_text(encoding="utf-8")

    def test_omnivoice_parity_isolated_from_kokoro_edge_and_audio(self):
        self.assertEqual(self.omni.count('id="omni-kokoro-parity-ui"'), 1)
        block = self.omni.split('id="omni-kokoro-parity-ui"', 1)[1].split("</style>", 1)[0]
        self.assertIn("#page-omnivoice .card.pad", block)
        self.assertIn("#page-omnivoice textarea", block)
        self.assertIn("min-height:44px", block)
        self.assertIn("min-height:180px", block)
        self.assertIn("max-width:1400px", block)
        self.assertIn("prefers-reduced-motion:reduce", block)
        self.assertIn("not(.vs-kokoro-active):not(.vs-edge-active):not(.vs-audio-active)", block)
        self.assertNotIn("#vsKokoroWorkspace", block)
        self.assertNotIn("#vsEdgeWorkspace", block)
        self.assertNotIn("#vsAudioWorkspace", block)

    def test_omnivoice_control_labels_and_status_are_accessible(self):
        for element in ("omniLanguage", "omniProfileSelect", "omniSpeed", "omniNumStep"):
            self.assertIn(f'for="{element}"', self.omni)
        self.assertIn('<output id="omniSpeedOut" for="omniSpeed">', self.omni)
        self.assertIn('id="omniQueueStatus" role="status" aria-live="polite"', self.omni)

    def test_scene_styles_are_scoped_to_exact_three_pages(self):
        self.assertIn("scene_builder_kokoro_parity_20261020.css?v=1", self.base)
        self.assertIn("{% if page in ['queue','settings','audit_media'] %}", self.base)
        self.assertIn("vps1-scene-parity", self.base)
        self.assertIn("body.vps1-scene-parity .scene-workspace-header", self.scene_css)
        self.assertIn("body.vps1-scene-parity .scene-workspace-tabs", self.scene_css)
        self.assertIn("min-height: 44px", self.scene_css)
        self.assertIn("scrollbar-gutter: stable", self.scene_css)
        self.assertIn("prefers-reduced-motion: reduce", self.scene_css)
        for page in ("queue", "settings", "audit_media"):
            self.assertIn(f'page==\'{page}\' %}} aria-current="page"', self.base)
        self.assertNotIn("body.vps1-native-voice", self.scene_css)

    def test_antidetect_controls_have_kokoro_geometry_without_engine_changes(self):
        self.assertEqual(self.experiment.count('id="experiment-kokoro-parity-ui"'), 1)
        block = self.experiment.split('id="experiment-kokoro-parity-ui"', 1)[1].split("</style>", 1)[0]
        for marker in (
            "#experiment-browser .experiment-browser-panel",
            "#experiment-browser .experiment-browser-row",
            "min-height:44px",
            "border-radius:16px",
            "@media(max-width:600px)",
            ":focus-visible",
        ):
            self.assertIn(marker, block)
        self.assertIn('id="browser-engine-status" role="status" aria-live="polite"', self.experiment)
        self.assertIn('aria-label="Browser session actions"', self.experiment)
        self.assertIn('id="browser-create-form"', self.experiment)
        self.assertIn('id="browser-profiles"', self.experiment)
        self.assertIn('id="browser-preview"', self.experiment)

    def test_no_app_operations_or_shared_shell_files_are_replaced(self):
        # The style changes must not implement fake data or mutate any queue
        # or browser profile state.
        self.assertNotIn("fetch(", self.scene_css)
        self.assertNotIn("localStorage", self.scene_css)
        self.assertNotIn("<script", self.scene_css)
        self.assertIn('vps1_native_site_shell.js', self.base)
        self.assertIn('vps1_native_site_shell.css', self.base)


if __name__ == "__main__":
    unittest.main()

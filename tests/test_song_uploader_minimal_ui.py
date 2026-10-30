"""Song-only minimal Auto Engine layout: offline presentation regression tests.

The Song Uploader redesign must not mutate uploader engines or other modes.
"""
from pathlib import Path
import unittest

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps/local-upload-manager/part01/templates/app.html"
CSS = ROOT / "apps/local-upload-manager/part01/static/song_uploader_kokoro_ui.css"
BASE = ROOT / "apps/local-upload-manager/part01/templates/base.html"


class SongUploaderMinimalUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = APP.read_text(encoding="utf-8")
        cls.css = CSS.read_text(encoding="utf-8")
        cls.base = BASE.read_text(encoding="utf-8")

    def test_jinja_template_parses(self):
        Environment().parse(self.app)

    def test_auto_engine_is_first_song_tab_but_legacy_tabs_remain(self):
        song_branch = self.app.split("{% if engine_instance == 'song' %}", 1)[1]
        self.assertLess(song_branch.index("tab=auto_engine"), song_branch.index("tab=channels"))
        self.assertIn("tab=sheets", song_branch)
        self.assertIn("tab=dashboard", song_branch)
        self.assertIn("{% else %}", song_branch)

    def test_intro_is_removed_only_for_song_not_other_uploaders(self):
        start = self.app.index("{% elif tab == 'auto_engine' %}")
        prefix = self.app[start:start + 550]
        self.assertIn("{% if engine_instance != 'song' %}", prefix)
        self.assertIn("Short Uploader · Auto Engine", prefix)
        self.assertIn("mode_registry.cross_mode_record_collisions", self.app)
        self.assertIn("engine_instance == 'song' and mode_registry.cross_mode_record_collisions", self.app)

    def test_song_maker_import_stays_functional_and_collapsible(self):
        self.assertEqual(self.app.count('id="songMakerLocalOutputList"'), 1)
        self.assertIn('<details class="lum-card lum-song-import-details" data-ae-panel="automation"', self.app)
        self.assertIn("Import from Song Maker", self.app)
        self.assertIn("/song-maker/api/uploader/outputs", self.app)
        self.assertIn("/song-uploader/actions/auto-v6/songmaker/import", self.app)
        self.assertIn("csrf_token:csrfToken", self.app)
        self.assertIn("profile_id:picker.value", self.app)
        self.assertIn("job_id:item.id", self.app)

    def test_engine_actions_and_panels_unchanged(self):
        for panel in ("automation", "profiles", "queue", "records"):
            self.assertIn(f'data-ae-panel-target="{panel}"', self.app)
        for name in ("arm_scheduler", "disarm_scheduler", "/actions/auto-v6/sync-all",
                     "/actions/auto-v6/start-all"):
            self.assertIn(name, self.app)

    def test_minimal_css_is_song_only_and_cache_busted(self):
        marker = "SONG_UPLOADER_MINIMAL_AUTO_ENGINE_V2"
        self.assertIn(marker, self.css)
        scoped = self.css.split(marker, 1)[1]
        self.assertIn('body.lum-song-ui .lum-shell[data-engine-instance="song"]', scoped)
        self.assertNotIn('display: none', scoped.split(".lum-song-import-details", 1)[-1])
        self.assertIn("20261030-minimal-auto-v2", self.base)


if __name__ == "__main__":
    unittest.main()

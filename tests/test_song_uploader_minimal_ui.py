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

    def test_song_tabs_follow_requested_order(self):
        song_branch = self.app.split("{% if engine_instance == 'song' %}", 1)[1].split("{% else %}", 1)[0]
        expected = (
            ("dashboard", "Dashboard"),
            ("channels", "Channels"),
            ("sheets", "Sheets Sync"),
            ("auto_engine", "Auto Engine"),
        )
        positions = []
        for tab, label in expected:
            anchor = f'href="{{{{ base_path }}}}/app?tab={tab}">{label}</a>'
            self.assertIn(anchor, song_branch)
            positions.append(song_branch.index(anchor))
        self.assertEqual(positions, sorted(positions))
        self.assertIn('tab==\'logs\'', self.app)
        self.assertIn(">Logs</a>", self.app)

    def test_intro_is_hidden_for_song_and_short_but_other_modes_keep_it(self):
        start = self.app.index("{% elif tab == 'auto_engine' %}")
        end = self.app.index("{% if auto_mode == 'engine1' %}", start)
        intro = self.app[start:end]
        self.assertIn("{% if engine_instance not in ['song', 'shortstudio'] %}", intro)
        self.assertIn("engine_instance in ['song', 'shortstudio'] and mode_registry.cross_mode_record_collisions", intro)
        template = Environment().from_string(intro.replace("{% elif tab == 'auto_engine' %}", "", 1))
        for instance in ("song", "shortstudio"):
            with self.subTest(instance=instance):
                html = template.render(engine_instance=instance, mode_registry={"cross_mode_record_collisions": 0})
                self.assertNotIn("independent uploader workspace", html)
                self.assertNotIn("Short Uploader · Auto Engine", html)
                self.assertNotIn('<section class="lum-card">', html)
        for instance in ("engine1", "engine2"):
            with self.subTest(instance=instance):
                html = template.render(engine_instance=instance, mode_registry={"cross_mode_record_collisions": 0})
                self.assertIn("Auto Uploader · Two Modes", html)
                self.assertIn('<section class="lum-card">', html)
        html = template.render(engine_instance="shortstudio", mode_registry={"cross_mode_record_collisions": 2})
        self.assertIn("Cross-mode receipt collision(s) detected: 2", html)
        self.assertNotIn('<section class="lum-card">', html)

    def test_song_maker_import_stays_functional_and_collapsible(self):
        self.assertEqual(self.app.count('id="songMakerLocalOutputList"'), 1)
        self.assertIn('<details class="lum-card lum-song-import-details" data-ae-panel="automation"', self.app)
        self.assertIn("Import from Song Maker", self.app)
        self.assertIn("/song-maker/api/uploader/outputs", self.app)
        self.assertIn("/song-uploader/actions/auto-v6/songmaker/import", self.app)
        self.assertIn("csrf_token:csrfToken", self.app)
        self.assertIn("profile_id:picker.value", self.app)
        self.assertIn("job_id:item.id", self.app)

    def test_song_dashboard_hides_vps_reports_without_deleting_other_mode_reports(self):
        """Only Song Dashboard omits the shared VPS Reports macro output."""
        start = self.app.index("{% macro reports_panel() %}")
        end = self.app.index("{% macro queue_table", start)
        begin = self.app.index("{% elif tab == 'dashboard' %}")
        finish = self.app.index("{% elif tab == 'channels' %}", begin)
        source = self.app[start:end] + self.app[begin:finish].replace(
            "{% elif tab == 'dashboard' %}", "", 1)
        template = Environment(autoescape=True).from_string(source)
        stats = {"counts": {"TOTAL": 1}, "channels": 3, "engine_enabled": True,
                 "settings": {"sheets_sync_enabled": "0", "auto_engine_enabled": "1",
                              "bulk_import_enabled": "0"}}
        sample_report = {"name": "LUM_HEALTH_SAMPLE.txt", "size": 512,
                         "modified_at": "31 Oct 2026"}
        for instance in ("song", "shortstudio", "engine1", "engine2"):
            with self.subTest(instance=instance):
                html = template.render(engine_instance=instance, base_path="/test",
                                       csrf="offline", stats=stats, reports=[sample_report])
                self.assertIn("Serial engine", html)
                self.assertIn("Save modes", html)
                if instance == "song":
                    for absent in ("VPS Reports", "Generate Health Report",
                                   "LUM_HEALTH_SAMPLE.txt", "/actions/report-health",
                                   'class="report-list"'):
                        self.assertNotIn(absent, html)
                else:
                    for present in ("VPS Reports", "Generate Health Report",
                                    "LUM_HEALTH_SAMPLE.txt", "/actions/report-health"):
                        self.assertIn(present, html)
        self.assertIn("{% macro reports_panel() %}", self.app)
        self.assertIn("{% if engine_instance != 'song' %}{{ reports_panel() }}{% endif %}",
                      self.app)

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

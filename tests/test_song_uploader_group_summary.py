"""Song Uploader group overview: accurate saved group summary, isolated from other uploaders."""
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "apps/local-upload-manager/part01/templates/app.html"


class SongAutomationGroupSummaryTests(unittest.TestCase):
    def render(self, profiles=None, instance="song"):
        source = TEMPLATE.read_text()
        start = source.index('<section class="lum-card lum-ae-automation-controls"')
        end = source.index('</section>', start) + len('</section>')
        return Environment(autoescape=True).from_string(source[start:end]).render(
            engine_instance=instance, ae=SimpleNamespace(profiles=profiles or []),
            p=None, base_path="/song-uploader", csrf="offline-test"
        )

    def test_group_name_exact_channel_count_and_saved_pkt_times(self):
        html = self.render([dict(id=7,category_name="Daily Music",group_name="Music Group",
            group_channel_count=12,enabled=1,v6_video_count=8,
            schedule_times="09:00,14:30",schedule_enabled=1)])
        for item in ('data-song-group-summary', '<th>Group Name</th>',
                     '<th>Channels</th>', '<th>Schedule (PKT)</th>',
                     'Music Group', 'data-song-group-channel-count="7">12</td>',
                     '09:00, 14:30', 'auto_profile_id=7'):
            self.assertIn(item, html)
        self.assertNotIn('data-ae-sync-scan',html)
        self.assertNotIn('Sync Channel</button>',html)
        self.assertEqual(html.count('>Sync All Channels</button>'),1)
        self.assertEqual(html.count('/actions/auto-v6/sync-all'),1)
        toolbar=html.split('lum-ae-bulk-profile-controls',1)[1].split('</div>',1)[0]
        self.assertIn('Generate Queue Now',toolbar)
        self.assertIn('Start All Profiles',toolbar)
        self.assertIn('Sync All Channels',toolbar)
        self.assertLess(toolbar.index('Start All Profiles'),toolbar.index('Sync All Channels'))
        for item in ('<th>Channel ID</th>', '<th>Thumbnails</th>',
                     '<th>Auto Sync</th>', '<th>Last Sync</th>'):
            self.assertNotIn(item, html)

    def test_zero_group_count_and_disabled_schedule(self):
        html = self.render([dict(id=8,category_name="Empty",group_name="No members",
            group_channel_count=0,enabled=1,schedule_times="18:00",schedule_enabled=0)])
        self.assertIn('data-song-group-channel-count="8">0</td>', html)
        self.assertIn('Schedule OFF', html)
        self.assertIn('18:00', html)
        self.assertNotIn('15 min', html)

    def test_legacy_individual_is_not_silently_converted(self):
        html = self.render([dict(id=1,category_name="music",channel_db_id=55,
            enabled=1, schedule_times="09:00")])
        self.assertIn('Single channel', html)
        self.assertIn('data-song-group-channel-count="1">1</td>', html)
        self.assertIn('Per-video schedule', html)
        self.assertNotIn('09:00', html)

    def test_escaping_and_empty_table(self):
        self.assertIn('colspan="9"',self.render())
        html = self.render([dict(id=5,category_name="Test",group_name="<script>bad</script>",
                                 enabled=1)])
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;",html)
        self.assertNotIn("<script>bad</script>",html)

    def test_song_release_health_uses_true_internal_route(self):
        # The isolated Song service binds LUM_BASE_PATH=/local-upload-manager/modes/song;
        # /song-uploader is public Nginx routing and redirects on port 8018.
        source = (ROOT / "production/song_uploader_group_summary_release.py").read_text()
        self.assertIn("http://127.0.0.1:8018/local-upload-manager/modes/song/health", source)
        self.assertNotIn("http://127.0.0.1:8018/song-uploader/health", source)
        self.assertIn("https_get('/song-uploader/app?tab=auto_engine&new_profile=1'", source)

    def test_auth_guard_denies_protected_content_but_accepts_sso_redirects(self):
        source = (ROOT / "production/song_uploader_group_summary_release.py").read_text()
        self.assertIn("anonymous_status in (302, 303, 401, 403)", source)
        self.assertIn("b'data-song-group-summary' not in anonymous_body", source)
        self.assertIn("b'lum-ae-automation-profiles' not in anonymous_body", source)
        self.assertNotIn("https_get('/song-uploader/app')[0]==401", source)

    def test_guarded_song_release_requires_global_sync_toolbar(self):
        source = (ROOT / "production/song_uploader_group_summary_release.py").read_text()
        self.assertIn("global sync is not on the bulk-action row", source)
        self.assertIn("individual sync UI persists or global sync is duplicated", source)
        self.assertIn("'data-ae-sync-scan' not in html", source)
        self.assertIn("html.count('>Sync All Channels</button>')==1", source)

    def test_engine1_remains_legacy_table(self):
        html = self.render([dict(id=3,enabled=1,category_name="Channel",
            bound_channel_name="Channel",bound_channel_id="UC123",
            sync_interval_minutes=15)], instance="engine1")
        self.assertIn("<th>Channel ID</th>",html)
        self.assertIn("<th>Thumbnails</th>",html)
        self.assertNotIn("data-song-group-summary",html)
        self.assertIn("data-ae-sync-scan",html)
        self.assertIn("Sync Channel</button>",html)


if __name__ == "__main__":
    unittest.main()

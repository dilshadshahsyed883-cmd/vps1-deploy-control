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
                     '09:00, 14:30', 'auto_profile_id=7', 'data-ae-sync-scan'):
            self.assertIn(item, html)
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

    def test_engine1_remains_legacy_table(self):
        html = self.render([dict(id=3,enabled=1,category_name="Channel",
            bound_channel_name="Channel",bound_channel_id="UC123",
            sync_interval_minutes=15)], instance="engine1")
        self.assertIn("<th>Channel ID</th>",html)
        self.assertIn("<th>Thumbnails</th>",html)
        self.assertNotIn("data-song-group-summary",html)


if __name__ == "__main__":
    unittest.main()

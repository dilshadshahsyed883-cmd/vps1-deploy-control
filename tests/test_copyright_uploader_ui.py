"""Rendered manual UI behavior and app-scoped release selection."""
from pathlib import Path
import os
import subprocess
import unittest
from production.copyright_uploader_browser import fixture

ROOT=Path(__file__).resolve().parents[1]

class CopyrightUploaderUI(unittest.TestCase):
    def test_dashboard_has_only_manual_workflow(self):
        html=fixture('dashboard')
        self.assertIn('Worker Online',html)
        self.assertIn('Queue Ready',html)
        self.assertIn('tab=add_video',html)
        for removed in ('Save modes','Future modes','Auto Engine','Bulk Import'):
            self.assertNotIn(removed,html)

    def test_add_video_preserves_sources_fields_and_public_default(self):
        from html.parser import HTMLParser
        class FormParser(HTMLParser):
            selected=False
            options=[]
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='select': self.selected=attrs.get('name')=='privacy_status'
                if tag=='option' and self.selected: self.options.append(attrs['value'])
            def handle_endtag(self,tag):
                if tag=='select': self.selected=False
        html=fixture('add_video')
        parser=FormParser();parser.feed(html)
        self.assertEqual(parser.options,['public','unlisted','private'])
        for name in ('source_mode','profile_id','video_file','folder_file','drive_url','channel_db_id','thumbnail','title','description','tags','category_id','playlist_id','app_upload_at','csrf_token'):
            self.assertIn('name="'+name+'"',html)
        self.assertIn('/actions/add-video',html)

    def test_queue_names_error_escaping_and_existing_recovery_actions(self):
        html=fixture('queue')
        self.assertIn('My Channel',html)
        self.assertIn('/actions/retry/2',html)
        self.assertIn('/actions/authorize-fresh/3',html)
        self.assertIn('NO REMOTE VIDEO',html)
        self.assertIn('&lt;unsafe&gt; Network issue',html)
        self.assertNotIn('<unsafe>',html)
        self.assertNotIn('/actions/stop-current',html)
        self.assertIn('/actions/pause-after-current',html)
        paused=fixture('queue',paused=True)
        self.assertIn('/actions/resume',paused)
        self.assertNotIn('/actions/pause-after-current',paused)
        empty=fixture('queue',projects=[])
        self.assertIn('Add your first video',empty)

    def test_queue_schedule_is_displayed_in_pkt(self):
        html=fixture('queue',projects=[dict(id=1,title='Scheduled',channel_db_id=7,status='SCHEDULED',progress=0,app_upload_at='2026-10-30T20:00:00+00:00')])
        self.assertIn('31 Oct 2026 · 01:00',html)

    def test_profiles_shortcut_and_saved_metadata_are_preserved(self):
        html=fixture('profiles')
        self.assertIn('tab=add_video&amp;profile_id=3',html)
        self.assertIn('Main profile',html)
        self.assertIn('/actions/copyright/profile',html)
        self.assertIn('value="sheets"',html)
        self.assertNotIn('Arm Scheduled',html)

    def test_sheets_sync_matches_compact_short_uploader_layout(self):
        html=fixture('sheets')
        self.assertIn('lum-sheet-format-help',html)
        self.assertIn('<summary>Sheet format</summary>',html)
        self.assertIn('lum-short-sheet-header-actions',html)
        self.assertEqual(html.count('Update All Enabled Sheets'),1)
        self.assertNotIn('<h2>Manual Update</h2>',html)
        self.assertIn('/actions/sheets/save',html)
        self.assertIn('/actions/sheets/sync-all',html)
        self.assertIn('/actions/sheets/sync',html)
        self.assertIn('No Google Sheet source saved yet.',html)
        self.assertIn('Sync Enabled',html)
        self.assertNotIn('Sync Mode OFF',html)

    def test_channels_have_no_manual_channel_or_group_creation_for_any_user(self):
        for is_admin in (True, False):
            with self.subTest(admin=is_admin):
                html=fixture('channels',is_admin=is_admin)
                for removed in ('Your YouTube channels', 'Add my channel', 'Create my group',
                                '/actions/studio-tenant/channel-create',
                                '/actions/studio-tenant/group-create'):
                    self.assertNotIn(removed,html)
                self.assertIn('Channel &amp; Brand Manager',html)
                self.assertIn('tab=sheets',html)

    def test_modes_card_absent_for_admin_and_nonadmin(self):
        for is_admin in (True, False):
            html=fixture('dashboard',is_admin=is_admin)
            for removed in ('Future modes','Uploader modes','Save modes',
                            '/actions/settings'):
                self.assertNotIn(removed,html)
            self.assertIn('tab=queue',html)

    def test_release_selection_keeps_other_apps_out_of_ui_release(self):
        workflow=(ROOT/'.github/workflows/vps1-copyright-studio-release.yml').read_text()
        self.assertIn("contains(github.event.head_commit.message, '[vps1-copyright-uploader-ui]')",workflow)
        start=workflow.index('          release_apps=(')
        end=workflow.index('          for app in ',start)
        script=workflow[start:end]+'\nprintf "%s\\n" "${release_apps[@]}"\n'
        for flag,expected in [('true',['copyright-uploader']),('false',['copyright-uploader','video-merger','scene-builder'])]:
            result=subprocess.run(['bash','-c',script],env={**os.environ,'COPYRIGHT_UI_ONLY':flag},capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.splitlines(),expected)
        self.assertIn('concurrency:',workflow)
        self.assertIn('remote rollback',workflow)
        self.assertIn('StrictHostKeyChecking=yes',workflow)

if __name__=='__main__': unittest.main()

"""Final release scope and read-only acceptance regression."""
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from jinja2 import Environment
from production.short_uploader_group_release import verify_final_ui

ROOT = Path(__file__).resolve().parents[1]

class FinalReleaseTests(unittest.TestCase):
    def test_intent_uses_isolated_lane_in_workflow_and_runtime(self):
        for path in ('.github/workflows/vps1-short-studio-release.yml', 'production/runtime.py'):
            self.assertIn('[vps1-shortstudio-final-ui]', (ROOT/path).read_text())
        runtime=(ROOT/'production/runtime.py').read_text()
        start=runtime.index("'[vps1-shortstudio-final-ui]'")
        lane=runtime[start:runtime.index('return',start)]
        self.assertIn("args.apps != ['local-upload-manager']",lane)
        self.assertIn('network_only=True',lane)

    def test_acceptance_checks_actual_new_profile_and_rejects_stale_ui(self):
        source=(ROOT/'apps/local-upload-manager/part01/templates/app.html').read_text()
        start=source.index('    <form id="lum-ae-profile-form"')
        form=Environment().from_string(source[start:source.index('</form>',start)+7]).render(
            engine_instance='shortstudio',p=None,src={},csrf='test',base_path='/short-uploader',
            ae=SimpleNamespace(groups=[],short_folders=[]))
        html='<div class="lum-short-engine-tabbar lum-short-engine-status">'+form+'</div>'
        sheets='<summary>Sheet format</summary><div class="lum-short-sheet-header-actions">Update All Enabled Sheets</div>'
        css=(ROOT/'apps/local-upload-manager/part01/static/local_upload_manager.css').read_bytes()
        calls=[]
        def get(url,cookie):
            calls.append(url)
            body=sheets.encode() if 'tab=sheets' in url else css if '/static/' in url else b'Dashboard'
            return 200,'text/html',body
        with patch('production.song_uploader_group_summary_release.health',return_value={'ok':True}):
            verify_final_ui(html,form,'cookie',get)
            with self.assertRaisesRegex(RuntimeError,'removed header'):
                verify_final_ui(html+'<section class="lum-ae-head">',form,'cookie',get)
            with self.assertRaisesRegex(RuntimeError,'default regressed: work_mode'):
                verify_final_ui(html,form.replace('value="auto" selected','value="auto"'),'cookie',get)
        self.assertEqual(len(calls),3)
        self.assertTrue(all('/actions/' not in url for url in calls))

if __name__ == '__main__': unittest.main()

"""Scene Builder Settings + Audit UI parity contract (no runtime writes)."""
from pathlib import Path
import unittest

APP = Path(__file__).resolve().parents[1]/"apps/scene-builder/part01"
SETTINGS = (APP/"templates/settings.html").read_text()
AUDIT = (APP/"templates/audit_media.html").read_text()
BASE = (APP/"templates/base.html").read_text()
STYLE = (APP/"static/scene_settings_audit_kokoro_20261021.css").read_text()

class SettingsAuditParityTests(unittest.TestCase):
    def test_local_opt_in_excludes_shell_queue_and_detached_embeds(self):
        for tpl in (SETTINGS,AUDIT):
            self.assertIn('scene_settings_audit_kokoro_20261021.css?v=1',tpl)
        self.assertNotIn('scene_settings_audit_kokoro_20261021.css',BASE)
        self.assertIn('main > .scene-settings-workspace',STYLE)
        self.assertIn('main .scene-audit-workspace',STYLE)
        self.assertNotIn('body.vps1-native-voice',STYLE)
        self.assertNotIn('fetch(',STYLE)
        self.assertNotIn('localStorage',STYLE)
        self.assertIn('min-height:44px',STYLE)
        self.assertIn('@media(max-width:760px)',STYLE)
        self.assertIn('prefers-reduced-motion:reduce',STYLE)

    def test_settings_all_real_tabs_and_form_actions_preserved(self):
        self.assertIn('id="settings-form" class="sb-settings scene-settings-workspace"',SETTINGS)
        for tab in ("presets","timing","media","creative","captions","audio","output"):
            self.assertIn(f'data-tab="{tab}"',SETTINGS)
            self.assertIn(f'data-panel="{tab}"',SETTINGS)
        for item in ('id="preset-new"','id="preset-save-state"','id="preset-auto-clean"',
                     'id="auto-clean-confirm-button"',"fetch('/settings'",
                     'function openTab(name)',"sessionStorage.getItem('sbSettingsTab')"):
            self.assertIn(item,SETTINGS)
        for item in ("Settings View","Presets Only","presets-only-toggle",
                     "sceneBuilderPresetsOnly"):
            self.assertNotIn(item,SETTINGS)

    def test_audit_list_selected_job_and_controls_preserved(self):
        self.assertIn('class="scene-audit-workspace"',AUDIT)
        self.assertIn('class="empty scene-audit-empty"',AUDIT)
        self.assertIn("{% if not selected_job %}",AUDIT)
        self.assertIn("{% for job in waiting_jobs %}",AUDIT)
        for item in ('id="audit-grid"','id="audit-empty"','id="audit-summary"',
                     'id="audit-approve-all"','id="audit-continue"',
                     'id="keyword-modal"','id="sml-modal"','id="sml-results"',
                     'data-filter="attention"','data-action="sml"',
                     'data-action="upload"','/sml-select','/sml-auto-match',
                     '/approve-all','/continue'):
            self.assertIn(item,AUDIT)
        self.assertIn('href="/audit-media?job=',AUDIT)

if __name__ == "__main__":
    unittest.main()

"""UI-only protection for SC-UI-20261021-01/04."""
from pathlib import Path
import unittest
S = Path(__file__).resolve().parents[1] / "apps/scene-builder/part01"
Q = (S/"templates/queue.html").read_text()
T = (S/"templates/settings.html").read_text()
C = (S/"static/scene_queue_kokoro_ui_20261021.css").read_text()
B = (S/"templates/base.html").read_text()
class SceneUiTests(unittest.TestCase):
    def test_scoped_queue_css(self):
        self.assertIn('scene_queue_kokoro_ui_20261021.css?v=1',Q)
        self.assertNotIn('scene_queue_kokoro_ui_20261021.css',B)
        for key in ('body.vps1-scene-parity','#project-form','max-width:1400px','min-height:44px','max-width:760px','prefers-reduced-motion:reduce'):
            self.assertIn(key,C)
        self.assertNotIn('body.vps1-native-voice',C)
        self.assertNotIn('fetch(',C)
    def test_queue_actions_untouched(self):
        for key in ('id="project-form"','action="/queue/submit"','id="project-name"','id="script-file"','id="audio-file"','id="preset-id"','id="channel-name"','id="submit-button"','id="folder-start"','id="folder-pause"','id="folder-clear"','id="project-filter-search"','id="project-filter-status"','data-queue-mode="verified_serial"','id="jobs"',"fetch('/api/jobs')","box.id='ae-queue-wire'","data.append('audio_engine_preset'",'scene-project-heading'):
            self.assertIn(key,Q,key)
    def test_obsolete_mode_removed(self):
        for key in ('Settings View','Presets Only','presets-only','sb-settings-viewbar','sb-view-toggle','sceneBuilderPresetsOnly','applyPresetsOnly'):
            self.assertNotIn(key,T,key)
    def test_real_settings_still_accessible(self):
        for tab in ('presets','timing','media','creative','captions','audio','output'):
            self.assertIn('data-tab="'+tab+'"',T)
            self.assertIn('data-panel="'+tab+'"',T)
        for key in ('id="settings-form"','name="default_scene_preset"','id="preset-new"','id="preset-save-state"','id="auto-clean-confirm-button"',"fetch('/settings'","sessionStorage.getItem('sbSettingsTab')",'function openTab(name)'):
            self.assertIn(key,T,key)
        self.assertIn('openTab(tabs.some(t=>t.dataset.tab===initialTab)',T)
if __name__ == '__main__':
    unittest.main()

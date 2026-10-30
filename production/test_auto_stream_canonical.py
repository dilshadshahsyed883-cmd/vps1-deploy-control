"""Auto Stream canonical route preserves independent engine/OAuth and subtab state."""
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
SCENE=ROOT/"apps/scene-builder/part01"
STREAM=ROOT/"apps/short-stream/part01/auto_stream_ui.py"
LUM=ROOT/"apps/local-upload-manager/part01/templates/app.html"

class AutoStreamCanonicalTests(unittest.TestCase):
    def test_scene_native_authenticated_document(self):
        s=(SCENE/"app.py").read_text()
        body=s.split('def canonical_auto_stream(',1)[1].split('@app.get("/auto-caption/studio"',1)[0]
        self.assertIn('@app.get("/auto-stream/", response_class=HTMLResponse)',s)
        self.assertIn('require_login(request)',body)
        self.assertIn("requests.get(\"http://127.0.0.1:8145/engine/studio\"",body)
        self.assertIn('data-workspace="stream"',body)
        self.assertIn('return HTMLResponse(page',body)
        self.assertNotIn('RedirectResponse',body)
        self.assertNotIn('start_stream',body)
    def test_stream_api_and_refresh_stability(self):
        s=STREAM.read_text()
        self.assertIn("const root=", s)
        self.assertIn("/short-stream/engine", s)
        self.assertIn("sessionStorage.setItem(stateKey,id)",s)
        self.assertIn('openTab(restore);',s)
        self.assertIn('oauth-client-secret',s)
        # Auto Stream Sheets requires an explicit read-only Sheets grant.
        self.assertIn('/oauth/start?with_sheets=1',s)
    def test_native_sidebar_and_navigation(self):
        base=(SCENE/"templates/base.html").read_text()
        native=(SCENE/"static/vps1_native_site_shell.js").read_text()
        css=(SCENE/"static/vps1_native_site_shell.css").read_text()
        lum=LUM.read_text()
        self.assertIn('<nav class="vps1-experiment-tabs" aria-label="Experiment Studio">',base)
        self.assertIn('href="/local-upload-manager/app?embedded=1&amp;tab=dashboard">Upload Studio</a>',base)
        self.assertNotIn('href="/local-upload-manager">Upload Studio</a>',base)
        self.assertNotIn('<a href="/auto-stream/">Auto Stream</a>',base)
        self.assertIn('data-workspace="stream"',css)
        self.assertIn('app === "uploader" || app === "stream"',native)
        self.assertIn('vps1-native-upload-tabs',native)
        self.assertIn('addLink(tabs, "Auto Stream", "/auto-stream/", app === "stream")',native)
        self.assertIn('href="/auto-stream/"',lum)
        self.assertIn('href="{{ base_path }}/app?tab=dashboard"',lum)

if __name__=="__main__":unittest.main()

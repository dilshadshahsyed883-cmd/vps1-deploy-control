"""Canonical web navigation contracts; no live user data or external calls."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
ACS=ROOT/"apps/auto-caption-studio/part01/web_backend"
SCENE=ROOT/"apps/scene-builder/part01"

class CanonicalNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.acs_app=(ACS/"app.py").read_text()
        cls.acs_flow=(ACS/"web_flow_api.py").read_text()
        cls.studio=(ACS/"acs_studio.html").read_text()
        cls.scene=(SCENE/"app.py").read_text()
        cls.shell=(SCENE/"static/vps1_native_site_shell.js").read_text()
        cls.base=(SCENE/"templates/base.html").read_text()

    def test_full_acs_studio_served_at_root(self):
        self.assertIn('@app.get("/", response_class=HTMLResponse)',self.acs_app)
        self.assertIn('return render_studio_html()',self.acs_app)
        self.assertIn('@router.get("/studio", response_class=HTMLResponse)',self.acs_flow)
        self.assertIn('html.replace("/auto-caption/api/", "/auto-caption-studio/api/")',self.acs_flow)
        self.assertIn('id="acs-native-live"',self.studio)

    def test_native_tab_state_not_hash_navigation(self):
        self.assertIn("sessionStorage.setItem(key,id)",self.studio)
        self.assertIn("const initialPage=acsIncomingHash||savedAcsTab()",self.studio)
        self.assertIn("history.replaceState(null,'',ACS_CANONICAL_PATH)",self.studio)
        self.assertNotIn("location.replace(\"/auto-caption/studio",self.studio)
        self.assertNotIn("history.replaceState(null,'',\`#\${id}\`)",self.studio)

    def test_queue_refresh_only_when_visible(self):
        self.assertIn("if(!document.hidden && document.getElementById('queue')?.classList.contains('active'))",self.studio)
        self.assertIn("document.addEventListener('acs:tab-changed'",self.studio)

    def test_direct_main_navigation(self):
        self.assertIn('@app.get("/scene-builder/", response_class=HTMLResponse)',self.scene)
        self.assertIn('@app.get("/admin/users", response_class=HTMLResponse)',self.scene)
        self.assertIn('RedirectResponse("/auto-caption-studio/", status_code=303)',self.scene)
        self.assertIn('"/auto-caption-studio/", true',self.shell)
        self.assertIn('href="/auto-caption-studio/"',self.base)
        self.assertIn('href="/short-maker/"',self.base)
        self.assertIn('href="/video-merger/"',self.base)

if __name__=="__main__":
    unittest.main()

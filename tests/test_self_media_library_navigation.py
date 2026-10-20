"""Non-destructive, cross-app Experiment Studio / Self Media Library shell contracts."""
from __future__ import annotations
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCENE=ROOT/"apps/scene-builder/part01"
LIBRARY=ROOT/"apps/self-media-library/part01"


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags=[]
    def handle_starttag(self, tag, attrs):
        self.tags.append((tag,dict(attrs)))


class ExperimentNavigation(unittest.TestCase):
    def test_experiment_subtab_uses_existing_independent_service(self):
        base=(SCENE/"templates/base.html").read_text()
        route=(SCENE/"app.py").read_text()
        self.assertEqual(base.count('href="/experiment-studio/self-media-library/"'),1)
        self.assertIn('return RedirectResponse("/self-media-library/", status_code=303)',route)
        self.assertIn('require_login(request)',route)
        self.assertIn('"/experiment-studio/antidetect-browser/"',route)

    def test_independent_library_loads_one_native_shared_shell(self):
        page=(LIBRARY/"static/index.html").read_text()
        m=Markup()
        m.feed(page)
        scripts=[attrs for tag,attrs in m.tags if tag=="script" and attrs.get("data-workspace")=="library"]
        self.assertEqual(len(scripts),1)
        self.assertEqual(scripts[0].get("src"),"/scene-builder/static/vps1_native_site_shell.js?v=20261020-experiment-library-v3")
        styles=[attrs.get("href") for tag,attrs in m.tags if tag=="link"]
        self.assertIn("/scene-builder/static/vps1_native_site_shell.css?v=20261020-experiment-library-v3",styles)
        self.assertIn("/self-media-library/static/styles.css?v=20261020-legacy-v2",styles)
        self.assertIn("/self-media-library/static/sml_shared_theme.css?v=20261020-shared-light-v1",styles)
        scripts_all=[attrs.get("src") for tag,attrs in m.tags if tag=="script"]
        self.assertIn("/self-media-library/static/app.js?v=20261020-mobile-shell-v3",scripts_all)
        self.assertNotIn('<aside class="sidebar">',page)
        self.assertEqual(page.count('id="nav"'),1)
        for item in ("dashboard","sources","processing","library","settings"):
            self.assertEqual(page.count('data-page="'+item+'"'),1)
        for element in ("healthDot","healthText","clipSearch","imagesGrid","sourceFile"):
            self.assertIn('id="'+element+'"',page)
        # The app has its own controls; website-level rail is only in the shared shell.
        self.assertNotIn("<iframe",page.lower())
        self.assertTrue(page.count("<main>")==1)
        self.assertEqual(page.count("</main>"),1)

    def test_experiment_shell_links_current_media_application(self):
        script=(SCENE/"static/vps1_native_site_shell.js").read_text()
        self.assertIn('app === "library" ? "experiment"',script)
        self.assertIn('if (app === "library")',script)
        self.assertIn('addLink(tabs, "Self Media Library", "/self-media-library/", true)',script)
        css=(SCENE/"static/vps1_native_site_shell.css").read_text()
        for selector in (".vps1-native-experiment-tabs","body.vps1-native-library .app-shell",
                         "body.vps1-native-library .sml-control-rail"):
            self.assertIn(selector,css)

if __name__=="__main__":
    unittest.main()

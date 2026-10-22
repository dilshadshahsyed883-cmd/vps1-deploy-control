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
        self.assertEqual(scripts[0].get("src"),"/static/vps1_native_site_shell.js?v=20261020-experiment-ui-v5")
        styles=[attrs.get("href") for tag,attrs in m.tags if tag=="link"]
        self.assertIn("/static/vps1_native_site_shell.css?v=20261020-experiment-ui-v5",styles)
        self.assertIn("/self-media-library/static/styles.css?v=20261020-legacy-v2",styles)
        self.assertIn("/self-media-library/static/sml_shared_theme.css?v=20261022-yt-queue-pair-batch-v1",styles)
        scripts_all=[attrs.get("src") for tag,attrs in m.tags if tag=="script"]
        self.assertIn("/self-media-library/static/app.js?v=20261022-image-sharp-v2-yt-queue-v1",scripts_all)
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

class PublicGatewayAssets(unittest.TestCase):
    def test_actual_gateway_rewrite_resolves_every_asset(self):
        # Exercise the same quote-prefix substitutions as production.gateway,
        # then parse like the browser. Checking loopback content misses this bug.
        from urllib.parse import urlsplit
        page=(LIBRARY/'static/index.html').read_text()
        for root in ('api/','static/','media/','outputs/','jobs','live'):
            for quote in ('"', "'", '`'):
                page=page.replace(quote+'/'+root,quote+'/self-media-library/'+root)
        parsed=Markup(); parsed.feed(page)
        shell_assets=[]
        for tag,attrs in parsed.tags:
            url=attrs.get('src') if tag=='script' else attrs.get('href') if tag=='link' else None
            if not url: continue
            path=urlsplit(url).path
            if path.startswith('/static/'):
                resolved=SCENE/path.lstrip('/')
                shell_assets.append(path)
            else:
                self.assertTrue(path.startswith('/self-media-library/static/'),path)
                resolved=LIBRARY/path.removeprefix('/self-media-library/')
            self.assertTrue(resolved.is_file(),f'Public asset {path} cannot be served')
        self.assertEqual(len(shell_assets),2)

    def test_inactive_browser_preview_remains_hidden(self):
        template=(SCENE/'templates/experiment_studio.html').read_text()
        self.assertIn('#experiment-browser [hidden]{display:none!important}',template)
        tags=Markup();tags.feed(template)
        preview=next(a for t,a in tags.tags if a.get('id')=='browser-preview')
        self.assertIn('hidden',preview)

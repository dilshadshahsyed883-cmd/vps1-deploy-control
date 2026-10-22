"""CW-003 Self Media Library responsive shell: catch route, CSS and API regressions."""
from __future__ import annotations
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
SML=ROOT/"apps/self-media-library/part01"
SCENE=ROOT/"apps/scene-builder/part01"

class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts=[]
        self.styles=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=="script" and a.get("src"): self.scripts.append((a["src"],a))
        if tag=="link" and a.get("rel")=="stylesheet":self.styles.append(a.get("href",""))

class SharedShellMobile(unittest.TestCase):
    def test_assets_use_only_existing_canonical_mounts(self):
        s=(SML/"static/index.html").read_text(encoding="utf-8")
        tags=Assets();tags.feed(s)
        scene_css="/static/vps1_native_site_shell.css?v=20261020-experiment-ui-v5"
        scene_js="/static/vps1_native_site_shell.js?v=20261020-experiment-ui-v5"
        sml_css="/self-media-library/static/sml_shared_theme.css?v=20261022-yt-queue-pair-batch-v1"
        assert (SCENE/"static/vps1_native_site_shell.css").is_file()
        assert (SCENE/"static/vps1_native_site_shell.js").is_file()
        assert (SML/"static/sml_shared_theme.css").is_file()
        self.assertIn(scene_css,tags.styles)
        self.assertIn(sml_css,tags.styles)
        self.assertIn(scene_js,[p for p,_ in tags.scripts])
        self.assertIn("/self-media-library/static/app.js?v=20261022-image-bulk-v1",
                      [p for p,_ in tags.scripts])
        self.assertEqual(len([a for _,a in tags.scripts if a.get("data-workspace")=="library"]),1)
        self.assertNotIn('src="/static/',s)
        self.assertNotIn('href="/static/',s)
        self.assertLess(tags.styles.index(scene_css),tags.styles.index(sml_css))
        self.assertNotIn("<iframe",s.lower())
        self.assertIn('class="vps1-native-library"',s)

    def test_shared_light_design_and_mobile_alignment_override_old_dock(self):
        css=(SML/"static/sml_shared_theme.css").read_text(encoding="utf-8")
        scene=(SCENE/"static/vps1_native_site_shell.css").read_text(encoding="utf-8")
        js=(SCENE/"static/vps1_native_site_shell.js").read_text(encoding="utf-8")
        for token in ("#f8fafc","#315edb","#dfe5ef","color-scheme:light",
                      "body.vps1-native-library .app-shell",
                      "body.vps1-native-library .sml-control-rail",
                      "body.vps1-native-library .topbar",
                      "flex-flow:row nowrap!important",
                      "font:750 13px/1.2",
                      "@media(max-width:760px)",
                      "min-width:0!important"):
            self.assertIn(token,css,token)
        self.assertIn(".vps1-native-experiment-tabs",scene)
        self.assertIn('if (app === "library")',js)
        self.assertIn('"Experiment Studio", "/experiment-studio/"',js)
        self.assertIn('addLink(tabs, "Self Media Library", "/self-media-library/", true)',js)
        self.assertIn('--vps1-sidebar-width:214px',scene)
        source=(SML/"static/index.html").read_text()
        self.assertNotIn('<aside class="sidebar">',source)
        for page in ("dashboard","sources","processing","library","settings"):
            self.assertIn('data-page="'+page+'"',source)

    def test_api_and_media_calls_are_single_prefix_even_after_nginx_rewrite(self):
        js=(SML/"static/app.js").read_text(encoding="utf-8")
        for m in ("const libraryPath = path =>", "const mediaUrl = path =>",
                  "fetch(libraryPath(url)", "credentials:'same-origin'",
                  "mediaUrl(c.clip_url)", "mediaUrl(c.thumb_url)",
                  "mediaUrl(i.thumb_url)", "mediaUrl(selectedImage.content_url)"):
            self.assertIn(m,js,m)
        self.assertIn('Website login required',js)
        if not shutil.which("node"):
            self.skipTest("node unavailable")
        extractor=(
            "const fs=require('fs'),vm=require('vm'),assert=require('assert');"
            "const s=fs.readFileSync(process.argv[1],'utf8');"
            "const m=s.match(/const libraryPath = path => \\{[\\s\\S]*?\\n\\};/);"
            "assert(m,'SML URL resolver missing');"
            "const fn=vm.runInNewContext(m[0]+'; libraryPath;');"
            "assert.equal(fn('/api/health'),'/self-media-library/api/health');"
            "assert.equal(fn('/self-media-library/api/health'),'/self-media-library/api/health');"
            "assert.equal(fn('/media/clips/x.mp4'),'/self-media-library/media/clips/x.mp4');"
            "assert.throws(()=>fn('https://invalid.test/api/health'));"
        )
        r=subprocess.run(["node","-e",extractor,str(SML/"static/app.js")],
                          capture_output=True,text=True,timeout=15)
        self.assertEqual(r.returncode,0,r.stderr)
        r=subprocess.run(["node","--check",str(SML/"static/app.js")],
                          capture_output=True,text=True,timeout=15)
        self.assertEqual(r.returncode,0,r.stderr)

if __name__=="__main__": unittest.main()

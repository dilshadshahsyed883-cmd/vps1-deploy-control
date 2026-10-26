"""Auto Script Kokoro skin regression: source/logic untouched; 390-1440 responsive UI."""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
AUTOSCRIPT = ROOT / "apps/auto-script"
BASE = SCENE / "templates/base.html"
OVERRIDE = SCENE / "static/auto_script_kokoro_20261026.css"
TEMPLATE = AUTOSCRIPT / "templates/auto_script.html"
SOURCE_CSS = AUTOSCRIPT / "static/auto_script.css"
SOURCE_JS = AUTOSCRIPT / "static/auto_script.js"


class AutoScriptKokoroUI(unittest.TestCase):
    def test_override_loaded_only_for_auto_script_after_source(self):
        base = BASE.read_text(encoding="utf-8")
        self.assertIn("{% if page=='auto_script' %}<link rel=\"stylesheet\" href=\"/static/auto_script_kokoro_20261026.css?v=1\">{% endif %}", base)
        self.assertLess(base.index("{% block content %}{% endblock %}"), base.index("auto_script_kokoro_20261026.css?v=1"))
        self.assertIn('class="vps1-experiment-tabs"', base)
        self.assertIn('href="/experiment-studio/auto-script/"', base)

    def test_source_manifest_valid_and_semantics_preserved(self):
        from rebuild.stage_auto_script import validated_files
        verified = validated_files()
        self.assertEqual(set(verified), {"auto_script.py", "templates/auto_script.html",
                                         "static/auto_script.js", "static/auto_script.css"})
        html = TEMPLATE.read_text(encoding="utf-8")
        js = SOURCE_JS.read_text(encoding="utf-8")
        for id_ in ("as-add-channel", "as-run-all", "as-channel-form", "as-channels-body",
                    "as-items-body", "as-modal", "as-stat-channels", "as-errors"):
            self.assertIn('id="' + id_ + '"', html)
        for endpoint in ("/api/auto-script/state", "/api/auto-script/channels",
                         "/api/auto-script/tick"):
            self.assertIn(endpoint, js)

    def test_no_global_or_backend_styles(self):
        css = OVERRIDE.read_text(encoding="utf-8")
        self.assertIn("AS-UI-KOKORO-1440-20261026", css)
        for token in ("max-width: 1400px", "min-height: 44px", "grid-template-columns",
                      "max-width: 1180px", "max-width: 900px", "max-width: 600px",
                      ":focus-visible", "prefers-reduced-motion"):
            self.assertIn(token, css)
        for unsafe in ("fetch(", "localStorage", "sessionStorage", "XMLHttpRequest",
                       "#app-sidebar", "@import"):
            self.assertNotIn(unsafe, css)
        for selector in re.findall(r"(?m)^([^@/\n][^\n]*?)\s*\{", css):
            selector = selector.strip()
            if selector.startswith("--"):
                continue
            self.assertTrue(
                selector.startswith(("#auto-script-app", "#as-modal", "body:has(#auto-script-app)")),
                "Out-of-scope selector: " + selector)


def browser_acceptance():
    from playwright.sync_api import sync_playwright, expect
    markup = TEMPLATE.read_text(encoding="utf-8")
    fragment = markup.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
    fragment = re.sub(r'<link rel="stylesheet" href="/static/auto_script.css\?v=2">', "", fragment)
    fragment = re.sub(r'<script src="/static/auto_script.js\?v=2"></script>', "", fragment)
    fixture = (
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>body{margin:0;background:#f8fafc;font:13px Inter,system-ui,sans-serif}'
        'main{box-sizing:border-box;padding:26px 34px 42px;min-width:0}'
        'main>header,main>nav{max-width:1400px;margin:0 auto 16px}'
        'main>nav{height:54px;border:1px solid #e2e7ef;border-radius:12px}'
        '@media(min-width:761px){main{margin-left:192px}}'
        '@media(max-width:760px){main{padding:16px 14px 24px}}</style>'
        '<style>' + SOURCE_CSS.read_text(encoding="utf-8") + '</style>'
        '<style>' + OVERRIDE.read_text(encoding="utf-8") + '</style></head>'
        '<body><main><nav></nav><header><h1>Auto Script</h1></header>'
        + fragment + '</main></body></html>'
    )
    state = {"scheduler": True, "channels": [{
        "id": 1, "name": "Example Channel", "timezone": "Asia/Karachi",
        "sheet_gid": "0", "daily_limit": 5, "today_count": 1, "enabled": True,
        "tabs_used": 0, "scene_tabs_used": 0, "max_tabs_per_doc": 50,
    }], "items": [], "documents": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width in (390, 768, 1024, 1440):
            page = browser.new_page(viewport={"width": width, "height": 900})
            errors, writes = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            def route(request):
                path = request.request.url
                if request.request.method != "GET":
                    writes.append(path)
                    return request.fulfill(status=405)
                if path.endswith("/auto-script"):
                    return request.fulfill(content_type="text/html", body=fixture)
                if path.endswith("/api/auto-script/state"):
                    return request.fulfill(content_type="application/json", body=json.dumps(state))
                return request.fulfill(status=404)
            page.route("**/*", route)
            page.goto("http://ui.test/auto-script")
            page.add_script_tag(content=SOURCE_JS.read_text(encoding="utf-8"))
            expect(page.locator("#as-stat-channels")).to_have_text("1")
            expect(page.locator("#as-scheduler")).to_have_text("Scheduler active")
            expect(page.locator("#as-channels-body .as-channel-name")).to_have_text("Example Channel")
            expect(page.locator(".as-stats > div")).to_have_count(4)
            expect(page.locator("#as-run-all")).to_have_css("min-height", "44px")
            expect(page.locator(".as-card").first).to_have_css("border-radius", "16px")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, "page overflow")
            if width >= 901:
                a = page.locator(".as-stats > div").nth(0).bounding_box()
                b = page.locator(".as-stats > div").nth(3).bounding_box()
                assert b["x"] > a["x"], (width, "four stat columns")
            else:
                a = page.locator(".as-stats > div").nth(0).bounding_box()
                b = page.locator(".as-stats > div").nth(2).bounding_box()
                assert b["y"] > a["y"], (width, "stacked stat rows")
            page.locator("#as-add-channel").click()
            expect(page.locator("#as-modal")).to_be_visible()
            expect(page.locator("#as-name")).to_have_css("min-height", "44px")
            expect(page.locator("#as-modal .as-modal")).to_have_css("border-radius", "16px")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, "modal overflow")
            page.locator("#as-modal-close").click()
            expect(page.locator("#as-modal")).to_be_hidden()
            assert writes == [], (width, writes)
            assert errors == [], (width, errors)
            print(f"AUTO_SCRIPT_KOKORO_UI=PASS WIDTH={width} SOURCE=UNCHANGED WRITES=0")
            page.close()
        browser.close()


if __name__ == "__main__":
    if "--browser" in sys.argv:
        browser_acceptance()
    else:
        unittest.main()

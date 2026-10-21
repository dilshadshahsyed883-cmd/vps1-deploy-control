"""EXP-UI-20261021-01: isolated static and Chromium UI regressions; no live writes."""
from pathlib import Path
import json
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
PART = ROOT / "apps/scene-builder/part01"
TEMPLATE = PART / "templates/experiment_studio.html"
CSS = PART / "static/experiment_antidetect_kokoro_20261021.css"
JS = PART / "static/experiment_browser.js"


class AntidetectKokoroUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = TEMPLATE.read_text(encoding="utf-8")
        cls.css = CSS.read_text(encoding="utf-8")
        cls.js = JS.read_text(encoding="utf-8")

    def test_preserve_all_profile_fields_and_session_controls(self):
        for field in ("name","start_url","proxy_server","proxy_username","proxy_password",
                      "locale","timezone","user_agent","privacy_mode","do_not_track",
                      "geolocation_mode","geo_lat","geo_lon","device_scale",
                      "color_scheme","reduced_motion"):
            self.assertRegex(self.html, r'name="' + re.escape(field) + '"')
        for action in ("start","stop","delete","clone","privacy","connection-check",
                       "go","reload","back","type","enter","scroll-up","scroll-down",
                       "refresh-preview"):
            self.assertEqual(1, self.html.count('id="browser-' + action + '"'))
        self.assertIn('id="browser-create-form"', self.html)
        self.assertIn('id="browser-profiles"', self.html)
        self.assertIn('id="browser-preview"', self.html)
        self.assertIn('data-csrf="{{ experiment_csrf }}"', self.html)

    def test_css_isolated_from_shell_and_other_apps(self):
        self.assertIn('experiment_antidetect_kokoro_20261021.css?v=1', self.html)
        self.assertIn('.experiment-browser-session-toolbar', self.css)
        self.assertIn('.experiment-browser-fields', self.css)
        self.assertIn('.experiment-profile.is-selected', self.css)
        for token in ("min-height: 44px", "border-radius: 16px",
                      "@media(max-width: 1000px)", "@media(max-width: 600px)",
                      ":focus-visible", "prefers-reduced-motion"):
            self.assertIn(token, self.css)
        for selector in re.findall(r'(?m)^([^@/\n][^\n]*?)\s*\{', self.css):
            if selector.strip().startswith("--"): continue
            self.assertTrue(selector.strip().startswith("#experiment-browser"),
                            "Non-app-scoped selector: " + selector)
        for unsafe in ("fetch(", "localStorage", "sessionStorage", "XMLHttpRequest",
                       "vps1_native_site_shell", "document.body", "#app-sidebar"):
            self.assertNotIn(unsafe, self.css)

    def test_existing_controller_api_semantics_preserved(self):
        for marker in ('const endpoint = "/api/experiment-browser"',
                       'new FormData(event.currentTarget)',
                       'const data = Object.fromEntries(form.entries())',
                       'await api(requireSelection() + "/start", "POST")',
                       'await api(requireSelection() + "/stop", "POST")',
                       'await api(requireSelection(), "DELETE")',
                       'if (!window.confirm' ):
            if marker == 'if (!window.confirm':
                self.assertIn('window.confirm("Delete this stopped browser profile',self.js)
            else:
                self.assertIn(marker, self.js)
        self.assertIn('button.setAttribute("aria-pressed"', self.js)
        self.assertIn('row.classList.toggle("is-selected"', self.js)


def browser_acceptance():
    from playwright.sync_api import sync_playwright, expect
    template = TEMPLATE.read_text(encoding="utf-8")
    fragment = template.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
    fragment = fragment.replace('{{ experiment_csrf }}', "ui-test-csrf")
    fragment = fragment.replace(
        '<link rel="stylesheet" href="/static/experiment_antidetect_kokoro_20261021.css?v=1">',
        "<style>" + CSS.read_text(encoding="utf-8") + "</style>")
    # Deliberately add conflicting shell-like !important styles, without altering the shell.
    shell_fixture = '<style>label{margin-bottom:8px!important}input[type="text"],input[type="password"]{margin-bottom:16px!important;padding:10px 14px!important}</style>'
    fixture = '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">' + shell_fixture + '</head><body>' + fragment + '</body></html>'
    data = {"profiles": [
        {"id":"profile-1","name":"Research Alpha","state":"stopped","locale":"en-US",
         "privacy_mode":"strict","proxy_server":"http://proxy.invalid:8000",
         "do_not_track":True,"geolocation_mode":"deny","start_url":"about:blank"},
        {"id":"profile-2","name":"Research Beta","state":"stopped","locale":"en-US",
         "privacy_mode":"standard","proxy_server":"",
         "do_not_track":False,"geolocation_mode":"deny","start_url":"https://example.com"}]}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for width in (390, 768, 1440):
            page = browser.new_page(viewport={"width":width,"height":940})
            errors = []
            writes = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            def route(req):
                url = req.request.url
                if req.request.method != "GET":
                    writes.append(url)
                    return req.fulfill(status=405)
                if url.endswith("/experiment"):
                    return req.fulfill(content_type="text/html",body=fixture)
                if url.endswith("/api/experiment-browser/health"):
                    return req.fulfill(content_type="application/json",body=json.dumps({"chrome_installed":True}))
                if url.endswith("/api/experiment-browser/profiles"):
                    return req.fulfill(content_type="application/json",body=json.dumps(data))
                return req.fulfill(status=404)
            page.route("**/*", route)
            page.goto("http://ui.test/experiment")
            page.add_script_tag(content=JS.read_text(encoding="utf-8"))
            expect(page.locator(".experiment-profile")).to_have_count(2)
            expect(page.locator(".experiment-browser-panel")).to_have_count(2)
            expect(page.locator("#browser-engine-status")).to_contain_text("Browser engine available")
            expect(page.locator("#browser-profile-name")).to_have_css("min-height", "44px")
            expect(page.locator("#browser-start")).to_have_css("min-height", "44px")
            expect(page.locator("#browser-create-form")).to_be_visible()
            expect(page.locator("#browser-proxy-server")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width,"horizontal overflow")
            for field in ("browser-profile-name","browser-proxy-server","browser-proxy-pass",
                          "browser-profile-locale","browser-profile-timezone"):
                assert page.locator("#"+field).count()==1
            if width >= 1001:
                a=page.locator(".experiment-browser-panel").nth(0).bounding_box()
                b=page.locator(".experiment-browser-panel").nth(1).bounding_box()
                assert b["x"]>a["x"],(width,"desktop layout")
            else:
                a=page.locator(".experiment-browser-panel").nth(0).bounding_box()
                b=page.locator(".experiment-browser-panel").nth(1).bounding_box()
                assert b["y"]>a["y"],(width,"stacked layout")
            page.locator(".experiment-profile").nth(1).get_by_role("button",name="Select").click()
            expect(page.locator(".experiment-profile.is-selected")).to_have_count(1)
            expect(page.locator(".experiment-profile.is-selected")).to_contain_text("Research Beta")
            expect(page.locator("#browser-start")).to_be_enabled()
            expect(page.locator("#browser-address")).to_have_value("https://example.com")
            page.locator("#browser-proxy-server").fill("http://local.example:8080")
            expect(page.locator("#browser-proxy-server")).to_have_value("http://local.example:8080")
            assert not writes, (width, writes)
            assert not errors, (width, errors)
            print(f"EXPERIMENT_KOKORO_UI=PASS WIDTH={width} PROFILE_SELECTION=PASS HTTP_WRITES=0")
            page.close()
        browser.close()

if __name__ == "__main__":
    if "--browser" in sys.argv:
        browser_acceptance()
    else:
        unittest.main()

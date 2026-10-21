"""Screenshot regression: Scene Antidetect must share Kokoro 1400px alignment.
Uses the real classic Scene and native shell styles, with no account or VPS writes.
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "apps/scene-builder/part01/static"
TEMPLATE = ROOT / "apps/scene-builder/part01/templates/experiment_studio.html"

class GeometryContract(unittest.TestCase):
    def test_experiment_only(self):
        css = (STATIC / "experiment_antidetect_kokoro_20261021.css").read_text()
        html = TEMPLATE.read_text()
        self.assertIn("body:has(#experiment-browser) > main", css)
        self.assertIn("max-width: none !important", css)
        self.assertIn("body:has(#experiment-browser) > main > header", css)
        self.assertIn("experiment_antidetect_kokoro_20261021.css?v=2", html)
        self.assertIn('id="browser-create-form"', html)
        self.assertIn('id="browser-preview"', html)

def chromium_geometry():
    from playwright.sync_api import sync_playwright
    html = TEMPLATE.read_text().split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
    html = html.replace("{{ experiment_csrf }}", "test-csrf")
    html = html.replace('<link rel="stylesheet" href="/static/experiment_antidetect_kokoro_20261021.css?v=2">',
        "<style>" + (STATIC / "experiment_antidetect_kokoro_20261021.css").read_text() + "</style>")
    page_html = (
        '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<style>" + (STATIC / "styles.css").read_text() + "</style>"
        "<style>" + (STATIC / "vps1_native_site_shell.css").read_text() + "</style>"
        '</head><body><aside id="app-sidebar" class="sidebar"><nav><a href="#">Experiment Studio</a></nav></aside>'
        '<main><nav class="vps1-experiment-tabs"><a href="#">Antidetect Browser</a></nav>'
        '<header><div><h1>Experiment Studio</h1><p>Independent experiments</p></div></header>'
        + html + "</main></body></html>"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in (390, 768, 1440, 1660, 1908):
            page = browser.new_page(viewport={"width":width,"height":940})
            page.route("**/*", lambda request: request.fulfill(content_type="text/html",body=page_html)
                       if request.request.url.endswith("/experiment") else request.abort())
            page.goto("http://fixture.test/experiment")
            positions = page.evaluate("""() => {
                const bounds = selector => document.querySelector(selector).getBoundingClientRect();
                const tab = bounds(".vps1-experiment-tabs");
                const header = bounds("main > header");
                const content = bounds("#experiment-browser");
                return { clientWidth: document.documentElement.clientWidth,
                    sidebarRight: bounds("#app-sidebar").right,
                    tabX: tab.left, tabWidth: tab.width,
                    headerX: header.left, contentX: content.left, contentWidth: content.width,
                    mainMax: getComputedStyle(document.querySelector("main")).maxWidth,
                    mainX: bounds("main").left,
                    mainWidth: bounds("main").width,
                    mainPaddingLeft: parseFloat(getComputedStyle(document.querySelector("main")).paddingLeft),
                    mainPaddingRight: parseFloat(getComputedStyle(document.querySelector("main")).paddingRight),
                    noOverflow: document.documentElement.scrollWidth <= innerWidth };
            }""")
            assert positions["noOverflow"], (width, positions)
            assert positions["contentWidth"] <= 1401, (width, positions)
            assert abs(positions["headerX"] - positions["tabX"]) < 2, (width, positions)
            assert abs(positions["contentX"] - positions["tabX"]) < 2, (width, positions)
            if width >= 1600:
                available = positions["mainWidth"] - positions["mainPaddingLeft"] - positions["mainPaddingRight"]
                expected = positions["mainX"] + positions["mainPaddingLeft"] + (available - positions["tabWidth"]) / 2
                assert abs(positions["tabX"] - expected) < 2, (width, positions, expected)
                assert positions["mainMax"] == "none", (width, positions)
                if width >= 1800:
                    assert abs(positions["tabWidth"] - 1400) < 2, (width, positions)
                    assert positions["tabX"] > 300, (width, positions)
                else:
                    assert positions["tabWidth"] >= 1300, (width, positions)
            print("ANTIDETECT_KOKORO_GEOMETRY=PASS WIDTH=" + str(width), flush=True)
            page.close()
        browser.close()

if __name__ == "__main__":
    if "--browser" in sys.argv:
        chromium_geometry()
    else:
        unittest.main()

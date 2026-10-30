"""Real headless Chromium regression gate for shared VPS1 Studio navigation.

Loads actual committed shared CSS/JS and app-owned tab markup. This uses real
browser layout/pixel screenshots, with synthetic *non-authenticated* content:
it is NOT a substitute for protected authenticated production UAT.
"""
from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
import os
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
CSS = SCENE / "static/vps1_native_site_shell.css"
VIDEO_CSS = SCENE / "static/vps1_video_studio_geometry.css"
JS = SCENE / "static/vps1_native_site_shell.js"
VIDEOS = {
    "scene-builder": SCENE / "templates/base.html",
    "video-merger": ROOT / "apps/video-merger/part01/templates/index.html",
    "auto-caption-studio": None,  # Native tabs are constructed by actual JS.
}
NATIVE = ("auto-caption-studio", "auto-uploader", "auto-stream", "voice-studio")
VIEWPORTS = ((1440, 900), (1024, 768), (768, 1024), (390, 844), (360, 780))
TOLERANCE_PX = 2.0
PIXEL_THRESHOLD = 0.02


def extract_video_nav(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    match = re.search(r'<nav data-video-studio-subnav="1"[\s\S]*?</nav>', source)
    if not match:
        raise ValueError(f"{path}: canonical four-tab HTML missing")
    # Template conditionals are not valid in a standalone no-server fixture.
    return re.sub(r'{[%{][\s\S]*?[}%]}', '', match.group(0))


def extract_experiment_nav() -> str:
    """Render the actual second-level rail in independent fixture pages."""
    source = (SCENE / "templates/base.html").read_text(encoding="utf-8")
    match = re.search(r'<nav class="vps1-experiment-tabs" aria-label="Experiment Studio">.*?</nav>',
                      source, flags=re.S)
    if not match:
        raise ValueError("Scene Experiment Studio second-level navigation missing")
    return re.sub(r'{[%{].*?[}%]}', '', match.group(0), flags=re.S)


def page_html(app: str) -> str:
    if app in VIDEOS and VIDEOS[app]:
        nav = extract_video_nav(VIDEOS[app])
        return (
            '<!doctype html><html><head><meta name="viewport" '
            'content="width=device-width,initial-scale=1"></head>'
            '<body><aside id="app-sidebar" class="sidebar">'
            '<nav aria-label="Applications"><a>Video Studio</a></nav></aside>'
            f'<main>{extract_experiment_nav()}{nav}<section data-fixture-content="1"></section></main>'
            '</body></html>'
        )
    # Independently routed apps retain their own DOM and native shell JS.
    header = "<header data-app-header='1'>Native app header</header>"
    return (
        '<!doctype html><html><head><meta name="viewport" '
        'content="width=device-width,initial-scale=1"></head>'
        f'<body>{header}<div class="shell"><div class="workspace"></div></div>'
        '</body></html>'
    )


def normalize_tabs(page, selector: str) -> None:
    page.locator(selector).first.evaluate(
        """(rail) => {
            rail.querySelectorAll('a').forEach((a, i) => {
                a.removeAttribute('style');
                a.classList.toggle('active', i === 0);
                if (i === 0) a.setAttribute('aria-current', 'page');
                else a.removeAttribute('aria-current');
            });
        }"""
    )


def changed_pixel_ratio(first: bytes, second: bytes) -> float:
    from PIL import Image, ImageChops
    a = Image.open(BytesIO(first)).convert("RGB")
    b = Image.open(BytesIO(second)).convert("RGB")
    if a.size != b.size:
        return 1.0
    # Compare normalized colors with anti-aliasing tolerance.
    diff = ImageChops.difference(a, b)
    pixels = list(diff.getdata())
    return sum(max(rgb) > 20 for rgb in pixels) / max(1, len(pixels))


def verify_actual_short_maker_identity(browser, shell_js: str, shell_css: str,
                                       geometry_css: str) -> list[str]:
    """Serve the real Short Maker HTML offline with both session roles in Chromium.

    The existing CSS geometry fixture alone cannot detect a missing Admin/account
    footer, nor can it detect an actual-page JS bootstrap failure.
    """
    source = (ROOT / "apps/short-maker/part01/app.py").read_text(encoding="utf-8")
    try:
        html = source.split("HTML = r'''", 1)[1].split("'''", 1)[0]
    except IndexError:
        return ["short-maker: actual embedded HTML fixture unavailable"]
    errors = []
    origin = "https://app.scenebuilder.online"
    for identity in (
        {"authenticated": True, "is_admin": True, "username": "fixture-admin"},
        {"authenticated": True, "is_admin": False, "username": "fixture-member"},
        None,
    ):
        role = "admin" if identity and identity["is_admin"] else (
            "member" if identity else "no-session")
        context = browser.new_context(viewport={"width": 1440, "height": 900},
                                      reduced_motion="reduce")
        try:
            def route_request(route):
                url = route.request.url
                if url.split("?", 1)[0] == origin + "/short-maker/":
                    route.fulfill(status=200, content_type="text/html", body=html)
                elif "/static/vps1_native_site_shell.js" in url:
                    route.fulfill(status=200, content_type="application/javascript",
                                  body=shell_js)
                elif "/static/vps1_native_site_shell.css" in url:
                    route.fulfill(status=200, content_type="text/css",
                                  body=shell_css)
                elif "/static/vps1_video_studio_geometry.css" in url:
                    route.fulfill(status=200, content_type="text/css",
                                  body=geometry_css)
                elif "/api/session/identity" in url:
                    route.fulfill(status=200 if identity else 401,
                                  content_type="application/json",
                                  body=__import__("json").dumps(identity or
                                      {"detail": "Authentication required"}))
                else:
                    route.fulfill(status=200, content_type="application/json",
                                  body='{"jobs":[],"counts":{}}')
            context.route("**/*", route_request)
            page = context.new_page()
            failures = []
            page.on("pageerror", lambda err: failures.append(str(err)))
            page.goto(origin + "/short-maker/", wait_until="load", timeout=15000)
            account = page.locator("#app-sidebar .sidebar-account")
            account.wait_for(state="visible", timeout=10000)
            page.wait_for_function(
                """() => {
                    const x=document.querySelector('#app-sidebar .sidebar-account');
                    return x && ['verified','unavailable'].includes(
                       x.dataset.vps1IdentityStatus);
                }""", timeout=10000)
            admin_count = page.locator('#app-sidebar nav a[href="/admin"]').count()
            if admin_count != (1 if identity and identity["is_admin"] else 0):
                errors.append(f"short-maker {role}: Admin visibility={admin_count}")
            if identity:
                if account.locator(".sidebar-account-name").inner_text() != identity["username"]:
                    errors.append(f"short-maker {role}: user identity missing")
                if account.locator('form[action="/logout"]').is_hidden():
                    errors.append(f"short-maker {role}: logout missing")
                if not account.locator(".vps1-session-login").is_hidden():
                    errors.append(f"short-maker {role}: login shown while signed in")
            else:
                if account.locator(".vps1-session-login").is_hidden():
                    errors.append("short-maker no-session: sign-in recovery missing")
                if not account.locator('form[action="/logout"]').is_hidden():
                    errors.append("short-maker no-session: logout exposed")
            if failures:
                errors.append(f"short-maker {role}: browser JS error {failures[:2]}")
        except Exception as exc:
            errors.append(f"short-maker {role}: {type(exc).__name__}: {exc}")
        finally:
            context.close()
    return errors



def verify_song_navigation(browser, shell_js: str, shell_css: str) -> list[str]:
    """Exercise separate Audio Downloader and Voice URLs with real shared navigation."""
    html = (ROOT / "apps/voice-studio/part01/templates/voice_studio.html").read_text()
    canonical_html = html
    for old, new, count in (
        ("(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/'+'tts/api/audio-downloader'",
         "'/voice-studio/tts/api/audio-downloader'", 1),
        ("(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/'+'tts/api/audio-downloader/proxy'",
         "'/voice-studio/tts/api/audio-downloader/proxy'", 1),
        ("document.documentElement.dataset.vsEngine='audio-downloader';history.replaceState(null,'','#vs-audio');tab('downloader')",
         "document.documentElement.dataset.vsEngine='audio-downloader';history.replaceState(null,'','/audio-downloader/');tab('downloader')", 1),
        ("drivePath();if(location.hash==='#vs-audio')choose(true);",
         "drivePath();if(location.pathname==='/audio-downloader/' || location.hash==='#vs-audio')choose(true);", 1),
        ('<title>Voice Studio · VPS1</title>',
         '<title>Audio Downloader · Song Studio</title>', 1),
        ('vps1_native_site_shell.js?v=20261029-song-studio-v1',
         'vps1_native_site_shell.js?v=20261030-audio-url-v1', 1),
    ):
        if canonical_html.count(old) != count:
            raise RuntimeError("canonical URL fixture differs from reviewed Voice HTML")
        canonical_html = canonical_html.replace(old, new)
    from jinja2 import ChainableUndefined, Environment, FileSystemLoader
    scene_env = Environment(loader=FileSystemLoader(SCENE / "templates"),
                            undefined=ChainableUndefined)
    experiment_html = scene_env.get_template("base.html").render(
        request={"session": {"is_admin": False, "app_access": "all"}},
        page="experiment_studio", username="fixture")
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    errors = []
    origin = "https://app.scenebuilder.online"
    try:
        def route_request(route):
            url = route.request.url
            if url.split("?", 1)[0] == origin + "/voice-studio/app":
                route.fulfill(content_type="text/html", body=html)
            elif url.split("?", 1)[0] == origin + "/song-studio/":
                route.fulfill(content_type="text/html", body='<script>location.replace("/audio-downloader/")</script>')
            elif url.split("?", 1)[0] == origin + "/audio-downloader/":
                route.fulfill(content_type="text/html", body=canonical_html)
            elif url.split("?", 1)[0] == origin + "/experiment-studio/":
                route.fulfill(content_type="text/html", body=experiment_html)
            elif "/static/vps1_native_site_shell.js" in url:
                route.fulfill(content_type="application/javascript", body=shell_js)
            elif "/static/vps1_native_site_shell.css" in url:
                route.fulfill(content_type="text/css", body=shell_css)
            else:
                route.fulfill(content_type="application/json", body=
                    '{"authenticated":true,"is_admin":false,"username":"fixture",'
                    '"jobs":[],"voices":[],"profiles":[],"counts":{}}')
        context.route("**/*", route_request)
        page = context.new_page()
        page.goto(origin + "/audio-downloader/", wait_until="load")
        if page.url != origin + "/audio-downloader/":
            errors.append("song-studio: canonical URL changed unexpectedly")
        page.locator('.vps1-native-song-tabs').wait_for(state="visible")
        song = page.locator('#vps1-native-site-sidebar nav a[href="/song-studio/"]')
        experiment = page.locator('#vps1-native-site-sidebar nav a[href="/experiment-studio/"]')
        if song.get_attribute("aria-current") != "page":
            errors.append("song-studio: initial Audio Downloader selection missing")
        if page.locator('.vs-engine-selector').is_visible():
            errors.append("song-studio: Voice engine tabs leaked into Audio Downloader")
        page.set_viewport_size({"width": 390, "height": 844})
        if page.locator('.vs-engine-selector').is_visible():
            errors.append("song-studio: Voice engine tabs leaked into mobile Audio Downloader")
        page.set_viewport_size({"width": 1440, "height": 900})
        if page.locator('.vps1-native-song-tabs a').all_text_contents() != [
                "Audio Downloader", "Song Maker", "Song Uploader"]:
            errors.append("song-studio: independent app tabs missing")
        experiment.click()
        page.wait_for_url(origin + "/experiment-studio/")
        page.locator('nav[aria-label="Experiment Studio"] a[href^="/voice-studio/app"]').click()
        page.wait_for_url(origin + "/voice-studio/app#vps1-production")
        voice = page.locator('nav[aria-label="Experiment Studio"] a[href^="/voice-studio/app"]')
        page.wait_for_function("!document.body.classList.contains('vs-audio-active')")
        if not page.locator('.vs-engine-selector').is_visible():
            errors.append("voice-studio: Voice engine tabs missing after switching back")
        if voice.get_attribute("aria-current") != "page":
            errors.append("song-studio: Voice Studio selection missing")
        song.click()
        page.wait_for_url(origin + "/audio-downloader/")
        page.locator('#vsAudioWorkspace').wait_for(state="visible")
        if page.locator('.vs-engine-selector').is_visible():
            errors.append("song-studio: Voice engine tabs visible after switching back")
        if song.get_attribute("aria-current") != "page":
            errors.append("song-studio: Song Studio selection missing after switch")
        if page.url != origin + "/audio-downloader/":
            errors.append("song-studio: navigation did not preserve the clean URL")
    except Exception as exc:
        errors.append(f"song-studio: {type(exc).__name__}: {exc}")
    finally:
        context.close()
    return errors


def run(screenshots: Path | None) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("UI_BROWSER_GATE=FAIL playwright not installed", file=sys.stderr)
        return 2

    shell_css = CSS.read_text(encoding="utf-8")
    geometry_css = VIDEO_CSS.read_text(encoding="utf-8")
    native_js = JS.read_text(encoding="utf-8")
    errors: list[str] = []
    if screenshots is not None:
        screenshots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        kwargs = {"headless": True}
        if os.environ.get("VPS1_CHROMIUM_EXECUTABLE"):
            kwargs["executable_path"] = os.environ["VPS1_CHROMIUM_EXECUTABLE"]
        browser = pw.chromium.launch(**kwargs)
        try:
            for workspace, selected in (("song", "Song Maker"), ("song-uploader", "Song Uploader")):
                context = browser.new_context()
                try:
                    page = context.new_page()
                    page.set_content(page_html("voice-studio"))
                    page.evaluate("""([workspace, source]) => {
                        const s = document.createElement('script');
                        s.dataset.workspace = workspace; s.textContent = source;
                        document.body.append(s);
                    }""", [workspace, native_js])
                    rails = page.locator('nav[aria-label="Song Studio"]')
                    if rails.count() != 1 or rails.locator('a[aria-current="page"]').all_text_contents() != [selected]:
                        errors.append(f"{workspace}: Song rail/selection mismatch")
                    if page.locator('nav[aria-label="Video Studio"]').count():
                        errors.append(f"{workspace}: unexpected Video Studio rail")
                finally:
                    context.close()
            errors.extend(verify_song_navigation(browser, native_js, shell_css))
            errors.extend(verify_actual_short_maker_identity(
                browser, native_js, shell_css, geometry_css))
            for width, height in VIEWPORTS:
                ctx = browser.new_context(viewport={"width": width, "height": height},
                                          reduced_motion="reduce",
                                          device_scale_factor=1)
                rails = {}
                screenshots_in_memory = {}
                try:
                    for app in list(VIDEOS) + ["auto-uploader", "auto-stream", "voice-studio"]:
                        page = ctx.new_page()
                        try:
                            page.set_content(page_html(app))
                            page.add_style_tag(content="body{margin:0}")
                            page.add_style_tag(content=shell_css)
                            page.add_style_tag(content=geometry_css)
                            if app in ("scene-builder", "video-merger"):
                                # Run the actual unifier, not only a mock legacy sidebar.
                                page.evaluate(
                                    """(source) => {
                                      const s=document.createElement('script');
                                      s.dataset.unifySidebar='1';
                                      s.textContent=source;
                                      document.body.append(s);
                                    }""",
                                    native_js)
                                names = page.locator("#app-sidebar > nav > a").all_text_contents()
                                if names != ["Dashboard", "Song Studio", "Short Studio", "Experiment Studio"]:
                                    errors.append(f"{width}px {app}: sidebar drift {names!r}")
                            if app in NATIVE:
                                workspace = {"auto-caption-studio": "acs",
                                             "auto-uploader": "uploader",
                                             "auto-stream": "stream",
                                             "voice-studio": "voice"}[app]
                                # Inject unmodified production shared JS as a real
                                # script element so document.currentScript is correct.
                                page.evaluate(
                                    """([workspace, source]) => {
                                      const s=document.createElement('script');
                                      s.dataset.workspace=workspace;
                                      s.textContent=source;
                                      document.body.append(s);
                                    }""",
                                    [workspace, native_js])
                            if app in VIDEOS:
                                selector = (".vps1-native-video-tabs"
                                            if app == "auto-caption-studio"
                                            else '[data-video-studio-subnav="1"]')
                            elif app in ("auto-uploader", "auto-stream"):
                                selector = ".vps1-native-upload-tabs"
                            else:
                                selector = None
                            sidebar = page.locator("#app-sidebar, #vps1-native-site-sidebar").first
                            if sidebar.count() != 1:
                                errors.append(f"{width}px {app}: shared sidebar missing")
                            elif width > 760:
                                sw = sidebar.evaluate("(x) => x.getBoundingClientRect().width")
                                if abs(sw - 214) > TOLERANCE_PX:
                                    errors.append(f"{width}px {app}: sidebar width={sw}")
                            if selector is None:
                                continue
                            nav = page.locator(selector).first
                            nav.wait_for(state="visible", timeout=5000)
                            normalize_tabs(page, selector)
                            rect = nav.bounding_box()
                            if not rect:
                                errors.append(f"{width}px {app}: top tabs not rendered")
                                continue
                            if rect["height"] < 62 - TOLERANCE_PX:
                                errors.append(f"{width}px {app}: rail height={rect['height']}")
                            # Every third-level rail must follow, not overlap, Experiment Studio.
                            outer = page.locator('nav[aria-label="Experiment Studio"]')
                            if outer.count() != 1:
                                errors.append(f"{width}px {app}: Experiment Studio level missing")
                            else:
                                outer_rect = outer.bounding_box()
                                if (not outer_rect or
                                        rect["y"] < outer_rect["y"] + outer_rect["height"]):
                                    errors.append(f"{width}px {app}: overlapping Studio levels")
                            if app in VIDEOS:
                                destinations = nav.locator("a").evaluate_all(
                                    "links => links.map(link => link.getAttribute('href'))")
                                expected = ["/scene-builder/", "/auto-caption-studio/",
                                            "/video-merger/"]
                                if destinations != expected:
                                    errors.append(f"{width}px {app}: Video destinations {destinations!r}")
                            else:
                                if nav.locator("a").count() != 2:
                                    errors.append(f"{width}px {app}: not two Upload tabs")
                            overflow = page.evaluate(
                                "() => document.documentElement.scrollWidth > "
                                "document.documentElement.clientWidth + 2")
                            if overflow:
                                errors.append(f"{width}px {app}: horizontal document overflow")
                            rails[app] = rect
                            shot = nav.screenshot(animations="disabled")
                            screenshots_in_memory[app] = shot
                            if screenshots is not None:
                                (screenshots / f"{width}-{app}.png").write_bytes(shot)
                            if app == "auto-stream":
                                # The stream-owned header MUST follow top-level tabs.
                                placement = page.evaluate(
                                    """() => {
                                       const t=document.querySelector('.vps1-native-upload-tabs');
                                       const h=document.querySelector('[data-app-header]');
                                       return !!t && !!h &&
                                              !!(t.compareDocumentPosition(h) &
                                                 Node.DOCUMENT_POSITION_FOLLOWING);
                                    }""")
                                if not placement:
                                    errors.append(f"{width}px stream: app header pushes tabs down")
                        except Exception as exc:
                            errors.append(f"{width}px {app}: {type(exc).__name__}: {exc}")
                        finally:
                            page.close()
                    if len(rails) != 5:
                        errors.append(f"{width}px missing one of five Studio group rails")
                    else:
                        base = rails["scene-builder"]
                        for app, rect in rails.items():
                            if abs(rect["x"] - base["x"]) > TOLERANCE_PX:
                                errors.append(
                                    f"{width}px {app}: x={rect['x']:.1f} "
                                    f"vs Scene={base['x']:.1f} (limit {TOLERANCE_PX})")
                        # Vertical alignment within each third-level Studio, not
                        # between unrelated legacy and independent wrappers.
                        for group in (("scene-builder", "video-merger", "auto-caption-studio"),
                                      ("auto-uploader", "auto-stream")):
                            reference = rails[group[0]]
                            for name in group[1:]:
                                if abs(rails[name]["y"] - reference["y"]) > TOLERANCE_PX:
                                    errors.append(
                                        f"{width}px {name}: Studio rail y drift "
                                        f"{rails[name]['y']:.1f} vs {reference['y']:.1f}")
                        for name in ("video-merger", "auto-caption-studio"):
                            ratio = changed_pixel_ratio(
                                screenshots_in_memory["scene-builder"],
                                screenshots_in_memory[name])
                            if ratio > PIXEL_THRESHOLD:
                                errors.append(
                                    f"{width}px {name}: Studio tab screenshot delta "
                                    f"{ratio:.1%} (limit {PIXEL_THRESHOLD:.1%})")
                        ratio = changed_pixel_ratio(
                            screenshots_in_memory["auto-uploader"],
                            screenshots_in_memory["auto-stream"])
                        if ratio > PIXEL_THRESHOLD:
                            errors.append(f"{width}px stream/uploader screenshot delta {ratio:.1%}")
                finally:
                    ctx.close()
        finally:
            browser.close()

    for error in errors:
        print("UI_BROWSER_REGRESSION:", error, file=sys.stderr)
    if errors:
        print(f"UI_BROWSER_GATE=FAIL issues={len(errors)} viewports={len(VIEWPORTS)}")
        return 1
    print("UI_BROWSER_GATE=PASS viewports=5 studio_rails=6 "
          "source=REAL render=CHROMIUM auth=FIXTURE_ONLY")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path, default=None)
    args = parser.parse_args()
    return run(args.screenshots)


if __name__ == "__main__":
    sys.exit(main())

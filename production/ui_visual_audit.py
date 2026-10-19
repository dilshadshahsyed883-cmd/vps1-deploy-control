#!/usr/bin/env python3
"""Opt-in AUTHENTICATED browser geometry audit for Video Studio.

Run only on a protected machine with a locally held Playwright storage-state
file. This is a genuine rendered-browser test, not an offline mock. It never
logs cookies or browser storage and never uploads screenshots.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APPS = {
    "scene-builder": ("/scene-builder/", '[data-video-studio-subnav="1"]'),
    "auto-caption-studio": ("/auto-caption-studio/", ".vps1-native-video-tabs"),
    "short-maker": ("/short-maker/", '[data-video-studio-subnav="1"]'),
    "video-merger": ("/video-merger/", '[data-video-studio-subnav="1"]'),
}
VIEWPORTS = ((1440, 900), (1024, 768), (768, 1024), (390, 844), (360, 780))
TOLERANCE_PX = 2.0


def validate_rectangles(geometry: dict[str, dict], viewport: tuple[int, int]) -> list[str]:
    """Browser-derived bounding boxes must share x/y tab origin."""
    errors = []
    if set(geometry) != set(APPS):
        errors.append(f"{viewport[0]}px: incomplete Video Studio coverage")
        return errors
    scene = geometry["scene-builder"]
    for app, box in geometry.items():
        for key in ("x", "y"):
            if abs(box[key] - scene[key]) > TOLERANCE_PX:
                errors.append(
                    f"{viewport[0]}px {app}: tab {key}={box[key]:.1f}px "
                    f"vs Scene={scene[key]:.1f}px exceeds {TOLERANCE_PX}px"
                )
        if box["height"] < 40:
            errors.append(f"{viewport[0]}px {app}: touch target/tab rail undersized")
    return errors


def audit(base_url: str, storage_state: Path, screenshot_dir: Path | None) -> int:
    if not storage_state.is_file():
        print("UI_VISUAL_AUDIT=FAIL authenticated storage-state file missing", file=sys.stderr)
        return 2
    if not base_url.startswith("https://"):
        print("UI_VISUAL_AUDIT=FAIL HTTPS base URL required", file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        print("UI_VISUAL_AUDIT=NOT_RUN install Python playwright + Chromium first",
              file=sys.stderr)
        return 2

    if screenshot_dir:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    all_geometry: dict[str, dict] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=str(storage_state),
                                          device_scale_factor=1,
                                          reduced_motion="reduce")
            try:
                for width, height in VIEWPORTS:
                    viewport_key = str(width)
                    boxes: dict[str, dict] = {}
                    for name, (route, selector) in APPS.items():
                        page = context.new_page()
                        try:
                            page.set_viewport_size({"width": width, "height": height})
                            response = page.goto(base_url.rstrip("/") + route,
                                                 wait_until="domcontentloaded",
                                                 timeout=30000)
                            if response is None or response.status >= 400:
                                raise RuntimeError(f"route HTTP {response.status if response else 'none'}")
                            if "/login" in page.url:
                                raise RuntimeError("authenticated session was rejected")
                            element = page.locator(selector).first
                            element.wait_for(state="visible", timeout=15000)
                            page.evaluate("document.fonts.ready")
                            box = element.bounding_box()
                            if box is None:
                                raise RuntimeError("tab rail has no bounding box")
                            metrics = page.evaluate("""() => ({
                                overflow: document.documentElement.scrollWidth >
                                    document.documentElement.clientWidth + 2,
                                sidebarWidth: (() => {
                                    const s = document.querySelector(
                                        '#app-sidebar, #vps1-native-site-sidebar');
                                    return s ? s.getBoundingClientRect().width : 0;
                                })()
                            })""")
                            if metrics["overflow"]:
                                errors.append(f"{width}px {name}: horizontal document overflow")
                            if width > 760 and abs(metrics["sidebarWidth"] - 214) > TOLERANCE_PX:
                                errors.append(f"{width}px {name}: desktop sidebar not 214px")
                            boxes[name] = box
                            if screenshot_dir is not None:
                                page.screenshot(
                                    path=str(screenshot_dir / f"{width}-{name}.png"),
                                    full_page=True, animations="disabled"
                                )
                        except (PlaywrightTimeout, RuntimeError, ValueError) as exc:
                            errors.append(f"{width}px {name}: {exc}")
                        finally:
                            page.close()
                    all_geometry[viewport_key] = boxes
                    errors.extend(validate_rectangles(boxes, (width, height)))
            finally:
                context.close()
        finally:
            browser.close()
    print(json.dumps({"result": "FAIL" if errors else "PASS",
                      "viewport_count": len(VIEWPORTS),
                      "apps": list(APPS), "geometry": all_geometry,
                      "errors": errors}, indent=2))
    print(f"UI_VISUAL_AUDIT={'FAIL' if errors else 'PASS'}", file=sys.stderr)
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://app.scenebuilder.online")
    parser.add_argument("--storage-state", type=Path, required=True,
                        help="Local Playwright auth state; NEVER commit this file")
    parser.add_argument("--screenshot-dir", type=Path,
                        help="Optional local PRIVATE screenshot folder; do not publish")
    args = parser.parse_args()
    return audit(args.base_url, args.storage_state, args.screenshot_dir)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Authenticated Chromium evidence for the shared VPS1 shell.

This is intentionally opt-in: pass a local Playwright storage state from a
protected runner. It records real route geometry, document reload count,
network request count, navigation timing and cumulative layout shift without
printing credentials, cookies or page contents. A prior JSON result may be
provided as --baseline to produce measured before/after deltas.
"""
from __future__ import annotations

import argparse, json, sys
from pathlib import Path

ROUTES = {
    "main": ("/", None),
    "video-studio": ("/scene-builder/", '[data-video-studio-subnav="1"]'),
    "upload-studio": ("/local-upload-manager/app?tab=queue", ".vps1-native-upload-tabs"),
    "voice-studio": ("/voice-studio/app#vps1-production", None),
    "auto-caption-studio": ("/auto-caption-studio/", ".vps1-native-video-tabs"),
    "storage-manager": ("/admin/storage", None),
}
VIEWPORTS = ((1440, 900), (1024, 768), (768, 1024), (390, 844), (360, 780))
SIDEBAR = "#app-sidebar, #vps1-native-site-sidebar"

def audit(base_url: str, state: Path, shots: Path | None) -> dict:
    if not state.is_file():
        raise RuntimeError("authenticated Playwright storage state is required")
    from playwright.sync_api import sync_playwright
    result = {"base_url": base_url, "viewports": {}, "errors": []}
    if shots: shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for width, height in VIEWPORTS:
                context = browser.new_context(storage_state=str(state), viewport={"width": width, "height": height}, reduced_motion="reduce")
                context.add_init_script("""() => { window.__vpsAudit={cls:0}; new PerformanceObserver(l=>{for(const e of l.getEntries()) if(!e.hadRecentInput) window.__vpsAudit.cls+=e.value}).observe({type:'layout-shift',buffered:true}); }""")
                viewport = {}
                try:
                    for name, (route, tab) in ROUTES.items():
                        page = context.new_page(); requests = [0]
                        page.on("request", lambda _: requests.__setitem__(0, requests[0] + 1))
                        try:
                            response = page.goto(base_url.rstrip("/") + route, wait_until="networkidle", timeout=45000)
                            if not response or response.status >= 400 or "/login" in page.url: raise RuntimeError("authenticated route unavailable")
                            page.evaluate("document.fonts.ready")
                            sidebar = page.locator(SIDEBAR).first
                            sidebar.wait_for(state="visible", timeout=15000)
                            sb = sidebar.bounding_box()
                            main = page.locator("main, [role=main], .workspace, .app-shell").first.bounding_box()
                            rail = None
                            if tab:
                                node = page.locator(tab).first; node.wait_for(state="visible", timeout=15000); rail = node.bounding_box()
                            nav = page.evaluate("performance.getEntriesByType('navigation')[0]?.toJSON() || {}")
                            row = {"http": response.status, "sidebar": sb, "main": main, "tab_rail": rail, "requests": requests[0], "cls": page.evaluate("window.__vpsAudit.cls"), "load_ms": nav.get("loadEventEnd", 0), "dom_ms": nav.get("domContentLoadedEventEnd", 0), "overflow": page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 2")}
                            if width > 760 and (not sb or abs(sb["width"] - 214) > 2): raise RuntimeError("desktop sidebar geometry mismatch")
                            if row["overflow"]: raise RuntimeError("horizontal overflow")
                            viewport[name] = row
                            if shots: page.screenshot(path=str(shots / f"{width}-{name}.png"), full_page=True, animations="disabled")
                        except Exception as exc:
                            result["errors"].append(f"{width}px {name}: {exc}")
                        finally: page.close()
                finally: context.close()
                result["viewports"][str(width)] = viewport
        finally: browser.close()
    result["result"] = "PASS" if not result["errors"] else "FAIL"
    return result

def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument("--storage-state", type=Path, required=True); p.add_argument("--base-url", default="https://app.scenebuilder.online"); p.add_argument("--output", type=Path, required=True); p.add_argument("--screenshots", type=Path); args=p.parse_args()
    try: report = audit(args.base_url, args.storage_state, args.screenshots)
    except Exception as exc: print(f"UI_PRODUCTION_AUDIT=FAIL {exc}", file=sys.stderr); return 2
    args.output.write_text(json.dumps(report, indent=2))
    print(f"UI_PRODUCTION_AUDIT={report['result']} viewports=5 routes=6")
    return 0 if report["result"] == "PASS" else 1
if __name__ == "__main__": raise SystemExit(main())

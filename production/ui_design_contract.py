"""Fail-closed source-level governance for the owner-approved VPS1 UI policy.

This is NOT a pixel-perfect browser audit. Use ui_visual_audit.py after
authenticated staging/live installation for actual geometry evidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
GEOMETRY_ASSET = "vps1_video_studio_geometry.css?v=20261020-geometry-v1"

SOURCE_FILES = {
    "shell_css": SCENE / "static/vps1_native_site_shell.css",
    "geometry_css": SCENE / "static/vps1_video_studio_geometry.css",
    "shell_js": SCENE / "static/vps1_native_site_shell.js",
    "scene": SCENE / "templates/base.html",
    "acs": ROOT / "apps/auto-caption-studio/part01/web_backend/acs_studio.html",
    "short_maker": ROOT / "apps/short-maker/part01/app.py",
    "video_merger": ROOT / "apps/video-merger/part01/templates/index.html",
    "voice": ROOT / "apps/voice-studio/part01/templates/voice_studio.html",
    "uploader": ROOT / "apps/local-upload-manager/part01/templates/base.html",
    "policy": ROOT / "docs/UI_UX_MASTER_RULES_V1.md",
}

VIDEO_ROUTES = (
    ("/scene-builder/", "Scene Builder"),
    ("/auto-caption-studio/", "Auto Caption Studio"),
    ("/video-merger/", "Video Merger"),
)


def read_sources(root: Path = ROOT) -> dict[str, str]:
    """Resolve paths within an optionally supplied repository snapshot."""
    return {
        name: (root / path.relative_to(ROOT)).read_text(encoding="utf-8")
        for name, path in SOURCE_FILES.items()
    }


def violations(source: dict[str, str]) -> list[str]:
    issues: list[str] = []
    def require(file: str, marker: str, reason: str) -> None:
        if marker not in source[file]:
            issues.append(f"{file}: {reason} (missing {marker!r})")

    require("shell_css", "--vps1-sidebar-width:214px",
            "canonical 214px global sidebar ownership lost")
    require("shell_css", "#app-sidebar.sidebar,#vps1-native-site-sidebar",
            "Scene/native sidebar geometry is no longer shared")
    require("shell_css", "html:has(script[data-workspace=\"uploader\"]) body",
            "native app left gutter must be reserved before JS navigation")
    require("shell_css", "@media(max-width:760px)",
            "canonical mobile sidebar breakpoint missing")
    require("shell_css", ".vps1-native-upload-tabs",
            "Upload Studio shared tabs must use site shell")
    require("geometry_css", "--vps1-studio-max: 1400px",
            "Video Studio global content max width lost")
    require("geometry_css", "--vps1-studio-top: 26px",
            "Video Studio desktop top baseline changed without coordinated migration")
    require("geometry_css", "--vps1-studio-gutter: 34px",
            "Video Studio desktop left gutter changed without coordinated migration")
    require("geometry_css", "min-height: 62px !important",
            "four-app tab height contract lost")
    require("geometry_css", "flex-wrap: nowrap !important",
            "studio tabs must not wrap and shift vertically")
    require("geometry_css", "overflow-x: auto !important",
            "studio tabs need mobile-safe overflow")
    require("geometry_css", "--vps1-studio-top: 72px",
            "mobile top baseline missing")
    require("shell_js", 'if (window.self !== window.top) return;',
            "do not inject duplicate nav into a nested document")
    require("shell_js", '["Upload Studio", "/local-upload-manager/app?embedded=1&tab=dashboard", "upload"]',
            "one combined Upload Studio sidebar entry missing")
    require("shell_js", 'addLink(tabs, "Auto Uploader"',
            "Auto Uploader primary tab missing")
    require("shell_js", 'addLink(tabs, "Auto Stream"',
            "Auto Stream independent primary tab missing")
    require("shell_js", '["Video Studio", "/scene-builder/", "video"]',
            "Video Studio sidebar grouping missing")
    require("shell_js", 'refreshIdentity()',
            "native sidebar identity must come from live backend")
    for route, label in VIDEO_ROUTES:
        require("shell_js", f'addLink(tabs, "{label}", "{route}"',
                f"canonical {label} shared tab missing")
    for page in ("scene", "acs", "short_maker", "video_merger"):
        if source[page].count(GEOMETRY_ASSET) != 1:
            issues.append(f"{page}: must load canonical Video Studio geometry asset once")
    for page in ("scene", "acs", "short_maker", "video_merger", "voice", "uploader"):
        require(page, "vps1_native_site_shell.css",
                "shared site shell CSS not linked")
    for page in ("scene", "short_maker", "video_merger"):
        require(page, 'data-unify-sidebar="1"',
                "classic app must activate the canonical navigation controller")
        # Scene's current V4 shell is released independently; legacy Video
        # Studio applications keep their last immutable asset URL until their
        # own guarded release. All versions resolve the same canonical files.
        version = ("20261020-sidebar-stable-v4" if page == "scene"
                   else "20261020-experiment-v1")
        require(page, "vps1_native_site_shell.js?v=" + version,
                "classic page lacks its versioned shared navigation script")
        require(page, "vps1_native_site_shell.css?v=" + version,
                "classic page loads stale shared CSS version")
    require("policy", "One Website Shell + Independent Application UIs",
            "owner-approved architecture principle missing")
    for marker in ("UI-01 to UI-10", "Phase C:", "No site CSS, live app code"):
        require("policy", marker, "governance contract incomplete")
    return issues


def main() -> int:
    try:
        problems = violations(read_sources())
    except (OSError, UnicodeError) as exc:
        print(f"UI_GOVERNANCE=FAIL source unavailable: {exc}", file=sys.stderr)
        return 1
    for problem in problems:
        print(f"UI_GOVERNANCE_ERROR: {problem}", file=sys.stderr)
    if problems:
        print(f"UI_GOVERNANCE=FAIL count={len(problems)}", file=sys.stderr)
        return 1
    print(f"UI_GOVERNANCE=PASS sources={len(SOURCE_FILES)} rendered_browser=NOT_VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Static shared website shell for isolated VPS1 fresh rootfs builds.

The shell owns only chrome/navigation. Individual applications own their UI,
backend, persistent data and release lifecycle. No production writes.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from tools.render_nginx_routes import load_registry, validate_registry

LIVE_ROUTES = Path(__file__).resolve().parents[1] / 'infrastructure/registry/apps.json'

ASSETS = Path(__file__).resolve().parent / "shell"
SLUG = re.compile(r"[a-z][a-z0-9-]{1,63}\Z")
TITLES = {
    "scene-builder": "Scene Builder",
    "script-work": "Script Work",
    "voice-studio": "Voice Studio",
    "auto-caption-studio": "Auto Caption Studio",
    "local-upload-manager": "Local Upload Manager",
    "short-stream": "Short Stream",
    "short-maker": "Short Maker",
    "video-merger": "Video Merger",
    "self-media-library": "Self Media Library",
    "vps-storage-manager": "VPS Storage Manager",
}


def catalog(group_config: dict, install_profiles: dict, route_registry: dict | None = None) -> dict:
    """Build allowlisted, same-origin navigation from installed app registrations."""
    groups = group_config["groups"]
    profiles = install_profiles["groups"]
    ids = [group["id"] for group in groups]
    if len(ids) != len(set(ids)) or set(ids) != set(profiles):
        raise ValueError("shell registration mismatch")
    # A configured installation profile is NOT a verified public route.
    # The live GitOps registry intentionally stays empty before Agent inventory.
    if route_registry is None:
        route_registry = load_registry(LIVE_ROUTES)
    entries = validate_registry(route_registry)
    verified_ids = {entry["id"] for entry in entries}
    if not verified_ids.issubset(set(ids)):
        raise ValueError("route points to unknown app")
    apps = []
    for group in groups:
        app = group["id"]
        if not isinstance(app, str) or not SLUG.fullmatch(app):
            raise ValueError("unsafe application ID")
        if group.get("deployment_mode", "active") == "offline":
            if app in verified_ids:
                raise ValueError("offline app must not have live route")
            continue
        profile = profiles[app]
        if profile.get("deployment_mode") == "offline" or profile.get("parallel_owner"):
            raise ValueError("shell must not expose an excluded app")
        services = profile.get("services", [])
        if not isinstance(services, list):
            raise ValueError("invalid service list")
        # Presence of a configured reverse proxy route is not proof of live health.
        apps.append({
            "id": app,
            "title": TITLES.get(app, app.replace("-", " ").title()),
            "path": "/" + app + "/",
            "route_configured": bool(services) and app in verified_ids,
            "runtime_verified": False,
        })
    return {"schema_version": 1, "apps": apps}


def build(root: Path, group_config: dict, install_profiles: dict) -> dict:
    """Copy browser assets and generate non-executable per-app route inventory."""
    root = Path(root)
    # All production paths are forbidden even if this helper is called directly.
    target = (root / "srv/fresh-website/shell").resolve()
    if not root.is_absolute() or root.is_symlink() or not root.resolve().is_relative_to(Path("/tmp")) or root.resolve() == Path("/tmp"):
        raise ValueError("shell output requires isolated /tmp rootfs")
    if not target.is_relative_to(root.resolve()):
        raise ValueError("unsafe shell destination")
    manifest = catalog(group_config, install_profiles)
    target.mkdir(parents=True, exist_ok=True)
    (target / "assets").mkdir(exist_ok=True)
    for relative in ("index.html", "assets/shell.css", "assets/shell.js"):
        source = ASSETS / relative
        if not source.is_file() or source.is_symlink():
            raise ValueError("missing or unsafe shell asset")
        destination = target / relative
        shutil.copyfile(source, destination)
    (target / "assets/apps.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "navigation_apps": len(manifest["apps"]),
        "routed_apps": sum(app["route_configured"] for app in manifest["apps"]),
        "services_activated": 0,
        "production_modified": False,
    }

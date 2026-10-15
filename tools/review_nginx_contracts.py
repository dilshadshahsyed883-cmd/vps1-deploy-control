#!/usr/bin/env python3
"""Read-only Nginx GitOps contract review; NEVER discovers or changes VPS routing.

All non-empty production entries must match externally verified Agent inventory.
CI fixture mode tests the parser only and cannot authorize a live apply.
"""
import argparse
import difflib
import json
from pathlib import Path
import re
import sys

from tools.render_nginx_routes import load_registry, render, validate_registry

NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
ENDPOINT = re.compile(r"/[a-zA-Z0-9_./-]*\Z")
FIELDS = {"id", "path", "upstream_port", "prefix_mode", "websocket", "service_unit"}
INVENTORY_FIELDS = FIELDS | {"auth_policy", "static_paths", "websocket_path", "prefix_browser_verified"}
AUTH = {"inherited-site-auth", "app-auth"}


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("unable to read evidence: " + str(exc)) from exc


def validate_inventory(raw, fixture=False):
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "origin", "apps", "evidence_report"}:
        raise ValueError("inventory schema invalid")
    if raw["schema_version"] != 1 or type(raw["schema_version"]) is not int:
        raise ValueError("inventory version invalid")
    expected = "synthetic-ci-only" if fixture else "verified-vps1-agent-report"
    if raw["origin"] != expected:
        raise ValueError("inventory origin mismatch")
    if not fixture and (not isinstance(raw["evidence_report"], str)
                        or not raw["evidence_report"].startswith("Automation_Master_System/VPS_1/02_REPORTS/")):
        raise ValueError("missing canonical agent report reference")
    if not isinstance(raw["apps"], list):
        raise ValueError("inventory apps invalid")
    indexed = {}
    for item in raw["apps"]:
        if not isinstance(item, dict) or set(item) != INVENTORY_FIELDS:
            raise ValueError("inventory entry fields invalid")
        ident = item["id"]
        if not isinstance(ident, str) or not NAME.fullmatch(ident) or ident in indexed:
            raise ValueError("duplicate or invalid inventory id")
        # Reuse the strict renderer allowlist, no freeform directives or hostnames.
        validate_registry({"schema_version": 1, "domain": "app.scenebuilder.online",
                           "apps": [{key: item[key] for key in FIELDS}]})
        if item["auth_policy"] not in AUTH:
            raise ValueError("authentication policy must be explicit")
        paths = item["static_paths"]
        if not isinstance(paths, list) or len(paths) > 12 or len(set(paths)) != len(paths):
            raise ValueError("invalid static paths")
        for path in paths:
            if not isinstance(path, str) or not ENDPOINT.fullmatch(path) or ".." in path.split("/"):
                raise ValueError("unsafe static asset URL")
        ws_path = item["websocket_path"]
        if ws_path is not None and (not isinstance(ws_path, str) or
                                    not ENDPOINT.fullmatch(ws_path) or ".." in ws_path.split("/")):
            raise ValueError("unsafe websocket path")
        if bool(ws_path) != item["websocket"]:
            raise ValueError("websocket flag and endpoint disagree")
        if type(item["prefix_browser_verified"]) is not bool:
            raise ValueError("invalid prefix compatibility evidence")
        if item["prefix_mode"] == "strip" and not item["prefix_browser_verified"]:
            raise ValueError("strip prefix needs browser verification")
        for static in paths:
            if not static.startswith(item["path"]):
                raise ValueError("absolute static asset path outside owned route")
        if ws_path and not ws_path.startswith(item["path"]):
            raise ValueError("websocket endpoint outside owned route")
        indexed[ident] = item
    return indexed


def review(registry, inventory, previous="", fixture=False):
    apps = validate_registry(registry)
    if not apps and not fixture:
        if inventory is not None and inventory.get("apps"):
            raise ValueError("nonempty inventory cannot populate empty live registry")
        return {"status": "NO_LIVE_ROUTES", "routes": 0, "diff": "",
                "needs_vps_activation": False, "production_changed": False}
    if not apps:
        raise ValueError("fixture review requires a route")
    if inventory is None:
        raise ValueError("nonempty registry requires verified live inventory")
    found = validate_inventory(inventory, fixture=fixture)
    ids = {entry["id"] for entry in apps}
    if set(found) != ids:
        raise ValueError("registry/inventory application mismatch")
    for app in apps:
        evidence = found[app["id"]]
        for key in FIELDS:
            if app[key] != evidence[key]:
                raise ValueError("routing diverges from agent inventory: " + app["id"] + " / " + key)
    candidate = render(registry, require_nonempty=True)
    delta = "".join(difflib.unified_diff(
        previous.splitlines(keepends=True), candidate.splitlines(keepends=True),
        fromfile="previous-reviewed-config", tofile="proposed-gitops-config"))
    return {"status": "REVIEW_REQUIRED", "routes": len(apps),
            "diff": delta, "needs_vps_activation": True, "production_changed": False}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--registry", default="infrastructure/registry/apps.json")
    ap.add_argument("--inventory", help="sanitized verified VPS Agent inventory JSON")
    ap.add_argument("--previous", help="sanitized existing managed snippet (diff only)")
    ap.add_argument("--fixture", action="store_true", help="CI only, never production approval")
    args = ap.parse_args()
    try:
        result = review(load_registry(args.registry),
                        _json(args.inventory) if args.inventory else None,
                        Path(args.previous).read_text() if args.previous else "",
                        fixture=args.fixture)
        print(json.dumps(result, indent=2))
    except (ValueError, OSError) as exc:
        print("NGINX_CONTRACT_REVIEW=BLOCKED: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

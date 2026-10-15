#!/usr/bin/env python3
"""Fail-closed Nginx location renderer. Emits server-context snippets, never edits VPS."""
import argparse
import json
import re
import sys
from pathlib import Path

DOMAIN = "app.scenebuilder.online"
ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z", re.ASCII)
UNIT_RE = re.compile(r"vps1-[a-z0-9]+(?:-[a-z0-9]+)*\.service\Z", re.ASCII)
APP_FIELDS = {"id", "path", "upstream_port", "prefix_mode", "websocket", "service_unit"}
MARKER = "# managed-by: vps1-deploy-control; renderer: v1"


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_registry(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse registry: {exc}") from exc


def validate_registry(raw, require_nonempty=False):
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "domain", "apps"}:
        raise ValueError("registry must contain only schema_version, domain, apps")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("unsupported schema_version")
    if raw["domain"] != DOMAIN:
        raise ValueError("domain does not match protected site")
    apps = raw["apps"]
    if not isinstance(apps, list) or len(apps) > 32:
        raise ValueError("apps must be a list of at most 32 entries")
    if require_nonempty and not apps:
        raise ValueError("refusing empty managed route set")
    seen_paths, seen_ids = set(), set()
    for app in apps:
        if not isinstance(app, dict) or set(app) != APP_FIELDS:
            raise ValueError("app registry fields do not match allowlist")
        app_id = app["id"]
        if not isinstance(app_id, str) or not ID_RE.fullmatch(app_id):
            raise ValueError("invalid application id")
        path = app["path"]
        if not isinstance(path, str) or path != f"/{app_id}/":
            raise ValueError(f"unsafe or noncanonical location for {app_id}")
        if path in seen_paths or app_id in seen_ids:
            raise ValueError("duplicate app id or location")
        seen_paths.add(path)
        seen_ids.add(app_id)
        port = app["upstream_port"]
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError(f"invalid upstream port for {app_id}")
        if app["prefix_mode"] not in ("preserve", "strip"):
            raise ValueError(f"unknown prefix mode for {app_id}")
        if type(app["websocket"]) is not bool:
            raise ValueError(f"websocket must be boolean for {app_id}")
        unit = app["service_unit"]
        if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit):
            raise ValueError(f"invalid service unit for {app_id}")
    return sorted(apps, key=lambda item: item["path"])


def render(raw, require_nonempty=False):
    apps = validate_registry(raw, require_nonempty=require_nonempty)
    lines = [
        MARKER,
        "# Nginx server-context include ONLY for app.scenebuilder.online.",
        "# Managed route entries are inactive until explicitly wired by the locked VPS agent.",
    ]
    for app in apps:
        app_id, path, port = app["id"], app["path"], app["upstream_port"]
        suffix = "/" if app["prefix_mode"] == "strip" else ""
        lines.extend([
            "",
            f"# app: {app_id}; service inventory: {app['service_unit']}",
            f"location ^~ {path} " + "{",
            f"    proxy_pass http://127.0.0.1:{port}{suffix};",
            "    proxy_http_version 1.1;",
            "    proxy_set_header Host $host;",
            "    proxy_set_header X-Real-IP $remote_addr;",
            "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "    proxy_set_header X-Forwarded-Proto $scheme;",
        ])
        if app["websocket"]:
            lines.extend([
                "    proxy_set_header Upgrade $http_upgrade;",
                '    proxy_set_header Connection "upgrade";',
            ])
        lines.append("}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="infrastructure/registry/apps.json")
    parser.add_argument("--output", default="build/nginx/vps1-managed-routes.conf")
    parser.add_argument("--require-nonempty", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        rendered = render(load_registry(args.registry), args.require_nonempty)
        if not args.validate_only:
            dest = Path(args.output)
            if dest.is_symlink():
                raise ValueError("refusing symlink output")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(rendered, encoding="utf-8")
        print("NGINX_ROUTE_REGISTRY_VALID=PASS")
    except ValueError as exc:
        print(f"NGINX_ROUTE_REGISTRY_VALID=FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

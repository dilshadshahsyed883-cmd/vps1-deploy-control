#!/usr/bin/env python3
"""Read-only VPS1 API/port/Nginx/HTTPS inventory; never changes production.

Run from a protected-main checkout with --host on VPS1. Emits no credentials,
cookies, HTML bodies, Nginx configuration or user application data.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import shutil
import socket
import ssl
import subprocess
from pathlib import Path
from urllib import error, request

from production.runtime import CATALOG

DOMAIN = "app.scenebuilder.online"
WEB_PORT = 18080
STORAGE_PORT = 8149
GATEWAY_PORT = 8888
HELPER = Path("/usr/local/sbin/vps1-production-deploy")
SITE = Path("/etc/nginx/sites-available/vps1-scene-public.conf")
CERT = Path("/etc/letsencrypt/live") / DOMAIN / "fullchain.pem"
REPO = Path(__file__).resolve().parents[1]


def catalog():
    """Read all independent application API and port contracts from runtime.py."""
    result = []
    seen = set()
    for app, spec in CATALOG.items():
        port = spec["port"]
        if port in seen:
            raise ValueError(f"duplicate configured port: {port}")
        seen.add(port)
        native = app in {"video-merger", "local-upload-manager"}
        route = "/" if app == "scene-builder" else f"/{app}/"
        result.append({
            "app": app, "port": port, "bind": "127.0.0.1",
            "module": spec.get("module", spec.get("script")),
            "health": spec["health"], "ui": spec["ui"],
            "https_path": route,
            "public_health": (spec["health"] if native else f"/{app}{spec['health']}")
                             if app != "scene-builder" else "/health",
            "service": f"vps1-production-{app}.service",
        })
    if {STORAGE_PORT, WEB_PORT, GATEWAY_PORT} & seen:
        raise ValueError("candidate, storage or gateway port conflicts with installer")
    result.append({
        "app": "vps-storage-manager", "port": STORAGE_PORT, "bind": "127.0.0.1",
        "module": "storage_manager.py serve", "health": "/health", "ui": None,
        "https_path": "/admin/storage", "public_health": None,
        "service": "vps1-storage-candidate.service",
    })
    result.append({
        "app": "scene-public-candidate", "port": WEB_PORT, "bind": "127.0.0.1",
        "module": "existing Scene public candidate", "health": "/health",
        "ui": "/login", "https_path": "/", "public_health": "/health",
        "service": "vps1-scene-candidate.service",
    })
    return result


def process(args, timeout=8):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def listeners():
    """Return local port -> addresses, without leaking process command lines."""
    p = process(["ss", "-H", "-ltn"], timeout=10)
    if not p or p.returncode:
        return {}
    result = {}
    for line in p.stdout.splitlines():
        columns = line.split()
        if len(columns) < 4:
            continue
        local = columns[3]
        m = re.search(r":(\d+)$", local)
        if not m:
            continue
        port = int(m.group(1))
        result.setdefault(port, set()).add(local[:m.start()].strip("[]"))
    return {k: sorted(v) for k, v in result.items()}


def http_probe(port, path):
    try:
        with request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
            body = json.load(r)
            return "PASS" if r.status == 200 and isinstance(body, dict) and body.get("ok") is True else "BAD_HEALTH"
    except (error.HTTPError, error.URLError, TimeoutError, OSError, ValueError):
        return "UNAVAILABLE"


def tls_probe(path):
    """Trusted certificate + SNI; anonymous GET only, no session/secret use."""
    try:
        with socket.create_connection(("127.0.0.1", 443), timeout=5) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=DOMAIN) as tls:
                conn = http.client.HTTPSConnection(DOMAIN, timeout=6)
                conn.sock = tls
                conn.request("GET", path, headers={"Host": DOMAIN, "Connection": "close"})
                response = conn.getresponse()
                status = response.status
                response.read(2048)
                conn.close()
                return status
    except (OSError, ssl.SSLError, TimeoutError, http.client.HTTPException):
        return None


def service_state(unit):
    p = process(["systemctl", "is-active", unit])
    return "ACTIVE" if p and p.returncode == 0 else "NOT_ACTIVE"


def helper_state():
    try:
        if not HELPER.is_file() or HELPER.is_symlink():
            return "ABSENT_OR_UNEXPECTED"
        expected = REPO / "production/remote_deploy.py"
        if not expected.is_file():
            return "SOURCE_UNAVAILABLE"
        installed_sha = hashlib.sha256(HELPER.read_bytes()).digest()
        source_sha = hashlib.sha256(expected.read_bytes()).digest()
        return "SOURCE_MATCH" if installed_sha == source_sha else "UPGRADE_REQUIRED_OR_DIFFERENT_REVISION"
    except PermissionError:
        return "UNVERIFIABLE_NON_ROOT"


def safe_presence(path):
    """Do not traverse root-only ACME symlinks or relax file permissions."""
    try:
        return "PRESENT" if path.is_file() else "ABSENT"
    except PermissionError:
        return "UNVERIFIABLE_NON_ROOT"


def nginx_syntax_state():
    """nginx -t commonly requires root to read private TLS keys."""
    if not shutil.which("nginx"):
        return "UNAVAILABLE"
    probe = process(["nginx", "-t"], timeout=12)
    if probe is None:
        return "UNAVAILABLE"
    if probe.returncode == 0:
        return "PASS"
    # Distinguish non-root inspection limits from actual Nginx failure.
    stderr = (probe.stderr or "").lower()
    if "permission denied" in stderr or "operation not permitted" in stderr:
        return "UNVERIFIABLE_NON_ROOT"
    return "FAIL"


def helper_route_capability():
    """Check only an allowlisted operation marker, never return helper source."""
    try:
        if not HELPER.is_file() or HELPER.is_symlink():
            return "NOT_INSTALLED"
        return "YES" if b"nginx-route-only" in HELPER.read_bytes() else "NO"
    except PermissionError:
        return "UNVERIFIABLE_NON_ROOT"


def site_route_ownership():
    """Summarize reviewed Nginx locations without disclosing the vhost file."""
    try:
        data = SITE.read_text()
    except FileNotFoundError:
        return {"status": "SITE_ABSENT"}
    except PermissionError:
        return {"status": "UNVERIFIABLE_NON_ROOT"}
    matches = {}
    for app in CATALOG:
        if app == "scene-builder":
            continue
        prefix = "/" + app + "/"
        matches[app] = (
            f"location ^~ {prefix}" in data or
            f"include /etc/nginx/snippets/vps1-route-{app}.conf;" in data or
            ("include /etc/nginx/snippets/vps1-workspace-https.conf;" in data)
        )
    return {
        "status": "READABLE",
        "reviewed_marker_present": data.count(
            "location = /internal/runtime-ready { return 404; }") == 1,
        "workspace_include": "include /etc/nginx/snippets/vps1-workspace-https.conf;" in data,
        "scene_public_upstream": "proxy_pass http://127.0.0.1:18080;" in data,
        "routes_or_workspace_include": matches,
    }


def gateway_anon_status():
    try:
        with request.urlopen(f"http://127.0.0.1:{GATEWAY_PORT}/", timeout=4) as r:
            return r.status
    except error.HTTPError as exc:
        return exc.code
    except (error.URLError, OSError, TimeoutError):
        return None


def host_inventory(source):
    occupied = listeners()
    checks = []
    for item in source:
        binds = occupied.get(item["port"], [])
        exposed = any(b not in {"127.0.0.1", "::1"} for b in binds)
        checks.append({
            "app": item["app"], "port": item["port"],
            "listen": "EXPOSED" if exposed else "LOOPBACK" if binds else "NO_LISTENER",
            "service": service_state(item["service"]),
            "health": http_probe(item["port"], item["health"]),
        })
    routes = {}
    for item in source:
        if item["app"] in {"scene-builder", "scene-public-candidate", "vps-storage-manager"}:
            continue
        code = tls_probe(item["https_path"])
        state = (
            "AUTH_REQUIRED" if code in {401, 403} else
            "REDIRECT_NEEDS_SESSION_REVIEW" if code in {302, 303, 307} else
            "MISSING" if code == 404 else
            "PUBLIC_200_REVIEW_REQUIRED" if code == 200 else
            "ERROR_OR_UNAVAILABLE"
        )
        routes[item["app"]] = {"anonymous_http_status": code, "state": state}
    return {
        "services": checks,
        "nginx_syntax": nginx_syntax_state(),
        "site_present": safe_presence(SITE), "certificate_present": safe_presence(CERT),
        "scene_login_status": tls_probe("/login"),
        "scene_health_status": tls_probe("/health"),
        "workspace_unauthenticated_status": tls_probe("/workspace/"),
        "gateway_unauthenticated_status": gateway_anon_status(),
        "installed_helper": helper_state(),
        "helper_route_only_capability": helper_route_capability(),
        "scene_site_route_ownership": site_route_ownership(),
        "independent_https": routes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", action="store_true",
                        help="Read-only inspect actual VPS services and HTTPS routes")
    args = parser.parse_args()
    source = catalog()
    out = {
        "schema_version": 1, "domain": DOMAIN,
        "mode": "host_read_only" if args.host else "source_only",
        "source_catalog": source, "gateway_port": GATEWAY_PORT,
        "auto_script": "OFFLINE", "deployed": False,
    }
    if args.host:
        out["host"] = host_inventory(source)
        # Authenticated deployment PASS is deliberately not inferred here.
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

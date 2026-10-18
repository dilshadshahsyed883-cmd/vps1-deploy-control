#!/usr/bin/env python3
"""App-scoped, GitHub-controlled HTTPS route release; no app install or data change.

Runs only via the existing root-owned vps1-production-deploy allowlisted helper.
A previous helper without this operation must be upgraded in a separate guarded
bootstrap: CI cannot silently grant itself broader sudo permissions.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import itertools
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from production import gateway
from production.runtime import BASE, CATALOG, check_host, deployment_lock
from production.scene_public_https import CERT, ENABLED, KEY, SITE, nginx_https, VOICE_ANCHOR, VOICE_EXTRA
from production.route_auto_caption_scene_https import ROUTE as ACS_ROUTE, add_route as add_acs_route
from production.route_uploader_scene_https import ROUTE as UPLOADER_ROUTE, add_route as add_uploader_route

DOMAIN = "app.scenebuilder.online"
APPS = tuple(a for a in CATALOG if a != "scene-builder")
SNIPPET_DIR = Path("/etc/nginx/snippets")
TRANSACTIONS = BASE / "transactions/nginx-route-only"
INCLUDE_MARKER = " location = /internal/runtime-ready { return 404; }"
def managed_re():
    return re.compile(r"^ include " + re.escape(str(SNIPPET_DIR)) +
                      r"/vps1-route-([a-z0-9-]+)\.conf;\n", re.M)
WORKSPACE_INCLUDE = " include /etc/nginx/snippets/vps1-workspace-https.conf;\n"
# Public requests cannot call the Scene internal SSO introspection endpoint.
SCENE_SESSION_HIDE = " location = /internal/workspace-auth { return 404; }\n"


def guard(condition, message):
    if not condition:
        raise RuntimeError("BLOCKED: " + message)


def digest(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def known_sites():
    """Account for reviewed Scene + Voice + Caption + session Uploader history."""
    base = nginx_https()
    sites = {base}
    variants = ("voice", "caption", "uploader")
    for size in range(1, 4):
        for sequence in itertools.permutations(variants, size):
            current = base
            for feature in sequence:
                if feature == "voice":
                    current = current.replace(VOICE_ANCHOR, VOICE_EXTRA + VOICE_ANCHOR, 1)
                elif feature == "caption":
                    current = add_acs_route(current)
                else:
                    current, _ = add_uploader_route(current)
            sites.add(current)
    return sites


def owned_routes(site):
    """Return (original reviewed site, app IDs) after exact managed-include check."""
    names = managed_re().findall(site)
    guard(len(names) == len(set(names)) and all(a in APPS for a in names),
          "unknown or repeated managed Nginx route")
    base = managed_re().sub("", site)
    guard(base.count(SCENE_SESSION_HIDE) <= 1, "duplicate internal SSO deny location")
    base = base.replace(SCENE_SESSION_HIDE, "", 1)
    guard(base in known_sites(), "existing Scene HTTPS site drift; no Nginx change")
    guard(site.count(INCLUDE_MARKER) == 1, "Scene HTTPS marker mismatch")
    for app in names:
        path = snippet_path(app)
        guard(path.is_file() and not path.is_symlink(), "managed route snippet absent: " + app)
        from production.scene_session_routes import config as session_route_config, APP_IDS
        accepted = (route_config(app), session_route_config(app)) if app in APP_IDS else (route_config(app),)
        guard(path.read_text() in accepted, "managed route snippet drift: " + app)
        guard(not standalone(base, app), "duplicate managed and preexisting route: " + app)
    return base, names


def snippet_path(app):
    guard(app in APPS, "unapproved application")
    return SNIPPET_DIR / ("vps1-route-" + app + ".conf")


def standalone(base, app):
    if app == "voice-studio":
        return VOICE_EXTRA in base
    if app == "auto-caption-studio":
        return ACS_ROUTE in base
    if app == "local-upload-manager":
        return UPLOADER_ROUTE in base
    return False


def route_config(app):
    guard(app in APPS, "unapproved application")
    p = "/" + app
    return (f"# VPS1_GITHUB_ROUTE_ONLY_V1 {app}\n"
            f"location = {p} {{ return 308 {p}/; }}\n"
            f"location ^~ {p}/ {{\n"
            "    proxy_pass http://127.0.0.1:8888;\n"
            "    proxy_http_version 1.1;\n"
            "    proxy_set_header Host $host;\n"
            "    proxy_set_header Authorization $http_authorization;\n"
            "    proxy_set_header X-Forwarded-Proto https;\n"
            "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "    proxy_set_header Upgrade $http_upgrade;\n"
            '    proxy_set_header Connection "upgrade";\n'
            "    proxy_read_timeout 300s;\n"
            "    proxy_send_timeout 300s;\n"
            "}\n")


def add_route(site, app):
    """Return (new site, changed); cannot modify unowned or existing locations."""
    guard(WORKSPACE_INCLUDE not in site,
          "prior workspace include needs separate reconciliation")
    base, names = owned_routes(site)
    if app in names or standalone(base, app):
        return site, False
    p = "/" + app
    guard(("location ^~ " + p + "/") not in site and
          ("location = " + p + " ") not in site,
          "application route owned by another config")
    guard(INCLUDE_MARKER in site, "Scene HTTPS marker unavailable")
    snippet = f" include {snippet_path(app)};\n"
    return site.replace(INCLUDE_MARKER, snippet + INCLUDE_MARKER, 1), True


def active(service):
    return subprocess.run(["systemctl", "is-active", "--quiet", service],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def command(args):
    subprocess.run(args, check=True, capture_output=True, timeout=25)


def atomic(path, data, mode=0o644):
    """Atomic, same-directory configuration write; never follow a target symlink."""
    guard(not path.is_symlink(), "refusing symlinked Nginx file")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False,
                                     prefix=".vps1-nginx-route-") as stream:
        tmp = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        tmp.chmod(mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def tls_get(path, authorization="", cookie=""):
    """Trust the signed live certificate and SNI while connecting over loopback."""
    with socket.create_connection(("127.0.0.1", 443), timeout=8) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=DOMAIN) as tls:
            conn = http.client.HTTPSConnection(DOMAIN)
            conn.sock = tls
            headers = {"Host": DOMAIN, "Connection": "close"}
            if authorization:
                headers["Authorization"] = authorization
            if cookie:
                headers["Cookie"] = cookie
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type", ""), resp.read(262144)


def credentials():
    guard(gateway.LOGIN.is_file(), "existing workspace login record missing")
    data = json.loads(gateway.LOGIN.read_text())
    user, password = data.get("username"), data.get("password")
    guard(isinstance(user, str) and bool(user) and
          isinstance(password, str) and len(password) >= 20,
          "existing workspace login invalid")
    return "Basic " + base64.b64encode((user + ":" + password).encode()).decode()


def check_scene():
    status, ct, _ = tls_get("/login")
    guard(status == 200 and "html" in ct.lower(), "Scene HTTPS login regression")
    status, _, body = tls_get("/health")
    guard(status == 200 and json.loads(body).get("ok") is True,
          "Scene HTTPS health regression")
    status, _, _ = tls_get("/internal/runtime-ready")
    guard(status == 404, "protected Scene endpoint exposed")


def public_path(app, name):
    spec = CATALOG[app]
    value = spec[name]
    if app in {"video-merger", "local-upload-manager"}:
        return value
    return "/" + app + value


# Old Nginx workers can briefly serve the existing Scene login redirect
# after a graceful reload. Redirects are never accepted as successful auth.
RELOAD_TRANSIENT_HTTP = frozenset((302, 303, 307, 308, 404, 502, 503))


def require_https_status(url, *, expected, app, authorization="", attempts=18, pause=0.6):
    """Wait for verified Nginx handover; fail closed on unexpected HTTP codes.

    The expected 401 protects unauthenticated routes. Authenticated 200 is
    accepted only by the caller after checking API JSON or UI HTML. A stale
    Scene login redirect is only retried for a short, bounded interval.
    """
    for i in range(attempts):
        status, content_type, body = tls_get(url, authorization=authorization)
        if status == expected:
            return content_type, body
        guard(status in RELOAD_TRANSIENT_HTTP and i + 1 < attempts,
              "HTTPS route validation failed: " + app +
              " expected=" + str(expected) + " observed=" + str(status))
        time.sleep(pause)
    raise RuntimeError("unreachable HTTPS check")


def check_app(app, auth, *, session_owned=False):
    if session_owned:
        # Session-bound uploader is intentionally NOT converted to Basic Auth.
        from production.route_uploader_scene_https import scene_cookie, verify
        verify(scene_cookie(), published=True)
        return
    path = public_path(app, "health")
    ui = public_path(app, "ui")
    for url in (path, ui):
        require_https_status(url, expected=401, app=app)
        ct, body = require_https_status(url, expected=200, app=app,
                                        authorization=auth)
        if url == path:
            guard(json.loads(body).get("ok") is True, "app health payload invalid: " + app)
        else:
            guard("html" in ct.lower() and b"<" in body,
                  "app HTTPS browser document unavailable: " + app)


def preflight(revision, app, *, require_app=True):
    check_host(revision)
    guard(app in APPS, "only independent app HTTPS routes may be published")
    guard(SITE.is_file() and not SITE.is_symlink() and ENABLED.is_symlink()
          and ENABLED.resolve() == SITE,
          "Scene HTTPS site ownership mismatch")
    guard(CERT.is_file() and KEY.is_file(), "existing Scene certificate missing")
    for unit in ("nginx.service", "vps1-scene-candidate.service"):
        guard(active(unit), "required service unavailable: " + unit)
    if not require_app:
        # The route must remain removable when its backend or gateway is down.
        return None
    guard(active("vps1-production-" + app + ".service"),
          "required app service unavailable: " + app)
    data = json.loads((BASE / "production-status.json").read_text())
    guard(data.get("gateway") == "ACTIVE" and app in data.get("apps", []),
          "app not published in existing authenticated workspace gateway")
    auth = credentials()
    check_scene()
    return auth


def publish(revision, app):
    auth = preflight(revision, app)
    old = SITE.read_text()
    # Existing HTTPS apps transition one at a time to the already-validated
    # Scene cookie, never reinstalling or restarting app services.
    from production import scene_session_routes as sso
    if app in sso.APP_IDS:
        base, managed = owned_routes(old)
        if sso.active(app, old):
            sso.verify(app, sso.ready_cookie(app))
            print("SCENE_SESSION_SSO=ALREADY_LIVE APP=" + app, flush=True)
            return
        if app in managed or standalone(base, app):
            sso.migrate(revision, app)
            return
    new, changed = add_route(old, app)
    base, names = owned_routes(old)
    if not changed:
        check_app(app, auth, session_owned=(app == "local-upload-manager"
                                           and standalone(base, app)))
        print("NGINX_ROUTE_ONLY=ALREADY_LIVE APP=" + app, flush=True)
        return
    route = snippet_path(app)
    guard(not route.exists() and not route.is_symlink(),
          "route file already exists without owned Nginx include")
    tx = TRANSACTIONS / (app + "-" + str(time.time_ns()))
    tx.mkdir(parents=True, mode=0o700, exist_ok=False)
    (tx / "before.site").write_bytes(old.encode())
    (tx / "before.site").chmod(0o600)
    wrote_site = False
    try:
        guard(SITE.read_text() == old, "concurrent Nginx site modification")
        atomic(route, route_config(app).encode())
        atomic(SITE, new.encode())
        wrote_site = True
        command(["nginx", "-t"])
        command(["systemctl", "reload", "nginx"])
        check_app(app, auth)
        check_scene()
        manifest = {
            "app": app, "revision": revision, "state": "ACTIVE",
            "before_site_sha256": digest(old),
            "after_site_sha256": digest(new),
            "route_sha256": digest(route_config(app)),
            "transaction": str(tx),
            "changed_services": [], "owner_uat": "PENDING"}
        atomic(tx / "release.json", (json.dumps(manifest, sort_keys=True) + "\n").encode(), 0o600)
        atomic(TRANSACTIONS / (app + ".json"),
               (json.dumps(manifest, sort_keys=True) + "\n").encode(), 0o600)
        print("NGINX_ROUTE_ONLY=PASS APP=" + app +
              " HTTPS=https://" + DOMAIN + "/" + app + "/ USER_TESTING=PENDING", flush=True)
    except BaseException:
        if wrote_site and SITE.read_text() == new:
            atomic(SITE, old.encode())
        elif SITE.read_text() != old:
            raise RuntimeError("BLOCKED: concurrent site edit; manual reconciliation needed")
        route.unlink(missing_ok=True)
        command(["nginx", "-t"])
        command(["systemctl", "reload", "nginx"])
        print("NGINX_ROUTE_ONLY=ROLLED_BACK APP=" + app, flush=True)
        raise


def rollback(revision, app):
    preflight(revision, app, require_app=False)
    from production import scene_session_routes as sso
    sso_record = TRANSACTIONS / ("sso-" + app + ".json")
    if app in sso.APP_IDS and sso_record.is_file() and not sso_record.is_symlink():
        state = json.loads(sso_record.read_text())
        if state.get("state") == "ACTIVE":
            sso.rollback(revision, app)
            return
    record_file = TRANSACTIONS / (app + ".json")
    guard(record_file.is_file() and not record_file.is_symlink(),
          "no recorded route-only release for app")
    record = json.loads(record_file.read_text())
    guard(record.get("app") == app and record.get("state") == "ACTIVE",
          "no active route release to roll back")
    tx = TRANSACTIONS / Path(record["transaction"]).name
    guard(str(tx) == record["transaction"] and tx.is_dir(),
          "route rollback transaction not trusted")
    old_site = (tx / "before.site").read_bytes()
    current_site = SITE.read_bytes()
    route = snippet_path(app)
    guard(digest(old_site) == record["before_site_sha256"] and
          digest(current_site) == record["after_site_sha256"] and
          route.is_file() and not route.is_symlink() and
          digest(route.read_bytes()) == record["route_sha256"],
          "route modified after release; safe rollback blocked")
    guard(add_route(old_site.decode(), app)[0] == current_site.decode(),
          "rollback source no longer matches reviewed route")
    try:
        atomic(SITE, old_site)
        route.unlink()
        command(["nginx", "-t"])
        command(["systemctl", "reload", "nginx"])
        record["state"] = "ROLLED_BACK"
        record["rollback_revision"] = revision
        atomic(record_file, (json.dumps(record, sort_keys=True) + "\n").encode(), 0o600)
        # A dead app/Scene service must not undo a successful route rollback.
        try:
            check_scene()
        except Exception:
            print("SCENE_HEALTH=DEGRADED_AFTER_ROUTE_ROLLBACK", flush=True)
        print("NGINX_ROUTE_ROLLBACK=PASS APP=" + app, flush=True)
    except BaseException:
        atomic(route, route_config(app).encode())
        atomic(SITE, current_site)
        command(["nginx", "-t"])
        command(["systemctl", "reload", "nginx"])
        print("NGINX_ROUTE_ROLLBACK=RESTORED_ACTIVE_ROUTE APP=" + app, flush=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["nginx-route-only", "nginx-route-rollback"])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--app", required=True, choices=APPS)
    args = parser.parse_args()
    with deployment_lock():
        if args.operation == "nginx-route-only":
            publish(args.revision, args.app)
        else:
            rollback(args.revision, args.app)


if __name__ == "__main__":
    main()

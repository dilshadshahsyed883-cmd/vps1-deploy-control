#!/usr/bin/env python3
"""Publish the existing Auto Uploader under the Scene HTTPS session.

This changes only the Scene Nginx site. It never changes uploader data, OAuth
credentials, worker state, schedules or queues. Invoke on VPS_1 via the locked
Drive Bridge agent after the reviewed revision is on protected main.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from production.scene_public_https import CERT, DOMAIN, ENABLED, HOST, KEY, SITE

SOURCE = "https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control.git"
SCENE = "http://127.0.0.1:18080"
UPLOADER = "http://127.0.0.1:8015"
ANCHOR = " location / {\n    proxy_pass http://127.0.0.1:18080;"
MARKER = "# VPS1_SCENE_SESSION_UPLOADER_ROUTE_V1"
TRANSACTIONS = Path("/srv/fresh-website/transactions/scene-uploader-https")

# An explicit exact /local-upload-manager proxy keeps Scene Builder's outer
# shell. Without it, Nginx automatically redirects to the slash-terminated
# uploader prefix because that location has proxy_pass.
# Only /local-upload-manager/* is handed to the isolated uploader service.
# Nginx performs the Scene session check; client-supplied identity headers are
# overwritten with values from the protected authorization subrequest.
APP_EXACT_ROUTE = """ location = /local-upload-manager/app {
    auth_request /_scene_uploader_auth;
    auth_request_set $scene_lum_user $upstream_http_x_auth_user;
    auth_request_set $scene_lum_access $upstream_http_x_app_access;
    auth_request_set $scene_lum_admin $upstream_http_x_is_admin;
    proxy_pass http://127.0.0.1:8015;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Auth-User $scene_lum_user;
    proxy_set_header X-App-Access $scene_lum_access;
    proxy_set_header X-Is-Admin $scene_lum_admin;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
 }
"""

ROUTE = """ # VPS1_SCENE_SESSION_UPLOADER_ROUTE_V1
 location = /local-upload-manager/ { return 308 /local-upload-manager; }
 location = /local-upload-manager {
    proxy_pass http://127.0.0.1:18080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
 }
 location = /_scene_uploader_auth {
    internal;
    proxy_pass http://127.0.0.1:18080/internal/local-upload-manager-auth;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Cookie $http_cookie;
 }
""" + APP_EXACT_ROUTE + """ location ^~ /local-upload-manager/ {
    auth_request /_scene_uploader_auth;
    auth_request_set $scene_lum_user $upstream_http_x_auth_user;
    auth_request_set $scene_lum_access $upstream_http_x_app_access;
    auth_request_set $scene_lum_admin $upstream_http_x_is_admin;
    proxy_pass http://127.0.0.1:8015;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Auth-User $scene_lum_user;
    proxy_set_header X-App-Access $scene_lum_access;
    proxy_set_header X-Is-Admin $scene_lum_admin;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
 }
"""
# The previous approved production route lacked only the exact slash handler.
# Recognize precisely that old route for an app-scoped, reversible upgrade.
PREVIOUS_ROUTE = ROUTE.replace(APP_EXACT_ROUTE, "", 1)
LEGACY_ROUTE = PREVIOUS_ROUTE.replace(
    " location = /local-upload-manager/ { return 308 /local-upload-manager; }\n", "", 1
)


def guard(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError("BLOCKED: " + message)


def add_route(original: str) -> tuple[str, bool]:
    guard(original.count(ANCHOR) == 1 and original.count("listen 443 ssl;") == 1,
          "unrecognized Scene HTTPS site")
    guard(original.count("server_name app.scenebuilder.online;") == 2
          and "location = /internal/runtime-ready { return 404; }" in original,
          "unexpected Scene TLS guard")
    if MARKER in original:
        guard(original.count(MARKER) == 1, "duplicate uploader route marker")
        if ROUTE in original:
            return original, False
        for previous in (PREVIOUS_ROUTE, LEGACY_ROUTE):
            if previous in original:
                return original.replace(previous, ROUTE, 1), True
        raise RuntimeError("BLOCKED: existing uploader route differs from this release")
    guard("location = /local-upload-manager " not in original
          and "location ^~ /local-upload-manager/" not in original
          and "location = /_scene_uploader_auth" not in original,
          "uploader route is already owned by another config")
    return original.replace(ANCHOR, ROUTE + ANCHOR, 1), True


def retire_legacy_managed_route(original: str) -> tuple[str, bool]:
    """Detach only the reviewed legacy uploader include.

    The first HTTPS cutover published an exact-path redirect plus a prefix
    proxy through a managed snippet. The Scene-integrated UI intentionally
    keeps the exact path in Scene and owns only the prefix. Preserve the
    snippet file so restoring ``original`` is a complete rollback.
    """
    from production import nginx_route_only as legacy

    include = f" include {legacy.snippet_path('local-upload-manager')};\n"
    if include not in original:
        return original, False
    guard(original.count(include) == 1, "duplicate legacy uploader include")
    _, names = legacy.owned_routes(original)
    guard("local-upload-manager" in names,
          "legacy uploader include is not a reviewed managed route")
    return original.replace(include, "", 1), True


def run(args: list[str], timeout: int = 25) -> str:
    proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    guard(proc.returncode == 0, "command failed: " + args[0])
    return proc.stdout.strip()


def active(service: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", service],
                          capture_output=True).returncode == 0


def local_health() -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(UPLOADER + "/local-upload-manager/health", timeout=8) as response:
        health = json.load(response)
    guard(health.get("ok") is True and health.get("base_path") == "/local-upload-manager"
          and health.get("suite_auth") is True,
          "installed uploader base path/auth does not match the new route")


def scene_cookie() -> str:
    env = Path("/etc/fresh-website/scene-builder.env")
    guard(env.is_file(), "Scene credentials file missing")
    settings = dict(line.split("=", 1) for line in env.read_text().splitlines()
                    if "=" in line and not line.startswith("#"))
    username, secret = settings.get("WEB_USERNAME", "team"), settings.get("WEB_ACCESS_KEY", "")
    guard(len(secret) >= 24, "Scene login credential not provisioned")
    body = urllib.parse.urlencode({"username": username, "secret_key": secret}).encode()
    request = urllib.request.Request(SCENE + "/login", data=body, method="POST")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        opener.open(request, timeout=8)
    except urllib.error.HTTPError as exc:
        guard(exc.code == 303, "Scene login did not return an authenticated redirect")
        cookie = exc.headers.get("Set-Cookie", "").split(";", 1)[0]
        guard(cookie.startswith("session="), "Scene session cookie absent")
        return cookie
    raise RuntimeError("BLOCKED: Scene login did not redirect")


def https_get(path: str, cookie: str = "") -> tuple[int, str, bytes]:
    with socket.create_connection(("127.0.0.1", 443), timeout=8) as sock:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=DOMAIN) as tls:
            conn = http.client.HTTPSConnection(DOMAIN)
            conn.sock = tls
            headers = {"Host": DOMAIN, "Connection": "close"}
            if cookie:
                headers["Cookie"] = cookie
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type", ""), response.read(180000)


OUTER_HANDOVER_HTTP = frozenset((301, 302, 303, 307, 308, 401, 404, 502, 503))


def verify_outer_shell(cookie: str, *, published: bool) -> None:
    """Verify the native uploader redirect and its real protected app document."""
    attempts = 12 if published else 1
    for attempt in range(attempts):
        status, _content_type, _body = https_get("/local-upload-manager", cookie)
        if status == 303:
            code, kind, body = https_get(
                "/local-upload-manager/app?embedded=1&tab=dashboard", cookie)
            guard(code == 200 and "html" in kind.lower() and b"lum-shell" in body,
                  "native uploader document missing after redirect")
            guard(b"id=\"lum-host-frame\"" not in body,
                  "nested uploader iframe returned after native redirect")
            return
        guard(published and status in OUTER_HANDOVER_HTTP and attempt + 1 < attempts,
              "Scene Auto Uploader outer shell regressed")
        time.sleep(0.6)
    raise RuntimeError("BLOCKED: Scene Auto Uploader outer shell regressed")


def verify(cookie: str, *, published: bool) -> None:
    status, ct, body = https_get("/login")
    guard(status == 200 and "html" in ct.lower(), "Scene HTTPS login regressed")
    status, _, body = https_get("/health")
    guard(status == 200 and json.loads(body).get("ok") is True,
          "Scene HTTPS health regressed")
    status, _, _ = https_get("/internal/runtime-ready")
    guard(status == 404, "Scene internal probe exposed")
    verify_outer_shell(cookie, published=published)
    if published:
        # The slash URL must return to Scene's original sidebar, not the
        # standalone LUM application. Old Nginx workers may linger briefly.
        for attempt in range(12):
            status, _, _ = https_get("/local-upload-manager/", cookie)
            if status == 308:
                break
            guard(attempt < 11 and status in OUTER_HANDOVER_HTTP | {200},
                  "Uploader trailing slash still bypasses Scene sidebar")
            time.sleep(0.6)
        else:
            raise RuntimeError("BLOCKED: uploader slash route did not settle")
        # Nginx reload can briefly serve old workers. Never accept old 404/303.
        for attempt in range(12):
            unauth, _, _ = https_get("/local-upload-manager/app")
            authed, content_type, html = https_get(
                "/local-upload-manager/app?embedded=1&tab=auto_engine&auto_mode=engine1",
                cookie)
            if unauth == 401 and authed == 200 and "html" in content_type.lower():
                guard(b"Auto Engine" in html or b"auto_engine" in html,
                      "uploader HTML does not expose Auto Engine")
                break
            if attempt == 11:
                raise RuntimeError("BLOCKED: uploader HTTPS session route not ready")
            time.sleep(0.6)
        # Verify the owner's original Engine 1 URL explicitly, without a
        # browser/canonical Song Uploader redirect. This check is read-only.
        for attempt in range(12):
            legacy_status, legacy_kind, legacy_html = https_get(
                "/local-upload-manager/app?tab=auto_engine&embedded=1", cookie)
            if legacy_status == 200 and "html" in legacy_kind.lower():
                guard(b"Auto Uploader" in legacy_html,
                      "legacy LUM URL no longer serves Auto Uploader")
                guard(b"Song Uploader \\xc2\\xb7 Short Uploader Auto Engine" not in legacy_html,
                      "legacy LUM Engine 1 URL was routed to Song Uploader")
                break
            guard(attempt < 11 and legacy_status in OUTER_HANDOVER_HTTP,
                  "legacy LUM Auto Engine URL redirected or failed")
            time.sleep(0.6)
        status, _, body = https_get("/local-upload-manager/health", cookie)
        guard(status == 200 and json.loads(body).get("ok") is True,
              "authenticated uploader health failed")
        status, _, _ = https_get("/_scene_uploader_auth", cookie)
        guard(status == 404, "internal identity check endpoint exposed")


def atomic_site(text: str) -> None:
    with tempfile.NamedTemporaryFile("w", dir=SITE.parent, prefix=".scene-lum-",
                                     delete=False) as file:
        temp = Path(file.name)
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    try:
        temp.chmod(0o644)
        os.replace(temp, SITE)
    finally:
        temp.unlink(missing_ok=True)


def deploy(revision: str) -> None:
    guard(os.geteuid() == 0 and socket.gethostname().split(".")[0] == HOST,
          "requires approved VPS_1 root execution")
    guard(re.fullmatch(r"[a-f0-9]{40}", revision) is not None, "invalid source SHA")
    guard(SITE.is_file() and ENABLED.is_symlink() and ENABLED.resolve() == SITE,
          "Scene Nginx config ownership mismatch")
    guard(CERT.is_file() and KEY.is_file(), "Scene TLS certificate missing")
    guard(shutil.disk_usage("/").free >= 5_000_000_000,
          "operational disk reserve unavailable")
    for unit in ("nginx.service", "vps1-scene-candidate.service",
                 "vps1-production-local-upload-manager.service"):
        guard(active(unit), unit + " not active")
    guard(run(["git", "ls-remote", SOURCE, "refs/heads/main"], 30).split()[0] == revision,
          "source revision must equal current protected main")
    local_health()
    old = SITE.read_text()
    reconciled, retired_legacy = retire_legacy_managed_route(old)
    new, added_session = add_route(reconciled)
    changed = retired_legacy or added_session
    cookie = scene_cookie()
    if not changed:
        verify(cookie, published=True)
        print("VPS1_SCENE_LUM RESULT=ALREADY_LIVE VERIFIED=YES", flush=True)
        return
    tx = TRANSACTIONS / revision
    guard(not tx.exists(), "this route transaction was already attempted")
    tx.mkdir(parents=True, mode=0o700)
    (tx / "before.conf").write_text(old)
    (tx / "before.conf").chmod(0o600)
    try:
        guard(SITE.read_text() == old, "concurrent Nginx config change")
        atomic_site(new)
        run(["nginx", "-t"])
        run(["systemctl", "reload", "nginx"])
        verify(cookie, published=True)
        (tx / "release.json").write_text(json.dumps({
            "source_commit": revision, "route": "/local-upload-manager/",
            "before_sha256": hashlib.sha256(old.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(new.encode()).hexdigest(),
            "scene_cookie_test": "PASS", "anonymous_denied": True,
            "rollback_file": str(tx / "before.conf")
        }, sort_keys=True) + "\n")
        (tx / "release.json").chmod(0o600)
        print("VPS1_SCENE_LUM RESULT=PASS ROUTE=LIVE AUTH=PASS HTML=PASS", flush=True)
    except BaseException:
        atomic_site(old)
        try:
            run(["nginx", "-t"])
            run(["systemctl", "reload", "nginx"])
            print("VPS1_SCENE_LUM ROLLBACK=PREVIOUS_SITE_RESTORED", flush=True)
        except Exception:
            print("VPS1_SCENE_LUM ROLLBACK=MANUAL_RECOVERY_REQUIRED", flush=True)
        raise


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "deploy":
        raise SystemExit("usage: python -m production.route_uploader_scene_https deploy <main_sha>")
    try:
        deploy(sys.argv[2])
    except Exception as exc:
        print("VPS1_SCENE_LUM RESULT=BLOCKED " + str(exc).splitlines()[0][:220], flush=True)
        raise SystemExit(24)

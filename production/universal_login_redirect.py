"""One reviewed, reversible browser-login 401 policy for the Scene HTTPS host.

Only HTML GET/HEAD navigations are redirected. API, AJAX, upload, WebSocket,
non-GET, and non-401 authorization failures retain their original status.
No gateway credentials, application data, or running jobs are modified.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from production import nginx_route_only as nginx
from production import scene_session_routes as sso
from production.route_uploader_scene_https import SITE, ENABLED, MARKER as LUM_MARKER

MARKER = "# VPS1_UNIVERSAL_BROWSER_LOGIN_401_V1"
ANCHOR = " location = /internal/runtime-ready { return 404; }"
RULE = """ # VPS1_UNIVERSAL_BROWSER_LOGIN_401_V1
 error_page 401 = @vps1_browser_login_401;
 location @vps1_browser_login_401 {
    # Machine-to-machine calls must never receive a login HTML redirect.
    if ($request_method !~ ^(GET|HEAD)$) { return 401; }
    if ($uri ~* "(^|/)(api|health|healthz)(/|$)") { return 401; }
    if ($http_upgrade != "") { return 401; }
    if ($http_accept !~* "text/html") { return 401; }
    add_header Cache-Control "private, no-store" always;
    return 303 /login?next=$request_uri;
 }
"""
JOURNALS = Path("/srv/fresh-website/transactions/universal-browser-login")


def guard(ok, reason):
    if not ok:
        raise RuntimeError("UNIVERSAL_LOGIN_BLOCKED: " + reason)


def apply(site: str) -> str:
    """Allow one exact change inside the already-reviewed Scene TLS server."""
    guard(site.count(" listen 443 ssl;") == 1 and
          site.count(" server_name app.scenebuilder.online;") == 2 and
          site.count(ANCHOR) == 1, "unexpected TLS server structure")
    guard(LUM_MARKER in site and
          "location = /_scene_uploader_auth {" in site,
          "shared Scene session uploader route not present")
    guard(" include /etc/nginx/snippets/vps1-route-short-maker.conf;" in site,
          "Short Maker managed route absent")
    if MARKER in site:
        guard(site.count(MARKER) == 1 and site.count(RULE) == 1,
              "existing universal login policy differs from reviewed source")
        return site
    guard("@vps1_browser_login_401" not in site and
          "error_page 401" not in site,
          "a different 401 handler already owns the site")
    return site.replace(ANCHOR, RULE + ANCHOR, 1)


def _short_maker_sso_ready() -> bool:
    route = nginx.snippet_path("short-maker")
    if not route.is_file() or route.is_symlink():
        return False
    value = route.read_text()
    return (value.startswith("# VPS1_SCENE_SESSION_SSO_V1 short-maker\n") and
            "auth_request /_vps1_sso_short-maker;" in value)


def verify(cookie: str, *, attempts: int = 16, pause: float = 0.5) -> None:
    """Read back browser 303 and API 401 after graceful Nginx worker drain."""
    guard(1 <= attempts <= 30, "invalid verification attempts")
    # Test one native app + the shared independent uploader authorization route.
    paths = ("/short-maker/", "/local-upload-manager/app")
    for path in paths:
        for iteration in range(attempts):
            status, _, _ = nginx.tls_get(path, accept="text/html")
            if status == 303:
                break
            guard(status == 401 and iteration + 1 < attempts,
                  "anonymous browser did not redirect to Scene login: " + path)
            time.sleep(pause)
        # Same endpoint without an HTML Accept must still be a real 401.
        status, _, _ = nginx.tls_get(path)
        guard(status == 401, "API/no-Accept 401 weakened: " + path)
    status, _, _ = nginx.tls_get("/short-maker/health")
    guard(status == 401, "Short Maker API not protected")
    status, content_type, _ = nginx.tls_get("/short-maker/", cookie=cookie)
    guard(status == 200 and "html" in content_type.lower(),
          "authenticated Short Maker no longer available")
    nginx.check_scene()


def publish(revision: str) -> None:
    """Called only by the existing allowlisted, serialized route-only operation."""
    guard(SITE.is_file() and not SITE.is_symlink() and
          ENABLED.is_symlink() and ENABLED.resolve() == SITE,
          "Nginx site ownership changed")
    guard(_short_maker_sso_ready(), "Short Maker is not Scene-session protected")
    cookie = sso.ready_cookie("short-maker")
    before = SITE.read_text()
    after = apply(before)
    if after == before:
        verify(cookie)
        print("UNIVERSAL_BROWSER_LOGIN=ALREADY_LIVE VERIFIED=YES", flush=True)
        return
    journal = JOURNALS / (revision + "-" + str(time.time_ns()))
    journal.mkdir(parents=True, mode=0o700, exist_ok=False)
    nginx.atomic(journal / "before.conf", before.encode(), 0o600)
    written = False
    try:
        guard(SITE.read_text() == before, "concurrent Nginx site edit")
        nginx.atomic(SITE, after.encode())
        written = True
        nginx.command(["nginx", "-t"])
        nginx.command(["systemctl", "reload", "nginx"])
        verify(cookie)
        guard(SITE.read_text() == after, "site changed during verification")
        receipt = {
            "revision": revision, "status": "ACTIVE",
            "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
            "rollback_path": str(journal / "before.conf"),
            "apps": "Scene + all session-protected Nginx routes",
            "auto_script": "OFFLINE", "app_restart": False,
        }
        nginx.atomic(journal / "release.json",
                     (json.dumps(receipt, sort_keys=True) + "\n").encode(), 0o600)
        print("UNIVERSAL_BROWSER_LOGIN=PASS API_401=PRESERVED USER_DATA=PRESERVED", flush=True)
    except BaseException:
        if written:
            guard(SITE.read_text() == after,
                  "concurrent change blocks automatic Nginx rollback")
            nginx.atomic(SITE, before.encode())
            nginx.command(["nginx", "-t"])
            nginx.command(["systemctl", "reload", "nginx"])
        print("UNIVERSAL_BROWSER_LOGIN=ROLLED_BACK", flush=True)
        raise

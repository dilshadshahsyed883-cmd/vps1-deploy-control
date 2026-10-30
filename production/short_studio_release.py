"""Guarded, reversible Scene HTTPS route for the independent Short Studio uploader.

Reuses the existing Scene session auth_request. No account, queue, OAuth,
scheduler, or original Short Uploader state is copied or modified here.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib import request

from production.route_uploader_scene_https import (
    SITE, ENABLED, ANCHOR, MARKER as LUM_ROUTE_MARKER,
)

MARKER = "# VPS1_SHORT_STUDIO_UPLOADER_ROUTE_V1"
ROUTE = """ # VPS1_SHORT_STUDIO_UPLOADER_ROUTE_V1
 location = /short-uploader { return 308 /short-uploader/; }
 location ^~ /short-uploader/ {
    auth_request /_scene_uploader_auth;
    auth_request_set $scene_lum_user $upstream_http_x_auth_user;
    auth_request_set $scene_lum_access $upstream_http_x_app_access;
    auth_request_set $scene_lum_admin $upstream_http_x_is_admin;
    proxy_pass http://127.0.0.1:8019;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Auth-User $scene_lum_user;
    proxy_set_header X-App-Access $scene_lum_access;
    proxy_set_header X-Is-Admin $scene_lum_admin;
    proxy_set_header Authorization "";
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
 }
"""


def guard(condition, message):
    if not condition:
        raise RuntimeError("Short Studio route BLOCKED: " + message)


def apply_route(site):
    guard(site.count(ANCHOR) == 1 and site.count(LUM_ROUTE_MARKER) == 1,
          "Scene HTTPS / uploader authentication route drift")
    guard("location = /_scene_uploader_auth {" in site and
          "auth_request /_scene_uploader_auth;" in site,
          "trusted Scene uploader auth endpoint missing")
    if MARKER in site:
        guard(site.count(MARKER) == 1 and site.count(ROUTE) == 1,
              "Short Uploader route differs from reviewed source")
        return site
    guard("location = /short-uploader" not in site and
          "location ^~ /short-uploader/" not in site,
          "short uploader URL already claimed")
    return site.replace(ANCHOR, ROUTE + ANCHOR, 1)


def command(*args):
    subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   timeout=25)


def write_site(payload):
    with tempfile.NamedTemporaryFile("w", dir=SITE.parent, prefix=".short-studio-",
                                     delete=False) as handle:
        tmp = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        tmp.chmod(0o644)
        os.replace(tmp, SITE)
    finally:
        tmp.unlink(missing_ok=True)


def verify_local():
    opener = request.build_opener(request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8019/short-uploader/health", timeout=6) as resp:
        health = json.load(resp)
    guard(health.get("ok") is True and
          health.get("engine_instance") == "shortstudio" and
          health.get("base_path") == "/short-uploader" and
          health.get("suite_auth") is True,
          "independent Short Uploader identity not ready")


# Nginx reload returns before old worker processes have drained. An immediate
# anonymous 404 during cutover is not evidence of public access, but it must
# converge to *exactly* 401. Never accept 200/3xx/403 as successful anonymous
# protection, and never grant access to a non-authenticated request.
TRANSIENT_HTTP = frozenset((404, 502, 503))


def verify_https_route(https_get, scene_cookie, *, attempts=12, delay=0.6):
    """Fail-closed bounded HTTPS readback after Nginx graceful reload."""
    guard(1 <= attempts <= 30 and 0 <= delay <= 5,
          "invalid HTTPS convergence bounds")
    latest = None
    for index in range(attempts):
        app_status = https_get("/short-uploader/app")[0]
        health_status = https_get("/short-uploader/health")[0]
        latest = (app_status, health_status)
        guard(all(code == 401 or code in TRANSIENT_HTTP for code in latest),
              f"anonymous Short Uploader access: app={app_status}, health={health_status}")
        if latest == (401, 401):
            break
        if index + 1 < attempts:
            time.sleep(delay)
    else:
        guard(False, f"anonymous Short Uploader auth never converged: app={latest[0]}, health={latest[1]}")

    cookie = scene_cookie()
    for index in range(attempts):
        status, content_type, html = https_get(
            "/short-uploader/app?tab=auto_engine", cookie)
        if (status == 200 and "html" in content_type.lower()
                and b"Short Uploader" in html and b"Auto Engine" in html):
            break
        guard(status in TRANSIENT_HTTP,
              f"authenticated Short Uploader HTML readback failed: status={status}")
        if index + 1 < attempts:
            time.sleep(delay)
    else:
        guard(False, f"authenticated Short Uploader route never converged: status={status}")

    status, _, _ = https_get("/short-uploader")
    guard(status == 308, f"Short Uploader canonical redirect missing: status={status}")
    status, _, _ = https_get("/_scene_uploader_auth", cookie)
    guard(status == 404, "internal uploader auth endpoint exposed")


def install_route(revision):
    """Return the previous content only when this transaction changed Nginx."""
    guard(SITE.is_file() and ENABLED.is_symlink() and ENABLED.resolve() == SITE,
          "managed Scene TLS site unavailable")
    verify_local()
    before = SITE.read_text()
    after = apply_route(before)
    if before == after:
        return None
    tx = Path("/srv/fresh-website/transactions/short-studio-route") / revision
    guard(not tx.exists(), "route transaction already recorded")
    tx.mkdir(parents=True, mode=0o700)
    (tx / "before.conf").write_text(before)
    (tx / "before.conf").chmod(0o600)
    try:
        guard(SITE.read_text() == before, "concurrent Nginx edit")
        write_site(after)
        command("nginx", "-t")
        command("systemctl", "reload", "nginx")
        # Allow Nginx old-worker drain, never relax authentication.
        from production.route_uploader_scene_https import https_get, scene_cookie
        verify_https_route(https_get, scene_cookie)
        (tx / "receipt.json").write_text(json.dumps({
            "revision": revision, "before_sha": hashlib.sha256(before.encode()).hexdigest(),
            "after_sha": hashlib.sha256(after.encode()).hexdigest(),
            "auth": "PASS", "html": "PASS",
        }, sort_keys=True))
        return {"before": before, "after_sha": hashlib.sha256(after.encode()).hexdigest()}
    except BaseException:
        write_site(before)
        command("nginx", "-t")
        command("systemctl", "reload", "nginx")
        raise


def restore_route(receipt):
    """Rollback only our exact new Nginx content (compare-and-swap)."""
    if not receipt:
        return
    current = SITE.read_text()
    guard(hashlib.sha256(current.encode()).hexdigest() == receipt["after_sha"],
          "concurrent Nginx mutation prevents automatic route rollback")
    write_site(receipt["before"])
    command("nginx", "-t")
    command("systemctl", "reload", "nginx")

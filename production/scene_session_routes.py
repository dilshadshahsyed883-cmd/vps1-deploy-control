"""Trusted Scene-session replacement of one existing browser Basic Auth route.

The installed root helper uses the existing allowlisted nginx-route-only operation.
Only reviewed Nginx config is mutated. Stored per-app backup restores legacy
gateway routing after failure or explicit rollback. Never restart applications.
"""
from __future__ import annotations
import json
from pathlib import Path
import time
from urllib import request

from production import gateway
from production.runtime import CATALOG
from production.route_uploader_scene_https import scene_cookie

APP_IDS = frozenset(CATALOG) - {"scene-builder", "local-upload-manager"}
AUTH_PREFIX = "/_vps1_sso_"


def config(app):
    """Copy the proven gateway rewrite contract, replacing Basic with Scene auth."""
    from production import nginx_route_only as old
    old.guard(app in APP_IDS, "unsupported Scene SSO app")
    source = gateway.nginx_config([app], Path("/srv/fresh-website/shell"), gateway.AUTH)
    prefix = "/" + app
    start = source.index("    location = " + prefix + " {")
    stop = source.index("    location / { return 404; }")
    fragment = source[start:stop]
    start_location = "    location ^~ " + prefix + "/ {"
    old.guard(fragment.count(start_location) == 1, "gateway route syntax drift")
    internal = AUTH_PREFIX + app
    check = (
        "# VPS1_SCENE_SESSION_SSO_V1 " + app + "\n"
        "location = " + internal + " {\n"
        "    internal;\n"
        "    proxy_pass http://127.0.0.1:18080/internal/workspace-auth?app=" + app + ";\n"
        "    proxy_pass_request_body off;\n"
        '    proxy_set_header Content-Length "";\n'
        "    proxy_set_header Cookie $http_cookie;\n"
        '    proxy_set_header Authorization "";\n'
        '    proxy_set_header X-Auth-User "";\n'
        '    proxy_set_header X-App-Access "";\n'
        '    proxy_set_header X-Is-Admin "";\n'
        "    proxy_set_header Host $host;\n"
        "}\n"
    )
    addition = (
        "\n        auth_request " + internal + ";"
        "\n        auth_request_set $scene_sso_user $upstream_http_x_auth_user;"
        "\n        auth_request_set $scene_sso_access $upstream_http_x_app_access;"
        "\n        auth_request_set $scene_sso_admin $upstream_http_x_is_admin;"
    )
    fragment = fragment.replace(start_location, start_location + addition, 1)
    for previous, trusted in (
        ("X-Auth-User $remote_user", "X-Auth-User $scene_sso_user"),
        ("X-App-Access both", "X-App-Access $scene_sso_access"),
        ("X-Is-Admin 1", "X-Is-Admin $scene_sso_admin"),
    ):
        needle = "proxy_set_header " + previous + ";"
        old.guard(fragment.count(needle) == 1, "gateway identity contract drift")
        fragment = fragment.replace(needle, "proxy_set_header " + trusted + ";", 1)
    old.guard('proxy_set_header Authorization "";' in fragment and
              "proxy_pass http://127.0.0.1:8888;" not in fragment,
              "Basic Auth still required by app proxy")
    return check + fragment


def ready_cookie(app):
    """Require real admin Scene login and new scoped endpoint before Nginx edit."""
    from production import nginx_route_only as old
    old.guard(app in APP_IDS, "app outside Scene SSO scope")
    cookie = scene_cookie()
    req = request.Request(
        "http://127.0.0.1:18080/internal/workspace-auth?app=" + app,
        headers={"Cookie": cookie, "Connection": "close"})
    opener = request.build_opener(request.ProxyHandler({}))
    with opener.open(req, timeout=8) as response:
        old.guard(response.status == 204 and
                  bool(response.headers.get("X-Auth-User")) and
                  response.headers.get("X-Is-Admin") == "1",
                  "Scene login session SSO endpoint not live")
    return cookie


def verify(app, cookie):
    from production import nginx_route_only as old
    urls = (old.public_path(app, "health"), old.public_path(app, "ui"))
    for url in urls:
        old.require_https_status(url, expected=401, app=app)
        ct, body = old.require_https_status(url, expected=200, app=app, cookie=cookie)
        if url == urls[0]:
            old.guard(json.loads(body).get("ok") is True, "SSO app health invalid: " + app)
        else:
            old.guard("html" in ct.lower() and b"<" in body,
                      "SSO app HTML invalid: " + app)
    code, _, _ = old.tls_get(AUTH_PREFIX + app, cookie=cookie)
    old.guard(code == 404, "internal SSO subrequest publicly visible")


def active(app, site):
    from production import nginx_route_only as old
    if app not in APP_IDS:
        return False
    _, names = old.owned_routes(site)
    return app in names and old.snippet_path(app).read_text() == config(app)


def _legacy(app, site):
    from production import nginx_route_only as old
    base, names = old.owned_routes(site)
    if app in names:
        route = old.snippet_path(app)
        prior = route.read_bytes()
        old.guard(prior.decode() == old.route_config(app),
                  "legacy route drift; cannot safely migrate")
        return site, prior, True
    old.guard(old.standalone(base, app), "existing app route not reviewed")
    if app == "voice-studio":
        fragment = old.VOICE_EXTRA
    elif app == "auto-caption-studio":
        fragment = old.ACS_ROUTE
    else:
        old.guard(False, "unrecognized standalone route")
    old.guard(site.count(fragment) == 1, "standalone route ownership drift")
    without = site.replace(fragment, "", 1)
    new_site, changed = old.add_route(without, app)
    old.guard(changed, "session SSO include generation failed")
    return new_site, None, False


def migrate(revision, app):
    from production import nginx_route_only as old
    old.guard(app in APP_IDS, "SSO app not allowlisted")
    cookie = ready_cookie(app)
    before_site = old.SITE.read_text()
    after_site, previous_route, had_route = _legacy(app, before_site)
    route = old.snippet_path(app)
    new_route = config(app).encode()
    if not had_route:
        old.guard(not route.exists() and not route.is_symlink(),
                  "orphan SSO Nginx snippet")
    tx = old.TRANSACTIONS / (app + "-sso-" + str(time.time_ns()))
    tx.mkdir(mode=0o700, parents=True, exist_ok=False)
    old.atomic(tx / "before.site", before_site.encode(), 0o600)
    if previous_route is not None:
        old.atomic(tx / "before.route", previous_route, 0o600)
    wrote_route = wrote_site = False
    try:
        old.guard(old.SITE.read_text() == before_site, "concurrent site change")
        if had_route:
            old.guard(route.read_bytes() == previous_route, "concurrent app snippet edit")
        old.atomic(route, new_route)
        wrote_route = True
        if after_site != before_site:
            old.atomic(old.SITE, after_site.encode())
            wrote_site = True
        old.command(["nginx", "-t"])
        old.command(["systemctl", "reload", "nginx"])
        verify(app, cookie)
        old.check_scene()
        record = dict(
            app=app, revision=revision, state="ACTIVE", mode="SCENE_SESSION_SSO",
            old_managed=had_route, transaction=str(tx),
            before_site_sha256=old.digest(before_site),
            after_site_sha256=old.digest(after_site),
            before_route_sha256=old.digest(previous_route) if previous_route is not None else None,
            after_route_sha256=old.digest(new_route),
            changed_services=[], owner_uat="PENDING",
        )
        payload = (json.dumps(record, sort_keys=True) + "\n").encode()
        old.atomic(tx / "release.json", payload, 0o600)
        old.atomic(old.TRANSACTIONS / ("sso-" + app + ".json"), payload, 0o600)
        print("SCENE_SESSION_SSO=PASS APP=" + app + " OWNER_UAT=PENDING", flush=True)
    except BaseException:
        if wrote_site:
            if old.SITE.read_text() != after_site:
                raise RuntimeError("BLOCKED: concurrent Nginx site edit during SSO rollback")
            old.atomic(old.SITE, before_site.encode())
        if wrote_route:
            if had_route:
                old.atomic(route, previous_route)
            else:
                route.unlink()
        old.command(["nginx", "-t"])
        old.command(["systemctl", "reload", "nginx"])
        print("SCENE_SESSION_SSO=ROLLED_BACK APP=" + app, flush=True)
        raise


def rollback(revision, app):
    """Explicit rollback restores pre-SSO route without uninstalling an app."""
    from production import nginx_route_only as old
    record_path = old.TRANSACTIONS / ("sso-" + app + ".json")
    old.guard(record_path.is_file() and not record_path.is_symlink(),
              "no Scene SSO record")
    record = json.loads(record_path.read_text())
    old.guard(record.get("app") == app and record.get("state") == "ACTIVE" and
              record.get("mode") == "SCENE_SESSION_SSO", "not an active SSO route")
    tx = old.TRANSACTIONS / Path(record["transaction"]).name
    old.guard(str(tx) == record["transaction"] and tx.is_dir(),
              "untrusted Scene SSO transaction")
    route = old.snippet_path(app)
    old.guard(route.is_file() and not route.is_symlink() and
              old.digest(route.read_bytes()) == record["after_route_sha256"],
              "SSO route was modified after publication")
    site_bytes = old.SITE.read_bytes()
    _, names = old.owned_routes(site_bytes.decode())
    old.guard(app in names, "SSO include missing from reviewed site")
    managed = bool(record["old_managed"])
    if managed:
        prior_route = (tx / "before.route").read_bytes()
        old.guard(old.digest(prior_route) == record["before_route_sha256"],
                  "legacy app route backup corrupted")
    else:
        old.guard(old.digest(site_bytes) == record["after_site_sha256"],
                  "standalone site changed since SSO cutover")
        prior_site = (tx / "before.site").read_bytes()
        old.guard(old.digest(prior_site) == record["before_site_sha256"],
                  "legacy Nginx site backup corrupted")
    try:
        if managed:
            old.atomic(route, prior_route)
        else:
            old.atomic(old.SITE, prior_site)
            route.unlink()
        old.command(["nginx", "-t"])
        old.command(["systemctl", "reload", "nginx"])
        record["state"] = "ROLLED_BACK"
        record["rollback_revision"] = revision
        old.atomic(record_path, (json.dumps(record, sort_keys=True) + "\n").encode(), 0o600)
        print("SCENE_SESSION_SSO_ROLLBACK=PASS APP=" + app, flush=True)
    except BaseException:
        old.atomic(route, config(app).encode())
        if not managed:
            old.atomic(old.SITE, site_bytes)
        old.command(["nginx", "-t"])
        old.command(["systemctl", "reload", "nginx"])
        raise

"""Guarded non-destructive authenticated Chrome smoke for Experiment Studio.

Runs only during Scene candidate install, after the new source is active but
before release commit. Creates a temporary blank browser profile, opens a real
private Chrome pipe, reads a screenshot, then stops and deletes the probe.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

ROOT = "http://127.0.0.1:18080"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _request(path: str, cookie: str, *, method="GET", csrf="", payload=None):
    body = json.dumps(payload if payload is not None else {}).encode() \
        if method != "GET" else None
    headers = {"Cookie": cookie, "Accept": "application/json",
               "Connection": "close"}
    if method != "GET":
        headers.update({"Content-Type": "application/json",
                        "X-Experiment-CSRF": csrf})
    request = urllib.request.Request(ROOT + path, data=body, method=method,
                                     headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with opener.open(request, timeout=42) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read(250000), response.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read(3500), ""


def verify():
    from production.route_uploader_scene_https import scene_cookie
    cookie = scene_cookie()
    status, kind, html, new_cookie = _request("/experiment-studio/antidetect-browser/", cookie)
    if status != 200 or "html" not in kind.lower() or b'id="experiment-browser"' not in html:
        raise RuntimeError("EXPERIMENT_BROWSER_BLOCKED: authenticated page missing")
    match = re.search(rb'data-csrf="([A-Za-z0-9_-]{20,})"', html)
    if not match:
        raise RuntimeError("EXPERIMENT_BROWSER_BLOCKED: session CSRF missing")
    csrf = match.group(1).decode("ascii")
    if new_cookie.startswith("session="):
        cookie = new_cookie.split(";", 1)[0]
    else:
        raise RuntimeError("EXPERIMENT_BROWSER_BLOCKED: CSRF session cookie not updated")
    status, kind, body, _ = _request("/api/experiment-browser/health", cookie)
    if status != 200 or not json.loads(body).get("chrome_installed"):
        raise RuntimeError("EXPERIMENT_BROWSER_BLOCKED: VPS Chrome binary unavailable")
    status, _, _, _ = _request("/api/experiment-browser/profiles", "")
    if status not in (303, 401):
        raise RuntimeError("EXPERIMENT_BROWSER_BLOCKED: anonymous profile API accessible")
    status, _, body, _ = _request("/api/experiment-browser/profiles", cookie,
                               method="POST", csrf=csrf, payload={"name": "vps1-engine-smoke"})
    if status != 200:
        raise RuntimeError("EXPERIMENT_BROWSER_BLOCKED: profile creation HTTP " + str(status))
    identity = json.loads(body)["profile"]["id"]
    base = "/api/experiment-browser/profiles/" + identity
    started = False
    try:
        status, _, body, _ = _request(base + "/start", cookie,
                                   method="POST", csrf=csrf)
        if status != 200 or json.loads(body).get("profile", {}).get("state") != "running":
            raise RuntimeError("EXPERIMENT_BROWSER_BLOCKED: Chrome start HTTP " + str(status))
        started = True
        status, content_type, image, _ = _request(base + "/screenshot", cookie)
        if status != 200 or "image/png" not in content_type or \
           not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("EXPERIMENT_BROWSER_BLOCKED: real Chrome screenshot missing")
    finally:
        if started:
            _request(base + "/stop", cookie, method="POST", csrf=csrf)
        _request(base, cookie, method="DELETE", csrf=csrf)
    print("EXPERIMENT_BROWSER_LIVE_SMOKE=PASS PROFILE=ISOLATED CHROME=REAL CDP=PRIVATE", flush=True)

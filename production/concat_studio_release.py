"""Deployable, reversible trusted HTTPS mapping for Concat Uploader.

SOURCE ONLY. This module does not modify Nginx, launch services or migrate
production state. A future guarded release must provision/verify the dedicated
8021 loopback runtime and its Scene-owned rclone access first.
"""
from __future__ import annotations

import re

from production.route_uploader_scene_https import ANCHOR

MARKER = "# VPS1_CONCAT_STUDIO_UPLOADER_ROUTE_V1"
SERVICE_PORT = 8021
SERVICE_ID = "concat-uploader"
BASE_PATH = "/concat-studio/concat-uploader"
DATA_ROOT = "/var/lib/fresh-website/concat-uploader"
CLOUD_ROOT = "/var/lib/fresh-website/scene-builder/cloud-render"

SERVICE_CONFIG = {
    "engine": "lum_engine.standalone_app:app",
    "source": "apps/copyright-uploader/part01",
    "instance": "concat",
    "listen_host": "127.0.0.1",
    "port": SERVICE_PORT,
    "base_path": BASE_PATH,
    "data_root": DATA_ROOT,
    "cloud_render_root": CLOUD_ROOT,
    "suite_auth": True,
    "session_cookie_secure": True,
}

# Only the internal subrequest supplies owner identity and access. An outside
# request's X-Auth-User, X-App-Access or X-Is-Admin is always overwritten.
ROUTE = """ # VPS1_CONCAT_STUDIO_UPLOADER_ROUTE_V1
 location = /_vps1_sso_concat_uploader {
    internal;
    proxy_pass http://127.0.0.1:18080/internal/workspace-auth?app=concat-uploader;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header Cookie $http_cookie;
    proxy_set_header Authorization "";
    proxy_set_header X-Auth-User "";
    proxy_set_header X-App-Access "";
    proxy_set_header X-Is-Admin "";
    proxy_set_header Host $host;
 }
 location = /concat-studio/concat-uploader { return 308 /concat-studio/concat-uploader/; }
 location ^~ /concat-studio/concat-uploader/ {
    auth_request /_vps1_sso_concat_uploader;
    auth_request_set $concat_scene_user $upstream_http_x_auth_user;
    auth_request_set $concat_scene_access $upstream_http_x_app_access;
    auth_request_set $concat_scene_admin $upstream_http_x_is_admin;
    proxy_pass http://127.0.0.1:8021;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Auth-User $concat_scene_user;
    proxy_set_header X-App-Access $concat_scene_access;
    proxy_set_header X-Is-Admin $concat_scene_admin;
    proxy_set_header Authorization "";
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    client_max_body_size 12288m;
    client_body_timeout 7200s;
    proxy_request_buffering off;
    proxy_read_timeout 7200s;
    proxy_send_timeout 7200s;
 }
"""


def guarded_route(site: str) -> str:
    """Prepare the exact route while preserving every other Nginx location."""
    if site.count(ANCHOR) != 1 or site.count("listen 443 ssl;") != 1:
        raise RuntimeError("Scene HTTPS anchor drift; Concat route not applied")
    if MARKER in site:
        if site.count(MARKER) == 1 and site.count(ROUTE) == 1:
            return site
        raise RuntimeError("Existing Concat route differs from reviewed source")
    if (re.search(r"location\s+(?:=|\^~)\s+/concat-studio/concat-uploader(?:/|\s)", site)
            or "/_vps1_sso_concat_uploader" in site):
        raise RuntimeError("Concat route is already controlled by another config")
    return site.replace(ANCHOR, ROUTE + ANCHOR, 1)


def rollback_route(current: str) -> str:
    """Exact-block-only rollback. Never overwrite concurrent site changes."""
    if current.count(ROUTE) != 1 or current.count(MARKER) != 1:
        raise RuntimeError("Cannot safely rollback a changed Concat route")
    return current.replace(ROUTE, "", 1)


def reviewed_service_environment() -> dict[str, str]:
    """Non-secret settings only. Generate real secrets at guarded deploy time."""
    return {
        "LUM_ENGINE_INSTANCE": "concat",
        "LUM_HOST": "127.0.0.1",
        "LUM_PORT": str(SERVICE_PORT),
        "LUM_BASE_PATH": BASE_PATH,
        "LUM_DATA_DIR": DATA_ROOT,
        "LUM_SUITE_AUTH": "1",
        "LUM_COOKIE_SECURE": "1",
        "LUM_SUITE_LOGIN_URL": "/login",
        "CONCAT_CLOUD_RENDER_ROOT": CLOUD_ROOT,
        "CONCAT_PUBLIC_ORIGIN": "https://app.scenebuilder.online",
    }

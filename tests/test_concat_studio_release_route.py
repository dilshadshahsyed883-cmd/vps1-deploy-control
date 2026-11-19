"""Nginx source-only Concat route authentication and rollback tests."""
from __future__ import annotations

import unittest
from pathlib import Path
from production.concat_studio_release import (
    BASE_PATH, DATA_ROOT, MARKER, ROUTE, SERVICE_CONFIG,
    guarded_route, rollback_route, reviewed_service_environment,
)
from production.route_uploader_scene_https import ANCHOR

MINIMAL_SITE = """server {
 listen 443 ssl;
 server_name app.scenebuilder.online;
""" + ANCHOR + """    return 404;
 }
}
"""


class ConcatStudioRouteTests(unittest.TestCase):
    def test_trusted_sso_headers_and_dedicated_runtime(self):
        rendered = guarded_route(MINIMAL_SITE)
        self.assertEqual(rendered.count(MARKER), 1)
        self.assertIn("auth_request /_vps1_sso_concat_uploader;", rendered)
        self.assertIn("internal/workspace-auth?app=concat-uploader", rendered)
        self.assertIn("proxy_set_header X-Auth-User $concat_scene_user;", rendered)
        self.assertIn("proxy_set_header X-Is-Admin $concat_scene_admin;", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:8021;", rendered)
        self.assertIn("proxy_set_header Authorization \"\";", rendered)
        self.assertNotIn("proxy_pass http://0.0.0.0:8021", rendered)
        self.assertEqual(guarded_route(rendered), rendered)
        self.assertEqual(rollback_route(rendered), MINIMAL_SITE)

    def test_route_collision_and_unsafe_replacements_denied(self):
        with self.assertRaises(RuntimeError):
            guarded_route("server { listen 443 ssl; }")
        with self.assertRaises(RuntimeError):
            guarded_route(MINIMAL_SITE.replace(ANCHOR, " location = /concat-studio/concat-uploader { return 200; }\n" + ANCHOR))
        with self.assertRaises(RuntimeError):
            guarded_route(MINIMAL_SITE.replace(ANCHOR, " location = /_vps1_sso_concat_uploader { return 200; }\n" + ANCHOR))
        with self.assertRaises(RuntimeError):
            rollback_route(MINIMAL_SITE)
        with self.assertRaises(RuntimeError):
            rollback_route(guarded_route(MINIMAL_SITE).replace("8021", "9999"))

    def test_reused_uploader_is_owner_isolated_and_source_only(self):
        env = reviewed_service_environment()
        self.assertEqual(env["LUM_ENGINE_INSTANCE"], "concat")
        self.assertEqual(env["LUM_SUITE_AUTH"], "1")
        self.assertEqual(env["LUM_BASE_PATH"], BASE_PATH)
        self.assertEqual(env["LUM_DATA_DIR"], DATA_ROOT)
        self.assertEqual(env["CONCAT_CLOUD_RENDER_ROOT"], SERVICE_CONFIG["cloud_render_root"])
        self.assertEqual(SERVICE_CONFIG["listen_host"], "127.0.0.1")
        self.assertNotIn("LUM_SESSION_SECRET", env)
        # This module merely emits immutable candidate routing/config source.
        self.assertFalse(any(word in Path(__file__).parents[1].joinpath(
            "production/concat_studio_release.py").read_text()
            for word in ("systemctl reload", "nginx -s reload")))


if __name__ == "__main__":
    unittest.main()

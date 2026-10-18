"""Source-controlled Auto Caption route, no existing services/queues touched."""
import ast
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode=True
from production import route_auto_caption_scene_https as route
from production import scene_public_https as scene

ROOT=Path(__file__).resolve().parents[2]

class AutoCaptionSceneRoute(unittest.TestCase):
    def test_https_location_preserves_original_scene_config(self):
        before=scene.nginx_https()
        after=route.add_route(before)
        self.assertIn("location ^~ /auto-caption-studio/",after)
        self.assertIn("location = /auto-caption-studio { return 308 /auto-caption-studio/; }",after)
        self.assertIn("proxy_pass http://127.0.0.1:8888;",after)
        self.assertNotIn("proxy_pass http://127.0.0.1:8144;",after)
        self.assertIn("proxy_set_header Authorization $http_authorization;",after)
        self.assertIn("proxy_set_header Host $host;",after)
        self.assertIn("location / {\n    proxy_pass http://127.0.0.1:18080;",after)
        self.assertIn("location = /internal/runtime-ready { return 404; }",after)
        self.assertIn("ssl_certificate /etc/letsencrypt/live/app.scenebuilder.online/fullchain.pem;",after)
        self.assertEqual(after.replace(route.ROUTE,""),before)
        self.assertEqual(after.count("listen 443 ssl;"),1)
        self.assertEqual(after.count("server_name app.scenebuilder.online;"),2)
        self.assertNotIn("default_server",after)

    def test_non_owner_routes_and_double_injection_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError,"already routed"):
            route.add_route(route.add_route(scene.nginx_https()))
        with self.assertRaisesRegex(RuntimeError,"differs"):
            route.add_route("server { listen 443 ssl; location / { } }")
        with self.assertRaisesRegex(RuntimeError,"unowned"):
            route.add_route(scene.nginx_https().replace(
                " location / {", " location ^~ /auto-caption-studio/ { return 403; }\n location / {"))

    def test_installer_is_app_route_only(self):
        code=(ROOT/"production/route_auto_caption_scene_https.py").read_text()
        ast.parse(code)
        for needed in ("site.before.conf","release.json","ROLLBACK=SCENE_HTTPS_SITE_ONLY",
                       "master-drive-agent.service","vps1-production-auto-caption-studio.service",
                       "vps1-production-acs-","unauthenticated public Auto Caption route exposed",
                       "existing gateway auth record","5_000_000_000"):
            self.assertIn(needed,code)
        self.assertNotIn("systemctl restart",code)
        self.assertNotIn("AUTO_SCRIPT_TOUCHED=YES",code)
        groups=__import__("json").loads((ROOT/"rebuild/groups.json").read_text())
        self.assertFalse(groups["production_deploy_enabled"])
        self.assertEqual(groups["offline_applications"],["auto-script"])

if __name__=="__main__":
    unittest.main()

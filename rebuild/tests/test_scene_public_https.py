"""Offline Scene domain cutover gates. No VPS, DNS change, cert issuance or workers."""
import ast
import json
from pathlib import Path
import unittest
from production import scene_public_https as https

ROOT=Path(__file__).resolve().parents[2]

class DomainCutoverContract(unittest.TestCase):
    def test_nginx_canary_and_https_isolation(self):
        http=https.nginx_http_only()
        tls=https.nginx_https()
        for expected in ("server_name app.scenebuilder.online;",
                         "location ^~ /.well-known/acme-challenge/",
                         "root /var/www/vps1-scene-acme;"):
            self.assertIn(expected,http)
            self.assertIn(expected,tls)
        self.assertIn("return 503;",http)
        self.assertNotIn("listen 443",http)
        self.assertIn("listen 443 ssl;",tls)
        self.assertIn("ssl_certificate /etc/letsencrypt/live/app.scenebuilder.online/fullchain.pem;",tls)
        self.assertIn("proxy_pass http://127.0.0.1:18080;",tls)
        self.assertIn("location = /internal/runtime-ready { return 404; }",tls)
        self.assertNotIn("default_server",tls)
        self.assertNotIn("sites-enabled/vps1-production.conf",tls)
        self.assertIn("return 308 https://$host$request_uri;",tls)

    def test_syntax_and_immutable_host_gates(self):
        for f in ("production/scene_public_https.py","production/scene_login_upgrade.py"):
            text=(ROOT/f).read_text()
            ast.parse(text)
            self.assertIn("vps-01a23801",text)
            self.assertNotIn("SSH_PRIVATE_KEY",text)
            self.assertIn("5_000_000_000",text)
        upgrade=(ROOT/"production/scene_login_upgrade.py").read_text()
        self.assertIn("SQLITE_BACKUPS=",upgrade)
        self.assertIn("VPS1_SCENE_PRECUTOVER_EDITING=1",upgrade)
        self.assertIn("PREVIOUS_SCENE_RELEASE_AND_ENV_RESTORED",upgrade)
        self.assertNotIn("Auto Script",upgrade)

    def test_repo_flags_still_off_and_source_guards(self):
        data=json.loads((ROOT/"rebuild/groups.json").read_text())
        self.assertFalse(data["production_deploy_enabled"])
        self.assertFalse(data["fresh_install_enabled"])
        self.assertEqual(data["offline_applications"],["auto-script"])
        text=(ROOT/"apps/scene-builder/part01/precutover_policy.py").read_text()
        self.assertIn("mutation_allowed",text)

if __name__=="__main__": unittest.main()

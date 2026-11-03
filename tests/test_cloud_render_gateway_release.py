"""Cloud Render app-only Nginx readback: no publication or gateway bypass."""
from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production import gateway, nginx_route_only, route_uploader_scene_https, scene_public_https

REVISION = "a"*40

class CloudRenderGatewayProof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.site = self.root/"vps1-scene-public.conf"
        self.site.write_text(scene_public_https.nginx_https())
        record = self.root/"control/state/scene-candidate.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"status":"ACTIVE","revision":REVISION}))
        self.patches = [
            patch.object(gateway,"BASE",self.root),
            patch.object(scene_public_https,"SITE",self.site),
            patch.object(gateway,"run",return_value=""),
            patch.object(nginx_route_only,"check_scene"),
            patch.object(route_uploader_scene_https,"scene_cookie",return_value="session=fixture"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def response(path, *, cookie="", accept=""):
        if not cookie:
            return (303,"text/html",b"")
        if path=="/cloud-render/":
            return (200,"text/html",b"<h2>Cloud Render</h2><nav>Media GPU Config</nav>")
        if path=="/api/cloud-render/media":
            return (200,"application/json",b'{"items":[]}')
        if path=="/api/cloud-render/config":
            return (200,"application/json",b'{"image_ready":false,"source_lock":"cloud_render/concat/source.lock.json"}')
        raise AssertionError("Unexpected HTTPS probe: "+path)

    def test_installed_admin_html_api_and_anonymous_denial(self):
        with patch.object(nginx_route_only,"tls_get",side_effect=self.response) as spy:
            result=gateway.verify_cloud_render_scene_only_https(REVISION)
        self.assertEqual(result["scene"],"ADMIN_HTML_API_VERIFIED")
        self.assertEqual(result["mode"],"NO_NGINX_MUTATION")
        self.assertEqual(spy.call_count,5)
        self.assertEqual(self.site.read_text(),scene_public_https.nginx_https())

    def test_rollback_old_revision_requires_denial_but_not_cloud_html(self):
        with patch.object(nginx_route_only,"tls_get",side_effect=self.response) as spy:
            result=gateway.verify_cloud_render_scene_only_https("b"*40)
        self.assertEqual(result["scene"],"PRIOR_SCENE_REVISION_SAFE_ROLLBACK")
        self.assertEqual(spy.call_count,2)

    def test_denies_anonymous_media_exposure(self):
        def unsafe(path,*,cookie="",accept=""):
            if path=="/api/cloud-render/media" and not cookie:
                return (200,"application/json",b'{"items":[]}')
            return self.response(path,cookie=cookie,accept=accept)
        with patch.object(nginx_route_only,"tls_get",side_effect=unsafe):
            with self.assertRaisesRegex(RuntimeError,"anonymous API exposed"):
                gateway.verify_cloud_render_scene_only_https(REVISION)

    def test_refuses_conflicting_nginx_cloud_prefix(self):
        self.site.write_text(self.site.read_text().replace(" location / {\n",
                            " location ^~ /cloud-render/ { return 200; }\n location / {\n",1))
        with patch.object(nginx_route_only,"tls_get",side_effect=self.response):
            with self.assertRaisesRegex(RuntimeError,"shadows Scene fallback"):
                gateway.verify_cloud_render_scene_only_https(REVISION)

    def test_release_marker_is_exact_and_requires_installed_scene(self):
        source=(Path(__file__).resolve().parents[1]/"production/gateway.py").read_text()
        self.assertIn("[vps1-cloud-render-scene-only]",source)
        self.assertIn("[vps1-experiment-management-release]",source)
        self.assertNotIn("'scene-builder' not in args.apps",source)
        self.assertIn('state.get("revision") == revision',source)
        self.assertIn('state.get("status") != "ACTIVE"',source)
        guard=source.split("if '[vps1-cloud-render-scene-only]' in intent:",1)[1].split("return",1)[0]
        self.assertIn("verify_cloud_render_scene_only_https(args.revision)",guard)
        self.assertNotIn("install(selected)",guard)

if __name__=="__main__": unittest.main()

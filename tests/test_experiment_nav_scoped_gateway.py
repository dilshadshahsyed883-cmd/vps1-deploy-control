"""Regression tests for the exact, non-mutating Experiment navigation gateway lane."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production import gateway


class Response(io.BytesIO):
    status = 200


class ExperimentNavigationGatewayTests(unittest.TestCase):
    def test_exact_lane_precedes_legacy_gateway_publisher(self):
        source = Path(gateway.__file__).read_text(encoding="utf-8")
        guard = "if '[vps1-experiment-library-nav-release]' in intent:"
        self.assertIn(guard, source)
        self.assertLess(source.index(guard), source.index("selected = public_apps_for_revision"))
        segment = source.split(guard, 1)[1].split("# The Three-Studio transaction", 1)[0]
        self.assertIn("verify_experiment_nav_no_gateway", segment)
        self.assertIn("gateway='NO_MUTATION'", segment)
        self.assertIn("return", segment)
        self.assertNotIn("install(selected)", segment)
        self.assertNotIn("publish_voice_on_scene_https", segment)

    def fixture(self, directory, js=None, revision="a" * 40):
        root = Path(directory)
        record = root / "control/state/scene-candidate.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"status": "ACTIVE", "revision": revision}))
        site = root / "scene.conf"
        site.write_text("server {\nlocation / {\n    proxy_pass http://127.0.0.1:18080;\n}\n}\n")
        valid_js = (
            'const EXPERIMENT_NAV = Object.freeze([\n'
            '["Antidetect Browser","/experiment-studio/antidetect-browser/","antidetect"],\n'
            '["Upload Studio","/local-upload-manager/app","upload"],\n'
            '["Cloud Render","/cloud-render/","cloud-render"]\n'
            ']);'
        )
        return root, site, js if js is not None else valid_js

    def verify(self, root, site, js, revision="a" * 40):
        def open_fake(url, timeout=15):
            if url.endswith("/health"):
                return Response(b'{"ok": true}')
            if url.endswith("/api/health"):
                return Response(b'{"ok": true}')
            if url.endswith("vps1_native_site_shell.js"):
                return Response(js.encode())
            raise AssertionError("unexpected network request")
        with patch.object(gateway, "BASE", root), \
             patch("production.scene_public_https.SITE", site), \
             patch.object(gateway, "run") as cmd, \
             patch.object(gateway.urllib.request, "urlopen", side_effect=open_fake):
            proof = gateway.verify_experiment_nav_no_gateway(revision)
            cmd.assert_called_once_with(["nginx", "-t"], quiet=True)
            return proof

    def test_live_shared_nav_is_read_only_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root, site, js = self.fixture(directory)
            original = site.read_bytes()
            proof = self.verify(root, site, js)
            self.assertEqual(proof["mode"], "NO_NGINX_MUTATION")
            self.assertEqual(proof["scene"], "SCOPED_NAVIGATION_VERIFIED")
            self.assertEqual(original, site.read_bytes())

    def test_hidden_menu_regression_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, site, js = self.fixture(directory)
            js = js.replace('["Cloud Render",', '["Video Studio","/scene-builder/","video"],\n["Cloud Render",')
            with self.assertRaisesRegex(RuntimeError, "hidden tab reappeared"):
                self.verify(root, site, js)

    def test_rollback_accepts_previous_safe_scene_without_claiming_new_nav(self):
        with tempfile.TemporaryDirectory() as directory:
            root, site, js = self.fixture(directory, js="old shell", revision="b" * 40)
            proof = self.verify(root, site, js)
            self.assertEqual(proof["scene"], "SAFE_PREVIOUS_SCENE_ROLLBACK")

    def test_https_fallback_drift_is_rejected_before_any_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root, site, js = self.fixture(directory)
            site.write_text("server { location / { proxy_pass http://127.0.0.1:9999; }}")
            with self.assertRaisesRegex(RuntimeError, "fallback changed"):
                self.verify(root, site, js)


if __name__ == "__main__":
    unittest.main()

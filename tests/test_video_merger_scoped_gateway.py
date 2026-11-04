"""Read-only Video Merger gateway release regression tests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from production import gateway


class VideoMergerScopedGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.revision = "a" * 40
        self.site = self.root / "legacy.conf"
        self.site.write_text("legacy gateway unmodified")
        self.scene = self.root / "scene.conf"
        self.scene.write_text("Scene SSO with unrelated new approved routes")
        self.tx = self.root / "transactions/video-merger-imports" / self.revision
        self.tx.mkdir(parents=True)
        (self.tx / "result.json").write_text(json.dumps({"status": "ACTIVE", "drive": "PASS"}))
        target = self.root / "releases/video-merger" / (self.revision + "-imports")
        target.mkdir(parents=True)
        cur = self.root / "current/video-merger"
        cur.parent.mkdir(parents=True)
        cur.symlink_to(target, target_is_directory=True)
        self.patches = [
            patch.object(gateway, "BASE", self.root),
            patch.object(gateway, "SITE", self.site),
            patch("production.scene_public_https.SITE", self.scene),
            patch.object(gateway, "http_check"),
            patch.object(gateway, "run"),
            patch.object(gateway, "auth_credentials", return_value={"username": "test", "password": "test"}),
            patch.object(gateway, "verify_gateway"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_valid_release_checks_existing_route_without_config_writes(self):
        before = (self.site.read_bytes(), self.scene.read_bytes())
        self.assertEqual(gateway.verify_video_merger_existing_gateway(self.revision),
                         "EXISTING_AUTHENTICATED_ROUTE_VERIFIED")
        self.assertEqual((self.site.read_bytes(), self.scene.read_bytes()), before)
        gateway.verify_gateway.assert_called_once_with(["video-merger"], {"username": "test", "password": "test"})
        gateway.run.assert_called_once_with(["nginx", "-t"], quiet=True)

    def test_refuse_unverified_release(self):
        (self.tx / "result.json").write_text(json.dumps({"status": "FAILED", "drive": "PASS"}))
        with self.assertRaisesRegex(RuntimeError, "verified imports transaction missing"):
            gateway.verify_video_merger_existing_gateway(self.revision)

    def test_refuse_gateway_drift_during_probe(self):
        def changed(*_args, **_kwargs):
            self.scene.write_text("unexpected concurrent Scene change")
        gateway.verify_gateway.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, "configuration changed"):
            gateway.verify_video_merger_existing_gateway(self.revision)


if __name__ == "__main__":
    unittest.main()

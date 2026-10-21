"""Offline tests of the authenticated SML Editor API consumer contract."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sml_scene_bridge", ROOT / "apps/scene-builder/part01/sml_scene_bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def clip(id="CLP-ABCD1234", keyword="snow mountain"):
    return {
        "id": id, "type": "video",
        "content_url": f"/api/v1/media/video/{id}/content",
        "description": "An aerial mountain view",
        "keywords": [keyword], "quality": 88, "duration": 3.2,
    }


class SMLBridgeTests(unittest.TestCase):
    def test_approved_only_via_editor_contract(self):
        rows = [clip(), clip(id="CLP-BAD1"), clip(id="CLP-BAD2")]
        rows[1]["type"] = "image"
        rows[2]["content_url"] = "https://untrusted.example/media.mp4"
        with patch.object(bridge, "_request", return_value={"items": rows}) as remote:
            result = bridge.approved_clips()
        self.assertEqual([x["id"] for x in result], ["CLP-ABCD1234"])
        self.assertEqual(remote.call_args.kwargs["params"]["media_type"], "video")

    def test_match_is_keyword_focused(self):
        scene = {"keyword_used": "snow mountain", "final_start": 0, "final_end": 3}
        unrelated = clip(id="CLP-OTHER", keyword="office")
        unrelated["description"] = "Desk office document"
        matches = bridge.rank_clips(scene, [clip(), unrelated])
        self.assertEqual([m["id"] for m in matches], ["CLP-ABCD1234"])

    def test_no_match_preserves_stock_fallback(self):
        self.assertEqual(bridge.rank_clips({"original_query": "underwater coral"},
                                           [clip()]), [])

    def test_no_duplicate_clip_across_scenes(self):
        self.assertEqual(bridge.rank_clips({"keyword_used": "mountain"},
                                           [clip()], {"CLP-ABCD1234"}), [])

    def test_invalid_paths_rejected(self):
        row = clip()
        row["content_url"] = "//evil.test/media"
        self.assertFalse(bridge.eligible(row))
        row = clip()
        row["type"] = "image"
        self.assertFalse(bridge.eligible(row))

    def test_service_origin_rejects_embedded_password(self):
        with patch.dict(os.environ, {"SCENE_SML_URL": "http://user:password@127.0.0.1:8147"}):
            with self.assertRaises(bridge.SMLUnavailable):
                bridge.library_url()

    def test_missing_editor_token_fails_closed(self):
        with patch.dict(os.environ, {"SCENE_SML_EDITOR_TOKEN": ""}):
            with self.assertRaises(bridge.SMLUnavailable):
                bridge.editor_token()

    def test_verified_video_download(self):
        data = b"sample media bytes"
        row = clip()
        row["sha256"] = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SCENE_SML_EDITOR_TOKEN": "test-value"}
        ), patch.object(bridge.urllib.request, "urlopen", return_value=io.BytesIO(data)):
            dest = Path(directory) / "video.mp4"
            self.assertEqual(bridge.download_verified(row, dest), dest)
            self.assertEqual(dest.read_bytes(), data)

    def test_bad_checksum_never_exposes_partial(self):
        row = clip()
        row["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SCENE_SML_EDITOR_TOKEN": "test-value"}
        ), patch.object(bridge.urllib.request, "urlopen", return_value=io.BytesIO(b"mismatch")):
            dest = Path(directory) / "video.mp4"
            with self.assertRaises(bridge.SMLUnavailable):
                bridge.download_verified(row, dest)
            self.assertFalse(dest.exists())
            self.assertFalse(Path(str(dest) + ".partial").exists())

    def test_exact_reservation_and_commit(self):
        item = {"id": "CLP-ABCD1234", "reservation_id": "RES-" + "a" * 32}
        with patch.object(bridge, "_request", return_value={"items": [item]}) as remote:
            result = bridge.reserve_for_scene("JOB-1", "SCENE-1", "", "CLP-ABCD1234")
        self.assertEqual(result["id"], "CLP-ABCD1234")
        self.assertEqual(remote.call_args.kwargs["body"]["preferred_ids"], ["CLP-ABCD1234"])
        with patch.object(bridge, "_request", return_value={"ok": True}) as remote:
            bridge.transition_reservation(item["reservation_id"], "commit")
        self.assertIn("/commit", remote.call_args.args[0])

    def test_source_contract_wiring(self):
        app = (ROOT / "apps/scene-builder/part01/app.py").read_text(encoding="utf-8")
        audit = (ROOT / "apps/scene-builder/part01/templates/audit_media.html").read_text(encoding="utf-8")
        sml = (ROOT / "apps/self-media-library/part01/media_extensions.py").read_text(encoding="utf-8")
        self.assertIn("sml-auto-match", app)
        self.assertIn("sml-select", app)
        self.assertIn("Browse SML", audit)
        self.assertIn("autoMatchSml()", audit)
        self.assertIn("preferred_ids", sml)
        self.assertNotIn("DELETE /api/clips", app)


if __name__ == "__main__":
    unittest.main()

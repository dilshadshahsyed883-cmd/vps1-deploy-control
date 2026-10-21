"""Offline, stdlib-only regression checks for Scene Builder's SML-first bridge."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "apps/scene-builder/part01/sml_scene_bridge.py"
SPEC = importlib.util.spec_from_file_location("sml_scene_bridge", FILE)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def clip(id="CLP-ABCD1234", keyword="snow mountain", status="approved"):
    return {
        "id": id, "status": status, "deleted_at": None,
        "duplicate_state": "clear", "clip_url": f"/media/clips/{id}.mp4",
        "description": "An aerial mountain view", "keywords": [{"keyword": keyword}],
        "quality": 88, "duration": 3.2, "source_orientation": "landscape",
    }


class SMLBridgeTests(unittest.TestCase):
    def test_approved_only(self):
        rows = [clip(), clip(id="CLP-BAD1", status="rejected"),
                clip(id="CLP-BAD2"), clip(id="CLP-BAD3")]
        rows[2]["deleted_at"] = "2026-10-21"
        rows[3]["duplicate_state"] = "duplicate"
        with patch.object(bridge, "_get", return_value=json.dumps({"items": rows}).encode()):
            result = bridge.approved_clips()
        self.assertEqual([x["id"] for x in result], ["CLP-ABCD1234"])

    def test_match_is_keyword_focused(self):
        scene = {"keyword_used": "snow mountain", "final_start": 0, "final_end": 3}
        matches = bridge.rank_clips(scene, [clip(), clip(id="CLP-OTHER", keyword="office")])
        self.assertEqual([m["id"] for m in matches], ["CLP-ABCD1234"])

    def test_no_match_preserves_stock_fallback(self):
        self.assertEqual(bridge.rank_clips({"original_query": "underwater coral"},
                                           [clip()]), [])

    def test_explicit_selection_not_reused(self):
        self.assertEqual(bridge.rank_clips({"keyword_used": "mountain"}, [clip()],
                                           {"CLP-ABCD1234"}), [])

    def test_invalid_path_and_unapproved_rejected(self):
        row = clip()
        row["clip_url"] = "//evil.test/mp4"
        self.assertFalse(bridge.eligible(row))
        row = clip(status="candidate")
        self.assertFalse(bridge.eligible(row))

    def test_no_untrusted_service_origin(self):
        with patch.dict(os.environ, {"SCENE_SML_URL": "http://user:password@127.0.0.1:8147"}, clear=False):
            with self.assertRaises(bridge.SMLUnavailable):
                bridge.library_url()

    def test_integrity_success(self):
        content = b"sample video bytes"
        row = clip()
        row["sha256"] = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            bridge.urllib.request, "urlopen", return_value=io.BytesIO(content)
        ):
            dest = Path(directory) / "sample.mp4"
            self.assertEqual(bridge.download_verified(row, dest), dest)
            self.assertEqual(dest.read_bytes(), content)

    def test_integrity_failure_cleans_partial(self):
        row = clip()
        row["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory, patch.object(
            bridge.urllib.request, "urlopen", return_value=io.BytesIO(b"not-the-hash")
        ):
            dest = Path(directory) / "sample.mp4"
            with self.assertRaises(bridge.SMLUnavailable):
                bridge.download_verified(row, dest)
            self.assertFalse(dest.exists())
            self.assertFalse(Path(str(dest) + ".partial").exists())

    def test_source_contract_uses_existing_worker(self):
        app = (ROOT / "apps/scene-builder/part01/app.py").read_text(encoding="utf-8")
        audit = (ROOT / "apps/scene-builder/part01/templates/audit_media.html").read_text(encoding="utf-8")
        self.assertIn('"/sml-auto-match"', app)
        self.assertIn("sml-select", app)
        self.assertIn('"/jobs/{job_id}/audit/media/{item[\'item_id\']}/upload"', app)
        self.assertIn("Browse SML", audit)
        self.assertIn("autoMatchSml()", audit)
        self.assertNotIn("DELETE /api/clips", app)


if __name__ == "__main__":
    unittest.main()

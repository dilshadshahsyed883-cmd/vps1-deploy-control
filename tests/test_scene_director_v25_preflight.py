"""Director V25 first-slice regression: no Classic/worker changes."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps/scene-builder/part01"
spec = importlib.util.spec_from_file_location("director_v25", WEB / "director_v25.py")
director = importlib.util.module_from_spec(spec)
spec.loader.exec_module(director)


def sample(source_mode="library_only", filename="Alexa 6.mp4", extra="",
           intro_enabled="false", fallback="none"):
    hooks = "\n".join(
        f"[INTRO.SHOT_{i:02d}]\nhook_role=detail\nmedia_type=video\n"
        for i in range(1, 9)
    )
    return f"""SCENE_PLAN_VERSION=2
[PROJECT]
mode=director
name=Test
base_preset=custom
validation=strict
[OUTPUT]
resolution=1080p
aspect_ratio=16:9
fps=30
codec=h264
filename=test.mp4
[MEDIA]
source_mode={source_mode}
avoid_duplicates=true
missing_media=fail
library_summary_generated=2026-10-27
[TIMING]
audio=narration_upload
narration_start=auto
split=manual
max_semantic_beat_seconds=3.0
[CAPTIONS]
captions_enabled=true
caption_style=karaoke
caption_position=bottom
[AUDIO]
voice_volume_percent=100
music_enabled=false
[LOOK]
lut_enabled=true
lut_pack=cinematic
lut_preset=filmic
[INTRO]
intro_enabled={intro_enabled}
intro_duration_seconds=15
{hooks}
[SCENE_001]
narration=Alexa arrives.
visual_direction=Alexa entering town
[SCENE_001.SHOT_01]
media_type=video
library_filename={filename}
library_keyword={Path(filename).stem}
source_fallback={fallback}
motion=none
animation_category=cam_motion
animation=pan_left
animation_duration=2.5
animation_intensity=65
transition_out=cross_dissolve
transition_duration=0.35
{extra}
"""


def asset(name, ident="a" * 32):
    return {"original_name": name, "kind": "video", "approved": True, "id": ident}


class DirectorPreflightTests(unittest.TestCase):
    def test_exact_numbered_filename_selects_six_not_one(self):
        rows = [asset("Alexa 1.mp4", "a" * 32), asset("Alexa 6.mp4", "b" * 32)]
        p = director.preflight_v25(sample(), rows, "library_only", True)
        self.assertEqual(p["status"], "VALIDATED_INTENT_ONLY", p["errors"])
        self.assertFalse(p["render_ready"])
        self.assertEqual(p["resolved_assets"][0]["asset_id"], "b" * 32)
        self.assertEqual(p["resolved_assets"][0]["filename"], "Alexa 6.mp4")

    def test_library_only_never_pexels(self):
        p = director.preflight_v25(sample(filename="Not Here.mp4"), [], "library_only", True)
        self.assertEqual(p["status"], "BLOCKED")
        self.assertEqual(p["resolved_assets"], [])
        p2 = director.preflight_v25(
            sample(extra="pexels_primary_keyword=engine repair\n", fallback="pexels_if_missing"),
            [], "library_only", True)
        self.assertEqual(p2["status"], "BLOCKED")
        self.assertTrue(any("forbidden" in error for error in p2["errors"]))

    def test_scene_first_uses_v24_fallback_without_inventing_local_media(self):
        p = director.preflight_v25(
            sample("scene_first", filename="Unmatched.mp4",
                   extra="pexels_primary_keyword=robot welding factory\n"
                         "pexels_backup_keywords=factory welding machine,industrial robots\n",
                   fallback="pexels_if_missing"),
            [], "scene_first", True)
        self.assertEqual(p["status"], "VALIDATED_INTENT_ONLY", p["errors"])
        self.assertEqual(p["resolved_assets"][0]["source"], "pexels_pending")
        self.assertEqual(p["resolved_assets"][0]["pexels_query"], "robot welding factory")

    def test_ui_source_is_authoritative(self):
        p = director.preflight_v25(sample(), [asset("Alexa 6.mp4")], "scene_first", True)
        self.assertEqual(p["status"], "BLOCKED")
        self.assertIn("conflicts with current UI", " ".join(p["errors"]))

    def test_duplicate_filename_requires_real_id(self):
        rows = [asset("Alexa 6.mp4", "a" * 32), asset("Alexa 6.mp4", "b" * 32)]
        p = director.preflight_v25(sample(), rows, "library_only", True)
        self.assertIn("duplicate exact filename", " ".join(p["errors"]))
        p2 = director.preflight_v25(
            sample(extra="library_asset_id=" + "b" * 32 + "\n"), rows, "library_only", True)
        self.assertEqual(p2["status"], "VALIDATED_INTENT_ONLY", p2["errors"])

    def test_technical_conflict_is_reported_not_overridden(self):
        p = director.preflight_v25(
            sample(extra="motion=slow_push\n"), [asset("Alexa 6.mp4")], "library_only", True)
        # Duplicate keys are rejected by strict parsing.
        self.assertEqual(p["status"], "BLOCKED")
        self.assertIn("syntax", " ".join(p["errors"]))
        txt = sample().replace("motion=none", "motion=slow_push")
        p2 = director.preflight_v25(txt, [asset("Alexa 6.mp4")], "library_only", True)
        self.assertIn("cannot both", " ".join(p2["errors"]))

    def test_classic_script_and_unknown_fields_rejected(self):
        for text in ["SCENE_001|spoken|primary_keyword=test",
                     sample().replace("source_fallback=none", "hidden_command=rm -rf /")]:
            self.assertEqual(director.preflight_v25(text, [], "library_only", True)["status"], "BLOCKED")

    def test_disabled_library_blocks(self):
        p = director.preflight_v25(sample(), [asset("Alexa 6.mp4")], "library_only", False)
        self.assertIn("disabled", " ".join(p["errors"]))

    def test_legacy_parser_and_worker_are_not_changed_by_preflight(self):
        app = (WEB / "app.py").read_text(encoding="utf-8")
        self.assertIn("def parse_authoritative_scene_plan(", app)
        self.assertIn("parse_outer_ai_scene_txt(txt_path)", app)
        self.assertIn("build_project_package(", app)
        self.assertNotIn("director_v25", (ROOT / "apps/scene-builder/part02/api_server.py")
                         .read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

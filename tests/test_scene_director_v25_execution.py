"""V25 Director execution contract, with exact local media and Classic isolation."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps/scene-builder/part01"
sys.path.insert(0, str(WEB))

from director_v25 import DirectorSpecError, preflight_v25
from director_v25_execution import compile_v25, bundle_exact_director_media


def plan(source_mode="library_only", name="Alexa 6.mp4", intro=False,
         extra="", fallback="none", preset="cinematic_documentary"):
    slots = "\n".join(
        f"[INTRO.SHOT_{i:02d}]\nhook_role=detail\nmedia_type=video\n"
        + (f"library_filename=Intro {i}.mp4\nlibrary_keyword=Intro {i}\n"
           if intro and source_mode == "library_only" else
           ("pexels_primary_keyword=factory machinery\n"
            if intro else ""))
        for i in range(1, 9)
    )
    return f"""SCENE_PLAN_VERSION=2
[PROJECT]
mode=director
name=Atlas
base_preset={preset}
validation=strict
[OUTPUT]
resolution=1080p
aspect_ratio=16:9
fps=30
codec=h264
filename=Atlas.mp4
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
intro_enabled={'true' if intro else 'false'}
intro_duration_seconds=16
{slots}
[SCENE_001]
narration=Alexander surveys the city.
visual_direction=City arrival
[SCENE_001.SHOT_01]
media_type=video
library_filename={name}
library_keyword={Path(name).stem}
source_fallback={fallback}
motion=none
animation_category=cam_motion
animation=pan_left
animation_duration=2.5
animation_intensity=65
transition_out=none
transition_duration=0
{extra}
"""


class Catalog:
    def __init__(self, root, files):
        self.root = root
        self.rows = []
        for i, filename in enumerate(files, 1):
            ident = format(i, "032x")
            path = self.root / (ident + Path(filename).suffix)
            path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"test-media" * 5)
            self.rows.append({"id": ident, "original_name": filename,
                              "kind": "video", "approved": True, "extension": ".mp4"})
    def list(self, limit=500):
        return list(self.rows)
    def file(self, asset_id):
        row = next(x for x in self.rows if x["id"] == asset_id)
        return self.root / (asset_id + row["extension"]), row


CAPS = {
    "caption_styles": ["karaoke", "documentary"],
    "lut_packs": {"cinematic": ["filmic", "blockbuster"]},
}


class DirectorExecutionTests(unittest.TestCase):
    def test_numbered_file_compiles_and_bundles_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = Catalog(root, ["Alexa 1.mp4", "Alexa 6.mp4"])
            result = compile_v25(plan(), catalog, source_mode="library_only",
                                 library_enabled=True,
                                 selected_preset="cinematic_documentary", capabilities=CAPS)
            shot = result["scenes"][0]
            self.assertEqual(shot["director_v25_media_asset_id"], format(2, "032x"))
            self.assertEqual(shot["capcut_animation_effect"], "pan_left")
            self.assertEqual(shot["capcut_animation_target"], "videos")
            self.assertEqual(shot["transition_out"], "none")
            self.assertEqual(shot["outer_ai_source"], "director_v25")
            bundled = bundle_exact_director_media(result, root / "assets/scene_local", catalog)
            self.assertEqual(bundled, {})
            self.assertTrue((root / shot["media_path"]).is_file())
            self.assertEqual(shot["media_provider"], "scene_media_library")
            self.assertFalse(shot["pexels_enabled"])
            self.assertEqual(result["narration_text"], "Alexander surveys the city.")

    def test_distinct_pexels_queries_stay_out_of_local_keywords(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp), [])
            result = compile_v25(
                plan(source_mode="scene_first", name="No local.mp4",
                     fallback="pexels_if_missing",
                     extra="pexels_primary_keyword=Alexander arriving ancient city\n"
                           "pexels_backup_keywords=ancient city streets,historical city\n"),
                catalog, source_mode="scene_first", library_enabled=True,
                selected_preset="cinematic_documentary", capabilities=CAPS)
            shot = result["scenes"][0]
            self.assertEqual(shot["director_v25_media_source"], "pexels_pending")
            self.assertEqual(shot["primary_keyword"], "Alexander arriving ancient city")
            self.assertEqual(shot["fallback_keywords"], ["ancient city streets", "historical city"])
            self.assertTrue(shot["pexels_enabled"])
            self.assertNotIn("No local", shot["primary_keyword"])

    def test_multiple_shots_cannot_guess_spoken_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp), ["Alexa 6.mp4", "Alexa 7.mp4"])
            txt = plan()
            txt += """
[SCENE_001.SHOT_02]
media_type=video
library_filename=Alexa 7.mp4
library_keyword=Alexa 7
transition_out=none
"""
            with self.assertRaisesRegex(DirectorSpecError, "narration"):
                compile_v25(txt, catalog, source_mode="library_only", library_enabled=True,
                            selected_preset="cinematic_documentary", capabilities=CAPS)
            txt = txt.replace("[SCENE_001.SHOT_01]\n", "[SCENE_001.SHOT_01]\nnarration=Alexander surveys\n")
            txt = txt.replace("[SCENE_001.SHOT_02]\n", "[SCENE_001.SHOT_02]\nnarration=the city.\n")
            result = compile_v25(txt, catalog, source_mode="library_only", library_enabled=True,
                                 selected_preset="cinematic_documentary", capabilities=CAPS)
            self.assertEqual([s["text"] for s in result["scenes"]], ["Alexander surveys", "the city."])
            self.assertEqual(len(result["scenes"]), 2)

    def test_intro_eight_hook_bundles_and_preserves_edit_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp), ["Alexa 6.mp4"] + [f"Intro {i}.mp4" for i in range(1, 9)])
            result = compile_v25(plan(intro=True), catalog, source_mode="library_only",
                                 library_enabled=True,
                                 selected_preset="cinematic_documentary", capabilities=CAPS)
            self.assertEqual(len(result["intro_queries"]), 8)
            self.assertEqual(len(result["intro_edit"]), 8)
            self.assertEqual(result["settings"]["intro_media_max_seconds"], 2.0)
            mapping = bundle_exact_director_media(result, Path(tmp)/"assets/scene_local", catalog)
            self.assertEqual(len(mapping), 8)
            for i in range(1, 9):
                self.assertEqual(mapping[f"INTRO_{i:03d}"]["provider_asset_id"],
                                 format(i+1, "032x"))

    def test_source_mode_preset_and_filename_conflicts_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Catalog(Path(tmp), ["Alexa 6.mp4"])
            bad = [
                (plan(preset="not_matching"), "base_preset"),
                (plan().replace("name=Atlas", "name=Wrong"), "filename"),
                (plan().replace("animation=pan_left", "animation=unknown_motion"), "preflight"),
            ]
            for txt, reason in bad:
                with self.subTest(reason=reason):
                    with self.assertRaises((DirectorSpecError, ValueError)):
                        compile_v25(txt, catalog, source_mode="library_only", library_enabled=True,
                                    selected_preset="cinematic_documentary", capabilities=CAPS)

    def test_existing_classic_workflows_and_worker_are_conditionally_isolated(self):
        web = (WEB / "app.py").read_text()
        worker = (ROOT / "apps/scene-builder/part02/api_server.py").read_text()
        queue = (WEB / "templates/queue.html").read_text()
        ast.parse(web)
        ast.parse(worker)
        ast.parse((WEB/"director_v25.py").read_text())
        ast.parse((WEB/"director_v25_execution.py").read_text())
        self.assertIn("parse_outer_ai_scene_txt(txt_path)", web)
        self.assertIn('director_processor: str = Form("classic")', web)
        self.assertIn("if director_compilation is not None:", web)
        self.assertIn('if bool((settings.metadata or {}).get("director_v25")):', worker)
        self.assertIn("splits/merges are forbidden in Director Mode", worker)
        self.assertIn("Director V25 — validate & render", queue)


if __name__ == "__main__":
    unittest.main()

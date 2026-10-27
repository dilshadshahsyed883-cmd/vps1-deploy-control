"""Regression: user-reported Eala Tokyo V25 337-shot Whisper stage failure."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "apps/scene-builder/part02/director_v25_timing.py"
spec = importlib.util.spec_from_file_location("director_v25_timing", MODULE)
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)


def authored(lengths):
    cursor = 0.0
    rows = []
    for i, duration in enumerate(lengths, 1):
        rows.append({
            "director_v25_shot": f"SCENE_{i:03d}.SHOT_01",
            "text": f"Spoken narration {i}",
            "media_path": f"assets/scene_local/asset_{i:03d}.mp4",
            "media_provider": "scene_media_library",
            "provider_asset_id": f"{i:032x}",
            "review_approved": True, "review_locked": True,
            "animation": "pan_left", "transition_out": "hard_cut",
            "start": cursor, "end": cursor + duration, "duration": duration,
            "timing_source": "whisper_word_alignment",
        })
        cursor += duration
    return rows, cursor


class DirectorV25TimingTests(unittest.TestCase):
    def test_within_cap_is_unchanged(self):
        scenes, total = authored([2, 1, 1.9, 2.8])
        result, report = timing.fit_director_shot_timing(scenes, audio_duration=total)
        self.assertEqual([r["end"] for r in result], [2, 3, 4.9, 7.7])
        self.assertEqual(report["shifted_whisper_boundaries"], 0)

    def test_long_whisper_beat_does_not_split_merge_or_change_media(self):
        scenes, total = authored([4.5, 1, 1.5, 2, 3.5, 0.8])
        result, report = timing.fit_director_shot_timing(scenes, audio_duration=total)
        self.assertEqual(len(result), len(scenes))
        self.assertEqual(result[-1]["end"], round(total, 3))
        self.assertTrue(all(.049 <= r["duration"] <= 3.001 for r in result))
        for key in ("director_v25_shot", "text", "media_path", "provider_asset_id",
                    "review_approved", "review_locked", "transition_out", "animation"):
            self.assertEqual([r[key] for r in result], [r[key] for r in scenes], key)
        self.assertGreater(report["shifted_whisper_boundaries"], 0)
        self.assertFalse(report["automatic_split_merge"])

    def test_337_shot_narration_with_long_and_tiny_beats(self):
        lengths = [4.1 if i % 8 == 0 else 1.35 if i % 7 == 0 else 2.1
                   for i in range(337)]
        scenes, total = authored(lengths)
        result, report = timing.fit_director_shot_timing(scenes, audio_duration=total)
        self.assertEqual(len(result), 337)
        self.assertEqual(report["authored_shots"], 337)
        self.assertLessEqual(max(r["duration"] for r in result), 3.0)
        self.assertEqual(result[-1]["end"], round(total, 3))
        self.assertEqual(result[0]["start"], 0)
        self.assertEqual([r["director_v25_shot"] for r in result],
                         [r["director_v25_shot"] for r in scenes])

    def test_impossible_audio_fails_explicitly_without_repeating_media(self):
        scenes, total = authored([5, 5])
        with self.assertRaisesRegex(timing.DirectorTimingError, "at most 6.00s"):
            timing.fit_director_shot_timing(scenes, audio_duration=total)

    def test_duplicate_shot_identity_and_invalid_alignment_fail(self):
        scenes, total = authored([2, 2])
        scenes[1]["director_v25_shot"] = scenes[0]["director_v25_shot"]
        with self.assertRaisesRegex(timing.DirectorTimingError, "duplicated"):
            timing.fit_director_shot_timing(scenes, audio_duration=total)
        scenes, total = authored([2, 2])
        scenes[1]["start"] = -1
        with self.assertRaisesRegex(timing.DirectorTimingError, "Whisper timing invalid"):
            timing.fit_director_shot_timing(scenes, audio_duration=total)

    def test_positive_narration_start_offset(self):
        scenes, total = authored([3.8, 1.5, 2, 2])
        for row in scenes:
            row["start"] += 1.1
            row["end"] += 1.1
        result, _ = timing.fit_director_shot_timing(
            scenes, audio_duration=total + 1.1, narration_start=1.1,
        )
        self.assertEqual(result[0]["start"], 1.1)
        self.assertEqual(result[-1]["end"], round(total + 1.1, 3))

    def test_worker_is_opt_in_and_classic_still_uses_original_normalizer(self):
        worker = (ROOT / "apps/scene-builder/part02/api_server.py").read_text(encoding="utf-8")
        ast.parse(worker)
        ast.parse(MODULE.read_text(encoding="utf-8"))
        self.assertIn("if director_v25:", worker)
        self.assertIn("fit_director_shot_timing(", worker)
        self.assertIn("else:\n        # Classic keeps its existing hook/general split", worker)
        self.assertIn("scenes = normalize_scene_timing(", worker)
        self.assertIn("director_v25_timing_fit", worker)
        self.assertIn("automatic splits/merges are forbidden", worker.replace('"\n                "', ''))

if __name__ == "__main__":
    unittest.main()

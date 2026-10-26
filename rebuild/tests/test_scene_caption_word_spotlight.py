"""Offline Scene Builder word-spotlight regression tests. No VPS/media/network."""
from __future__ import annotations

from pathlib import Path
import ast
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
PART2 = ROOT / "apps/scene-builder/part02"
sys.path.insert(0, str(PART2))


class SceneWordSpotlightTests(unittest.TestCase):
    def _write(self, segment, **settings):
        from smart_scroll.v2_renderer import _write_ass
        options = dict(
            caption_style="hormozi_purple",
            caption_karaoke_enabled=True,
            caption_highlight_color="#aa66ff",
            text_color="#ffffff",
            font_family="DejaVu Sans",
            font_size=48,
            bold=True,
            outline_enabled=True,
            outline_width=3,
        )
        options.update(settings)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "captions.ass"
            self.assertTrue(_write_ass([segment], path, SimpleNamespace(**options), 1920, 1080))
            content = path.read_text(encoding="utf-8-sig")
        return content, [line for line in content.splitlines() if line.startswith("Dialogue: ")]

    def test_whisper_word_highlight_and_silent_gaps(self):
        segment = {
            "start": 0.0, "end": 1.0, "text": "the quick",
            "words": [
                {"word": "the", "start": .20, "end": .35, "timing_source": "whisper"},
                {"word": "quick", "start": .62, "end": .85, "timing_source": "whisper"},
            ],
        }
        content, lines = self._write(segment)
        # Plain lead-in, THE emphasis, white gap, QUICK emphasis, plain tail.
        self.assertEqual(len(lines), 5)
        self.assertEqual(sum(r"{\1c&HFF66AA&}" in line for line in lines), 2)
        self.assertIn(r"{\1c&HFF66AA&}THE{\r}", lines[1])
        self.assertIn(r"{\1c&HFF66AA&}QUICK{\r}", lines[3])
        self.assertNotIn(r"{\1c&HFF66AA&}", lines[2])
        self.assertIn("0:00:00.20,0:00:00.35", lines[1])
        self.assertIn("0:00:00.62,0:00:00.85", lines[3])
        self.assertNotIn(r"\kf", content)

    def test_custom_word_highlight_keeps_original_case_and_color(self):
        segment = {
            "start": 0.0, "end": .8, "text": "Hello WORLD",
            "words": [
                {"word": "Hello", "start": 0.0, "end": .30},
                {"word": "WORLD", "start": .40, "end": .70},
            ],
        }
        content, lines = self._write(
            segment, caption_style="word_highlight", caption_highlight_color="#20c070"
        )
        self.assertIn(r"{\1c&H70C020&}Hello{\r}", content)
        self.assertIn(r"{\1c&H70C020&}WORLD{\r}", content)
        self.assertEqual(len(lines), 4)

    def test_missing_or_invalid_word_times_fall_back_without_disappearing(self):
        from smart_scroll.v2_renderer import _word_spotlight_events
        for rows in ([], [{"start": .9, "end": 1}, {"start": .2, "end": .4}],
                     [{"start": float("nan"), "end": .2}, {"start": .4, "end": .5}]):
            with self.subTest(rows=rows):
                events = _word_spotlight_events("alpha beta", 0, 1, rows, "#aa66ff")
                self.assertTrue(events)
                self.assertAlmostEqual(events[0][0], 0)
                self.assertAlmostEqual(events[-1][1], 1)
                self.assertEqual(sum(r"{\1c" in text for _, _, text in events), 2)
                for i, (begin, end, _) in enumerate(events):
                    self.assertLess(begin, end)
                    if i:
                        self.assertAlmostEqual(begin, events[i - 1][1])

    def test_overlap_gives_newest_word_priority(self):
        from smart_scroll.v2_renderer import _word_spotlight_events
        events = _word_spotlight_events(
            "one two", 0, 1,
            [{"start": 0.1, "end": 0.7}, {"start": .4, "end": .9}],
            "#aa66ff",
        )
        overlapping = [line for begin, end, line in events if begin <= .4 < end]
        self.assertEqual(len(overlapping), 1)
        self.assertIn(r"}two{\r}", overlapping[0])
        self.assertNotIn(r"}one{\r}", overlapping[0])

    def test_existing_karaoke_style_retains_legacy_ass(self):
        segment = {
            "start": 0, "end": 1.0, "text": "alpha beta",
            "words": [{"start": 0, "end": .2}, {"start": .7, "end": .9}],
        }
        content, lines = self._write(segment, caption_style="karaoke")
        self.assertEqual(len(lines), 1)
        self.assertIn(r"{\kf70}alpha", content)
        self.assertIn(r"{\kf30}beta", content)

    def test_preset_registration_and_python_parse(self):
        path = ROOT / "apps/scene-builder/part01/app.py"
        source = path.read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn('"hormozi_purple": {"label": "Hormozi', source)
        self.assertIn('"word_highlight": {"label": "Word Spotlight', source)
        self.assertIn("if caption_style not in CAPTION_STYLE_PRESETS:", source)


if __name__ == "__main__":
    unittest.main()

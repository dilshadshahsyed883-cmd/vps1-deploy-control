"""Real FFmpeg regression matrix for Scene Builder selected effects (CI-only media).

No production queues, VPS, credentials, external downloads or user media.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
PART2 = ROOT / "apps/scene-builder/part02"
sys.path.insert(0, str(PART2))


def ffmpeg(*args, timeout=150, capture=True):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-y", "-filter_complex_threads", "1", *map(str, args)]
    run = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if run.returncode:
        raise AssertionError(f"FFmpeg failed: {run.stderr.decode(errors='replace')[-1800:]}\n{cmd}")
    return run.stdout if capture else b""


def image_fixture(folder):
    image = folder / "pattern.png"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=8:d=0.5",
           "-frames:v", "1", "-threads", "1", image)
    video = folder / "frozen.mp4"
    ffmpeg("-loop", "1", "-framerate", "8", "-i", image,
           "-t", "3.5", "-r", "8", "-pix_fmt", "yuv420p",
           "-c:v", "libx264", "-preset", "ultrafast", video)
    return image, video


def frame_bytes(source, filtergraph, *, image=True, width=128, height=72, seconds=1.5, fps=8):
    cmd = (["-loop", "1", "-framerate", str(fps), "-t", str(seconds), "-i", source]
           if image else ["-i", source])
    data = ffmpeg(*cmd, "-vf", filtergraph, "-frames:v", str(int(seconds*fps)),
                  "-pix_fmt", "rgb24", "-f", "rawvideo", "-")
    length = width * height * 3
    assert len(data) >= length * 2 and len(data) % length == 0, len(data)
    return data[:length], data[-length:]


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe required")
class SceneEngineEffects(unittest.TestCase):
    def test_selected_image_and_video_motions_change_real_pixels(self):
        from smart_scroll.v2_renderer import _motion
        with tempfile.TemporaryDirectory(prefix="scene-motion-ci-") as tmp:
            image, video = image_fixture(Path(tmp))
            for name in ("pan_left", "pan_right", "ken_burns", "zoom_in", "zoom_out"):
                with self.subTest(motion=name):
                    f = _motion({"motion": name, "motion_intensity": 0.8}, 128, 72, 8, 1.5, True)
                    first, last = frame_bytes(image, f)
                    self.assertNotEqual(hashlib.sha256(first).digest(), hashlib.sha256(last).digest(), name)
            for name in ("slow_push", "reframe_left", "reframe_right", "gentle_zoom"):
                with self.subTest(motion=name):
                    f = _motion({"motion": name, "motion_intensity": 0.8}, 128, 72, 8, 1.5, False)
                    first, last = frame_bytes(video, f, image=False)
                    self.assertNotEqual(hashlib.sha256(first).digest(), hashlib.sha256(last).digest(), name)
            # No motion must not acquire unexpected zoom/pan.
            f = _motion({"motion": "none"}, 128, 72, 8, 1.5, True)
            self.assertEqual(*frame_bytes(image, f))

    def test_each_named_transition_is_a_distinct_real_ffmpeg_effect(self):
        from smart_scroll.transition_engine import TRANSITION_CATALOG
        from smart_scroll.v2_renderer import _xfade_filter
        results = {}
        for name in TRANSITION_CATALOG:
            if name in {"none", "hard_cut"}:
                continue
            with self.subTest(transition=name):
                spec = _xfade_filter({"transition_out": name}, 0, 0.6, 0.3)
                data = ffmpeg("-f", "lavfi", "-i", "testsrc2=s=128x72:r=12:d=1.2",
                              "-f", "lavfi", "-i", "smptebars=s=128x72:r=12:d=1.2",
                              "-filter_complex", f"[0:v][1:v]{spec},format=yuv420p[v]",
                              "-map", "[v]", "-frames:v", "18", "-pix_fmt", "rgb24",
                              "-f", "rawvideo", "-")
                self.assertGreater(len(data), 128*72*3*8)
                results[name] = hashlib.sha256(data).hexdigest()
        # Selected names must not collapse into the same fade output.
        self.assertEqual(len(results), len(set(results.values())), results)

    def test_whisper_word_times_survive_grouping_and_drive_karaoke(self):
        from smart_scroll.caption_sync import _script_guided_segments_from_anchors, CaptionSegment
        from smart_scroll.v2_renderer import _write_ass
        with tempfile.TemporaryDirectory(prefix="scene-word-ci-") as tmp:
            segments = _script_guided_segments_from_anchors(
                script_text="alpha beta", anchors=[(0.0, 0.2), (0.7, 0.9)],
                duration=1.0, max_words=8, offset=0.0)
            self.assertEqual(len(segments), 1)
            self.assertEqual([w["timing_source"] for w in segments[0].words], ["whisper", "whisper"])
            self.assertAlmostEqual(segments[0].words[1]["start"], 0.7)
            dest = Path(tmp) / "captions.ass"
            ok = _write_ass([segments[0].to_dict()], dest,
                            SimpleNamespace(caption_karaoke_enabled=True), 1280, 720)
            self.assertTrue(ok)
            body = dest.read_text(encoding="utf-8-sig")
            self.assertIn(r"{\\kf70}alpha", body)
            self.assertIn(r"{\\kf30}beta", body)
            self.assertNotIn(r"{\\kf50}alpha", body)
            # Backward compatibility with saved caption rows lacking words.
            older = CaptionSegment(start=0, end=1, text="old caption")
            self.assertTrue(_write_ass([older.to_dict()], dest,
                                       SimpleNamespace(caption_karaoke_enabled=True), 1280, 720))

    def test_selected_filter_stays_active_after_lut(self):
        from smart_scroll.v2_renderer import _resolved_color_filters, _filter
        settings = SimpleNamespace(video_quality_preset="enhanced")
        scene = {"filter": "warm", "filter_intensity": 0.9}
        f, quality = _resolved_color_filters(scene, settings, "lut3d=file='/tmp/example.cube'")
        self.assertEqual(f, _filter("warm", 0.9))
        self.assertNotEqual(f, "null")
        self.assertNotIn("eq=contrast=", quality)
        self.assertIn("saturation=1", _filter("black_white", 0.0))
        self.assertIn("saturation=0", _filter("black_white", 1.0))

    def test_all_23_builtin_luts_strength_affects_rendered_pixels(self):
        from smart_scroll.lut_engine import PACK_DEFINITIONS, ensure_builtin_luts, blended_lut
        from smart_scroll.lut_engine import ffmpeg_filter_path
        self.assertEqual(sum(len(pack["presets"]) for pack in PACK_DEFINITIONS.values()), 23)
        with tempfile.TemporaryDirectory(prefix="scene-lut-ci-") as tmp:
            root = Path(tmp)
            ensure_builtin_luts(root)
            image, _ = image_fixture(root)
            zero_filters = set()
            full_filters = set()
            for pack, spec in PACK_DEFINITIONS.items():
                for preset in spec["presets"]:
                    with self.subTest(pack=pack, preset=preset):
                        source = root / pack / f"{preset}.cube"
                        self.assertTrue(source.is_file(), source)
                        low = blended_lut(source, 0, root / "cache")
                        high = blended_lut(source, 100, root / "cache")
                        def pixels(lut):
                            return ffmpeg("-i", image, "-vf",
                                          f"format=rgb24,lut3d=file='{ffmpeg_filter_path(lut)}',format=rgb24",
                                          "-frames:v", "1", "-pix_fmt", "rgb24",
                                          "-f", "rawvideo", "-")
                        before, after = pixels(low), pixels(high)
                        self.assertEqual(len(before), len(after))
                        self.assertNotEqual(hashlib.sha256(before).digest(), hashlib.sha256(after).digest(),
                                            f"{pack}/{preset} strength ignored")
                        zero_filters.add(hashlib.sha256(before).hexdigest())
                        full_filters.add(hashlib.sha256(after).hexdigest())
            self.assertGreaterEqual(len(full_filters), 20)

    def test_multiscene_720p_1080p_landscape_and_portrait_outputs(self):
        from smart_scroll.v2_renderer import render_mixed_media, _validate_final_mp4
        with tempfile.TemporaryDirectory(prefix="scene-matrix-ci-") as tmp:
            root = Path(tmp)
            image, video = image_fixture(root)
            audio = root / "narration.wav"
            ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3.15",
                   "-c:a", "pcm_s16le", audio)
            scenes = [
                {"scene_code": "CI_IMAGE_1", "start": 0, "end": 1.05,
                 "media_type": "image", "media_path": str(image), "motion": "pan_left",
                 "motion_intensity": 0.65, "filter": "warm",
                 "transition_out": "cross_dissolve", "transition_duration_ms": 200},
                {"scene_code": "CI_VIDEO_2", "start": 1.05, "end": 2.10,
                 "media_type": "video", "media_path": str(video), "motion": "slow_push",
                 "motion_intensity": 0.65, "filter": "none",
                 "transition_out": "directional_wipe", "transition_duration_ms": 200},
                {"scene_code": "CI_IMAGE_3", "start": 2.10, "end": 3.15,
                 "media_type": "image", "media_path": str(image), "motion": "ken_burns",
                 "motion_intensity": 0.65, "filter": "none", "transition_out": "none"},
            ]
            for width, height in ((1280,720),(720,1280),(1920,1080),(1080,1920)):
                with self.subTest(canvas=(width,height)):
                    folder = root / f"{width}x{height}"
                    folder.mkdir()
                    output = folder / "complete.mp4"
                    conf = SimpleNamespace(
                        width=width, height=height, fps=8, quality_crf=26,
                        intermediate_render_preset="ultrafast", render_chunk_scenes=4,
                        video_quality_preset="original",
                        output_path=str(output), audio_path=str(audio),
                        render_audio_path=str(audio), lut_enabled=False,
                        captions_enabled=False,
                        metadata={"scene_sync_plan": scenes, "caption_segments": []})
                    result = render_mixed_media(conf)
                    self.assertEqual(Path(result), output)
                    probe = _validate_final_mp4(output, "ffmpeg", expected_duration=3.15)
                    self.assertEqual((probe["width"],probe["height"]), (width,height))
                    self.assertEqual(probe["video_codec"], "h264")
                    self.assertEqual(probe["audio_codec"], "aac")


if __name__ == "__main__":
    unittest.main()

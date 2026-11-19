"""Offline unit and small-frame FFmpeg integration tests for Short Maker Desktop."""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "short_maker.py"
spec = importlib.util.spec_from_file_location("short_maker_desktop", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def ppm(path: Path, rgb: bytes):
    path.write_bytes(b"P6\n24 40\n255\n" + rgb * (24 * 40))


class ShortMakerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.output = self.root / "output"
        self.source.mkdir()
        self.output.mkdir()
        (self.source / "one.jpg").write_bytes(b"sample")
        (self.source / "music.mp3").write_bytes(b"sample")

    def tearDown(self):
        self.temp.cleanup()

    def test_one_folder_detects_images_and_audio(self):
        nested = self.source / "nested"
        nested.mkdir()
        (nested / "second.PNG").write_bytes(b"sample")
        (nested / "voice.WAV").write_bytes(b"sample")
        images, audio = mod.scan_folder(str(self.source))
        self.assertEqual(len(images), 2)
        self.assertEqual(len(audio), 2)

    def test_output_folder_is_required_and_separate(self):
        with self.assertRaises(ValueError):
            mod.validate_paths(str(self.source), "")
        with self.assertRaises(ValueError):
            mod.validate_paths(str(self.source), str(self.source))
        nested = self.source / "inside"
        nested.mkdir()
        with self.assertRaises(ValueError):
            mod.validate_paths(str(self.source), str(nested))
        mod.validate_paths(str(self.source), str(self.output))

    def test_audio_cycles_without_adjacent_repeat(self):
        files = ["a.mp3", "b.wav", "c.flac"]
        sequence = [mod.picked_audio(files, 12345, i) for i in range(30)]
        self.assertEqual(set(sequence), set(files))
        self.assertTrue(all(a != b for a, b in zip(sequence, sequence[1:])))
        self.assertIsNone(mod.picked_audio([], 1, 0))

    def test_concat_catalog_and_motion(self):
        self.assertEqual(len(mod.CONCAT_TRANSITIONS), 20)
        self.assertEqual(len(mod.MOTIONS), 10)
        self.assertIn("spring-zoom", mod.MOTIONS)
        self.assertIn("gravity-bounce", mod.MOTIONS)
        for motion in mod.MOTIONS:
            z, x, y = mod.motion_params(motion, 90)
            self.assertTrue(z and x and y)

    def test_persistent_queue_preserves_outputs_and_choices(self):
        queue_file = self.root / "queue.json"
        with patch.object(mod, "QUEUE_PATH", queue_file):
            queue = mod.RenderQueue()
            job = queue.enqueue(str(self.source), str(self.output), [], 16, 3, 5)
            self.assertEqual(job["status"], "queued")
            self.assertEqual(len(job["audios"]), 1)
            queue.action(job["id"], "pause")
            self.assertEqual(mod.RenderQueue().jobs[0]["status"], "paused")
            queue.action(job["id"], "delete")
            self.assertEqual(mod.RenderQueue().jobs, [])
            self.assertTrue((self.source / "one.jpg").exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "FFmpeg/FFprobe required for integration test")
    def test_small_frame_audio_and_transitions_render(self):
        a = self.root / "red.ppm"
        b = self.root / "blue.ppm"
        ppm(a, bytes([220, 30, 30]))
        ppm(b, bytes([20, 40, 210]))
        sound = self.root / "sound.wav"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        str(sound)], check=True, timeout=35)
        out = self.output / "test.mp4"
        cmd = mod.build_command([str(a), str(b)], str(sound), str(out),
                                seconds=1.8, image_seconds=0.8, seed=53,
                                fps=10, width=144, height=256)
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        self.assertTrue(mod.verified_video(out, 1.8))
        self.assertGreater(out.stat().st_size, 4096)
        silent = self.output / "silent.mp4"
        cmd2 = mod.build_command([str(a)], None, str(silent),
                                 seconds=1.2, image_seconds=2.0, seed=17,
                                 fps=10, width=144, height=256)
        subprocess.run(cmd2, check=True, capture_output=True, timeout=120)
        self.assertTrue(mod.verified_video(silent, 1.2))


if __name__ == "__main__":
    unittest.main()

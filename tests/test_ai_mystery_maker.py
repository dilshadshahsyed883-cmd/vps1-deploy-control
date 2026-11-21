"""Offline tests; never call real AI/GPU/Drive endpoints."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MOD = Path(__file__).resolve().parents[1] / "pilot" / "ai_mystery_maker.py"
spec = importlib.util.spec_from_file_location("ai_mystery_maker", MOD)
maker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maker)

STORY = {
    "title": "A Message Sent From Tomorrow",
    "narration": " ".join(["mysterious"] * 125),
    "scenes": [{"prompt": "Cinematic futuristic dark laboratory with one person, no text."} for _ in range(8)],
}


class MakerTests(unittest.TestCase):
    def test_story_validation(self):
        self.assertEqual(len(maker.valid_story(STORY)["scenes"]), 8)
        with self.assertRaises(maker.MakerError):
            maker.valid_story({**STORY, "scenes": []})
        with self.assertRaises(maker.MakerError):
            maker.valid_story({**STORY, "narration": "too short"})

    def test_localhost_only(self):
        self.assertEqual(maker.base("http://localhost:8188/"), "http://localhost:8188")
        for url in ("https://evil.example", "http://192.168.1.2:8188", "http://localhost.evil.com"):
            with self.assertRaises(maker.MakerError):
                maker.base(url)

    def test_ollama_json(self):
        with patch.object(maker, "request_json", return_value={"response": json.dumps(STORY)}) as request:
            story = maker.script_from_ollama("A phone call from tomorrow", "http://localhost:11434", "qwen3:4b")
            self.assertEqual(story["title"], STORY["title"])
            self.assertEqual(request.call_args.args[1]["format"], "json")

    def test_comfy_image_replaces_workflow_without_mutation(self):
        workflow = {"6": {"inputs": {"text": "placeholder"}}, "9": {"inputs": {"filename_prefix": "test"}}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(maker, "request_json", side_effect=[
            {"prompt_id": "f" * 32}, {"f" * 32: {"outputs": {"9": {"images": [{"filename": "x.png", "type": "output"}]}}}}]), patch.object(maker, "request_bytes", return_value=b"\x89PNG\r\n\x1a\n123"):
            image = maker.generate_comfy_image("new prompt", "http://localhost:8188", workflow, "6", Path(tmp) / "a.png")
            self.assertTrue(image.is_file())
            self.assertEqual(workflow["6"]["inputs"]["text"], "placeholder")

    def test_kokoro_poll_download(self):
        wav = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 40
        with tempfile.TemporaryDirectory() as tmp, patch.object(maker, "request_json", side_effect=[
            {"job_id": "KOJ-" + "A" * 16},
            {"jobs": [{"job_id": "KOJ-" + "A" * 16, "status": "completed", "audio_url": "/tts/api/kokoro/audio/" + "KOJ-" + "A" * 16}]}
        ]), patch.object(maker, "request_bytes", return_value=wav):
            audio = maker.synthesize_kokoro("hello", "http://localhost:8143", Path(tmp))
            self.assertEqual(audio.read_bytes(), wav)

    def test_render_command_and_captions(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            voice = d / "narration.wav"
            voice.write_bytes(b"fake")
            img = d / "01.png"
            img.write_bytes(b"fake")
            def fake_run(args, cwd=None):
                if args[0] == "ffprobe":
                    return '{"format":{"duration":"60.0"}}'
                (cwd / "video.mp4").write_bytes(b"X" * 1001)
                return ""
            with patch.object(maker, "run", side_effect=fake_run) as run:
                out = maker.render([img], voice, STORY["narration"], d)
                self.assertTrue(out.is_file())
                self.assertIn("subtitles=captions.srt", " ".join(run.call_args.args[0]))
                self.assertIn("00:01:00,000", (d / "captions.srt").read_text())

    def test_drive_upload_verified_and_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            video.write_bytes(b"sample")
            checksum = maker.hashlib.md5(b"sample").hexdigest()
            with patch.object(maker, "remote_md5", side_effect=[None, checksum]), patch.object(maker, "run") as process:
                self.assertEqual(maker.upload_verified(video, "gdrive:AI Mystery Videos/job.mp4"), "gdrive:AI Mystery Videos/job.mp4")
                self.assertEqual(process.call_count, 1)
            with patch.object(maker, "remote_md5", return_value="a" * 32):
                with self.assertRaisesRegex(maker.MakerError, "different content"):
                    maker.upload_verified(video, "gdrive:AI Mystery Videos/job.mp4")
            with self.assertRaises(maker.MakerError):
                maker.upload_verified(video, "gdrive:../secret")

    def test_rclone_auth_failure_does_not_appear_missing(self):
        from subprocess import CompletedProcess
        with patch.object(maker.subprocess, "run", return_value=CompletedProcess([], 1, "", "403 permission denied")):
            with self.assertRaisesRegex(maker.MakerError, "verification error"):
                maker.remote_md5("gdrive:clip.mp4")
        with patch.object(maker.subprocess, "run", return_value=CompletedProcess([], 1, "", "directory not found")):
            self.assertIsNone(maker.remote_md5("gdrive:clip.mp4"))

    def test_produce_fails_closed_and_keeps_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"ollama_url": "http://localhost:11434", "kokoro_url": "http://localhost:8143", "comfy_url": "http://localhost:8188", "comfy_workflow": str(Path(tmp) / "missing.json"), "comfy_prompt_node": "6"}
            with patch.object(maker, "script_from_ollama", return_value=STORY), patch.object(maker, "synthesize_kokoro", side_effect=maker.MakerError("worker unavailable")):
                with self.assertRaises(maker.MakerError):
                    maker.produce("A phone call from tomorrow", Path(tmp), config)
            folders = [p for p in Path(tmp).iterdir() if p.is_dir()]
            self.assertEqual(len(folders), 1)
            self.assertEqual(json.loads((folders[0] / "result.json").read_text())["status"], "failed")
            self.assertTrue((folders[0] / "story.json").is_file())


if __name__ == "__main__":
    unittest.main()

"""GPU candidate unit contracts run without an NVIDIA card (hardware acceptance is separate)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_render.concat.gpu.cuda_backend import (
    CUDACompatibilityError, parse_project, segment_command, probe_cuda,
)


class CudaPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        (self.project / "media").mkdir()
        (self.project / "media" / "image1.png").write_bytes(b"placeholder")
        (self.project / "media" / "image2.png").write_bytes(b"placeholder")
        self.doc = {
            "version": 1, "activeTimelineId": "TL1",
            "media": [
                {"id": "m1", "kind": "image", "path": str(self.project / "media/image1.png")},
                {"id": "m2", "kind": "image", "path": str(self.project / "media/image2.png")},
            ],
            "timelines": [{
                "id": "TL1",
                "video": {"width": 1920, "height": 1080, "rateNum": 30, "rateDen": 1},
                "tracks": [{"id": "T1", "visible": True}],
                "clips": [
                    {"id": "c1", "kind": "image", "trackId": "T1", "mediaId": "m1",
                     "start": 0, "duration": 2, "scale": 1.1},
                    {"id": "c2", "kind": "image", "trackId": "T1", "mediaId": "m2",
                     "start": 2, "duration": 3, "scale": 1.0},
                ],
            }],
        }
        self.save()

    def tearDown(self):
        self.tmp.cleanup()

    def save(self):
        (self.project / "concat.json").write_text(json.dumps(self.doc))

    def test_real_native_timeline_multiple_stills(self):
        plan = parse_project(self.project)
        self.assertEqual(plan["duration"], 5)
        self.assertEqual(len(plan["images"]), 2)
        self.assertEqual(plan["skipped"], [])

    def test_skips_optional_effect_with_traceable_log(self):
        self.doc["timelines"][0]["clips"][0]["videoEffects"] = [{"id": "colorgrade", "enabled": True}]
        self.doc["timelines"][0]["clips"][1]["transitionIn"] = {"id": "cross-fade", "duration": 0.3}
        self.save()
        plan = parse_project(self.project)
        self.assertEqual(len(plan["skipped"]), 2)
        self.assertEqual(plan["skipped"][0]["clip_id"], "c1")
        self.assertEqual(plan["skipped"][0]["effect_id"], "colorgrade")
        self.assertEqual(plan["skipped"][1]["reason_code"], "GPU_TRANSITION_UNSUPPORTED")
        self.assertEqual(plan["duration"], 5)
        self.assertTrue((self.project / "concat.json").exists())

    def test_unsafe_gap_or_overlap_fails_not_dropped(self):
        self.doc["timelines"][0]["clips"][1]["start"] = 1.8
        self.save()
        with self.assertRaisesRegex(CUDACompatibilityError, "gap-free"):
            parse_project(self.project)
        self.doc["timelines"][0]["clips"][1]["start"] = 3
        self.save()
        with self.assertRaisesRegex(CUDACompatibilityError, "gap-free"):
            parse_project(self.project)

    def test_real_text_clips_do_not_silently_disappear(self):
        self.doc["timelines"][0]["clips"].append({
            "id": "caption", "kind": "text", "trackId": "T1", "start": 0,
            "duration": 5, "text": {"content": "important words"}
        })
        self.save()
        with self.assertRaisesRegex(CUDACompatibilityError, "essential text"):
            parse_project(self.project)

    def test_missing_source_fails(self):
        self.doc["media"][0]["path"] = str(self.project / "does-not-exist.png")
        self.save()
        with self.assertRaisesRegex(CUDACompatibilityError, "missing or outside"):
            parse_project(self.project)

    def test_path_escape_fails(self):
        outside = Path(self.tmp.name) / "outside.png"
        outside.write_bytes(b"not an actual image")
        self.doc["media"][0]["path"] = str(outside)
        self.save()
        with self.assertRaisesRegex(CUDACompatibilityError, "outside verified"):
            parse_project(self.project)

    def test_unsupported_audio_mixing_fails(self):
        self.doc["timelines"][0]["tracks"].append({"id":"A1"})
        for idx in range(2):
            file = self.project / "media" / f"voice{idx}.m4a"
            file.write_bytes(b"placeholder")
            self.doc["media"].append({"id": f"a{idx}", "path": str(file), "kind":"audio"})
            self.doc["timelines"][0]["clips"].append({
                "id": f"voice{idx}", "kind": "audio", "trackId": "A1",
                "mediaId": f"a{idx}", "start": 0, "duration": 5
            })
        self.save()
        with self.assertRaisesRegex(CUDACompatibilityError, "Multiple audio"):
            parse_project(self.project)

    def test_cuda_graph_has_no_cpu_video_filter_fallback(self):
        clip = parse_project(self.project)["images"][0]
        cmd = segment_command(Path("/tmp/input.yuv"), (1920, 1080), clip,
                              Path("/tmp/out.mp4"), width=1920,
                              height=1080, fps=30, frames=60)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("scale_cuda", graph)
        self.assertIn("overlay_cuda", graph)
        self.assertIn("hwupload_cuda", graph)
        for forbidden in ("zoompan", "xfade", "drawtext", "subtitles", "format=", "scale="):
            self.assertNotIn(forbidden, graph)
        self.assertIn("h264_nvenc", cmd)
        self.assertNotIn("libx264", cmd)

    def test_pan_endpoint_keys_still_run_on_cuda_graph(self):
        self.doc["timelines"][0]["clips"][0]["keys"] = [
            {"property":"offsetX", "at":0.0, "value":-0.04},
            {"property":"offsetX", "at":1.0, "value":0.04},
        ]
        self.save()
        clip = parse_project(self.project)["images"][0]
        cmd = segment_command(Path("/tmp/input.yuv"), (1920,1080), clip,
                              Path("/tmp/out.mp4"), width=1920,height=1080,
                              fps=30,frames=60)
        self.assertIn("min(max(t/2.000000,0),1)", cmd[cmd.index("-filter_complex")+1])

    def test_missing_gpu_produces_explicit_unavailable(self):
        with patch("cloud_render.concat.gpu.cuda_backend.shutil.which", return_value=None):
            d = probe_cuda()
        self.assertFalse(d["cuda_nvenc_ready"])
        self.assertIn("ffmpeg", d["missing"])

    def test_no_fallback_on_unrecognized_animation(self):
        self.doc["timelines"][0]["clips"][0]["keys"] = [
            {"property": "offsetX", "at": 0.5, "value": 0.05}
        ]
        self.save()
        with self.assertRaisesRegex(CUDACompatibilityError, "exactly two endpoint"):
            parse_project(self.project)


if __name__ == "__main__":
    unittest.main()

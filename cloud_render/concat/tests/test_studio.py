"""Offline regression tests for the Concat Studio Z Builder/GPU adapter."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_render.concat.studio.core import (StudioError, assign_media, parse_plan,
                                              parse_srt, build_project)
from cloud_render.concat.studio.gpu import Gateway, GatewayError, origin_only


SAMPLE = """name=Test Movie
aspect=16:9
resolution=1080p
fps=30
00:00.000 --> 00:02.500 | image | first.jpg
00:02.500 --> 00:04.500 | video | clip.mp4 | in=1.000
"""


class StudioParseTests(unittest.TestCase):
    def test_exact_scene_timing_and_media_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "first.jpg").write_bytes(b"photo")
            (root / "clip.mp4").write_bytes(b"video")
            plan = assign_media(parse_plan(SAMPLE), root)
            self.assertEqual((plan.width, plan.height, plan.fps), (1920, 1080, 30))
            self.assertEqual(plan.duration, 4.5)
            self.assertEqual(plan.shots[1].source_in, 1.0)
            self.assertEqual(plan.shots[0].file, root / "first.jpg")

    def test_missing_or_ambiguous_media_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(StudioError):
                assign_media(parse_plan(SAMPLE), root)
            (root / "first.jpg").write_bytes(b"image")
            (root / "clip.mp4").write_bytes(b"video")
            (root / "other").mkdir()
            (root / "other" / "first.jpg").write_bytes(b"image")
            with self.assertRaisesRegex(StudioError, "ambiguous"):
                assign_media(parse_plan(SAMPLE), root)

    def test_gap_overlap_and_unsafe_paths_block(self):
        for source in [
            "00:01.000 --> 00:02.000 | image | first.jpg",
            "00:00.000 --> 00:03.000 | image | first.jpg\n"
            "00:02.000 --> 00:04.000 | image | second.jpg",
            "00:00.000 --> 00:02.000 | image | first.jpg | unknown=value",
        ]:
            with self.assertRaises(StudioError):
                parse_plan(source)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "x.jpg").write_bytes(b"x")
            plan = parse_plan("00:00 --> 00:01 | image | ../x.jpg")
            with self.assertRaisesRegex(StudioError, "filename"):
                assign_media(plan, Path(td))

    def test_srt_timing_and_out_of_range(self):
        text = ("1\n00:00:00,000 --> 00:00:01,000\nHello there\n\n"
                "2\n00:00:01,000 --> 00:00:02,250\nWorld\n")
        captions = parse_srt(text, 2.5)
        self.assertEqual([c["text"] for c in captions], ["Hello there", "World"])
        with self.assertRaises(StudioError):
            parse_srt("1\n00:00:00,000 --> 00:00:03,000\nToo long", 2.5)


class MockMCP:
    def __init__(self, binary, roots):
        self.root = roots[0]
        self.clips = []
        self.media = []
        self.last_id = 0
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def call(self, command, **args):
        self.commands.append((command, args))
        if command == "project_create":
            self.project = Path(args["location"]) / args["name"]
            self.project.mkdir()
            return {"project": {"name": args["name"]}}
        if command in ("timeline_read", "project_open"):
            return {"timeline": {"tracks": [{"id": "T1"}, {"id": "T4"}],
                                 "clips": [dict(c) for c in self.clips]}}
        if command == "media_import":
            mid = "m" + str(len(self.media) + 1)
            self.media.append({"path": args["file"], "id": mid,
                               "duration": 5.0 if args["file"].endswith(".jpg") else 8.0})
            return {"media": [dict(m) for m in self.media]}
        if command == "clip_add":
            self.last_id += 1
            cid = "c" + str(self.last_id)
            media = next(m for m in self.media if m["id"] == args["mediaId"])
            self.clips.append({"id": cid, "start": args["start"], "duration": media["duration"],
                               "sourceStart": 0.0})
            return {"createdId": cid, "timeline": {"clips": [dict(c) for c in self.clips]}}
        if command == "clip_trim":
            clip = next(c for c in self.clips if c["id"] == args["clipId"])
            delta = args["delta"]
            if args["edge"] == "start":
                clip["sourceStart"] += delta
                clip["duration"] -= delta
            else:
                clip["duration"] += delta
            return {"timeline": {"clips": [dict(c) for c in self.clips]}}
        if command == "project_save":
            (self.project / "concat.json").write_text(json.dumps({"media": self.media}))
            return {}
        if command in ("clip_update", "project_close"):
            return {}
        if command == "track_add":
            return {"createdId": "T5"}
        if command == "captions_add":
            return {}
        raise AssertionError(command)


class StudioBuildTests(unittest.TestCase):
    def test_native_mcp_project_and_exact_trim(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "first.jpg").write_bytes(b"photo")
            (folder / "clip.mp4").write_bytes(b"movie")
            plan = assign_media(parse_plan(SAMPLE), folder)
            with patch("cloud_render.concat.studio.core.ConcatMCP", MockMCP):
                dest = build_project(folder / "ignored.exe", plan, folder,
                                     captions=[{"start": 0, "end": 1.0, "text": "Hello"}])
            self.assertTrue((dest / "concat.json").is_file())
            self.assertEqual(dest.name, "Test Movie")
            with self.assertRaisesRegex(StudioError, "already exists"):
                build_project(folder / "ignored.exe", plan, folder)

    def test_unresolved_shot_blocks_prior_to_project_creation(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(StudioError, "Unresolved"):
                build_project(Path("no.exe"), parse_plan(SAMPLE), Path(td))


class GPUContractTests(unittest.TestCase):
    def test_https_origin_and_no_credentials_in_url(self):
        self.assertEqual(origin_only("https://gpu.example.com/"), "https://gpu.example.com")
        for url in ["http://gpu.example.com", "https://user:pw@gpu.example.com",
                    "https://gpu.example.com/path", "https://gpu.example.com/?key=x"]:
            with self.assertRaises(GatewayError):
                origin_only(url)

    def test_nvidia_vulkan_gate(self):
        client = Gateway("https://gpu.example.com", "key", "token")
        with patch.object(client, "diagnose", return_value={
            "nvidia_compute_visible": True, "software_vulkan_visible": True,
            "nvidia_vulkan_visible": False}):
            with self.assertRaisesRegex(GatewayError, "NVIDIA Vulkan"):
                client.require_hardware()
        with patch.object(client, "diagnose", return_value={
            "nvidia_vulkan_visible": True}):
            self.assertTrue(client.require_hardware()["nvidia_vulkan_visible"])


if __name__ == "__main__":
    unittest.main()

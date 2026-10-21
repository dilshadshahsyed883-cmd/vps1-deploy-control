"""Exercise the real worker SML-first acquisition function with mocked providers.

Imports only this function AST, so unit tests need not install CUDA, ffmpeg
or other production-only Scene rendering dependencies.
"""
from __future__ import annotations

import ast
import hashlib
import shutil
import tempfile
import typing
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps/scene-builder/part02/api_server.py"
source_ast = ast.parse(SOURCE.read_text(encoding="utf-8"))
fn = next(node for node in source_ast.body if isinstance(node, ast.FunctionDef)
          and node.name == "_try_sml_first_scene")


class OfflineSML(Exception):
    pass


class FakeClient:
    enabled = True

    def __init__(self):
        self.events = []
        self.offline = False
        self.payload = b"valid synthetic media"

    def ranked(self, query, *, used_ids=None):
        self.events.append(("rank", query))
        return [{"id": "CLP-A123", "type": "video",
                 "content_url": "/api/v1/media/video/CLP-A123/content",
                 "duration": 3.0}]

    def reserve(self, job_id, scene_id, clip_id, used_ids):
        self.events.append(("reserve", scene_id, clip_id))
        return "RES-" + "a" * 32

    def download(self, clip, path):
        self.events.append(("download", clip["id"]))
        if self.offline:
            raise OfflineSML("SML offline")
        path.write_bytes(self.payload)

    def transition(self, reservation, action):
        self.events.append(("transition", action))


def load_acquisition():
    def working_clip(source, target, seconds, **kwargs):
        shutil.copyfile(source, target)
        return {"source_duration": 3.0, "working_duration": seconds, "trimmed": False}
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    globals_map = {
        "SMLWorkerClient": FakeClient,
        "SMLMediaUnavailable": OfflineSML,
        "Any": typing.Any, "Path": Path, "uuid": uuid,
        "_scene_duration": lambda scene: float(scene.get("end", 3)) - float(scene.get("start", 0)),
        "sha256_file": lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest(),
        "validate_media": lambda path, kind, seconds, **kw: {
            "handling": "use", "duration": 3.0},
        "build_working_video_clip": working_clip,
        "working_clip_target_duration": lambda duration: duration,
    }
    exec(compile(module, str(SOURCE), "exec"), globals_map)
    return globals_map["_try_sml_first_scene"]


class WorkerAcquisitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.acquire = staticmethod(load_acquisition())

    def run_acquisition(self, client, source_dir, **scene):
        root = Path(source_dir)
        sources, resolved = root / "source", root / "resolved"
        sources.mkdir(exist_ok=True)
        resolved.mkdir(exist_ok=True)
        used, hashes = set(), set()
        output = self.acquire(
            client=client, job_id="JOB-1", scene={
                "_sml_first": True, "pexels_allow_video": True, "start": 0, "end": 3,
                **scene,
            }, scene_code="SCENE_001", keywords=["snow mountain"],
            assignment_root=resolved, source_root=sources, used_sml_ids=used,
            used_hashes=hashes,
        )
        return output, used, hashes

    def test_success_commits_after_local_media_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            result, used, hashes = self.run_acquisition(client, folder)
            self.assertEqual(result["provider"], "sml")
            self.assertEqual(result["source_type"], "SML")
            self.assertEqual(result["asset_id"], "CLP-A123")
            self.assertEqual(Path(result["media_path"]).read_bytes(), client.payload)
            self.assertEqual(used, {"CLP-A123"})
            self.assertEqual(len(hashes), 1)
            events = client.events
            self.assertLess(events.index(("download", "CLP-A123")),
                            events.index(("transition", "commit")))

    def test_unavailable_sml_returns_none_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            client.offline = True
            result, used, _ = self.run_acquisition(client, folder)
            self.assertIsNone(result)
            self.assertFalse(used)
            self.assertIn(("transition", "release"), client.events)
            self.assertFalse(list((Path(folder) / "resolved").glob("*.mp4")))

    def test_disabled_scene_never_contacts_sml(self):
        with tempfile.TemporaryDirectory() as folder:
            client = FakeClient()
            result, _, _ = self.run_acquisition(client, folder, _sml_first=False)
            self.assertIsNone(result)
            self.assertEqual(client.events, [])


if __name__ == "__main__":
    unittest.main()

"""Parallel SML acquisition regressions without production imports or credentials."""
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "apps/scene-builder/part02/api_server.py"
SOURCE = WORKER.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def isolated(*names):
    selected = [n for n in TREE.body
                if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    if len(selected) != len(names):
        raise AssertionError("Missing isolated worker functions/classes")
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
              *selected], type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, str(WORKER), "exec")


class NotAvailable(Exception):
    pass


class FakeSML:
    enabled = True

    def __init__(self, *, count=5, on_download=None, bytes_by_id=None):
        self.items = [
            {"id": f"IMG-{i}", "type": "image", "source_filename": f"clip_{i}.jpg"}
            for i in range(count)
        ]
        self.on_download = on_download
        self.bytes_by_id = bytes_by_id or {}
        self._mutex = threading.Lock()
        self.reserved = []
        self.transitions = []

    def ranked(self, query, *, used_ids=None, media_type="video"):
        assert media_type == "image", "Fixture supports image-only"
        return [row for row in self.items if row["id"] not in (used_ids or set())]

    def reserve(self, job_id, scene_code, media_id, used_ids=None, media_type="video"):
        assert media_type == "image"
        assert media_id not in (used_ids or set())
        with self._mutex:
            self.reserved.append((scene_code, media_id))
        return f"RES-{scene_code}-{media_id}"

    def download(self, candidate, destination):
        if self.on_download:
            self.on_download(candidate)
        destination.write_bytes(self.bytes_by_id.get(candidate["id"], candidate["id"].encode()))
        return "fixture"

    def transition(self, reservation, action):
        with self._mutex:
            self.transitions.append((reservation, action))


class SceneParallelSMLTests(unittest.TestCase):
    def make_env(self, client, *, cancelled=None, max_workers=5):
        env = {
            "Path": Path, "SMLWorkerClient": FakeSML, "SMLMediaUnavailable": NotAvailable,
            "threading": threading, "shutil": shutil, "uuid": uuid,
            "ThreadPoolExecutor": ThreadPoolExecutor, "IMAGE_EXTENSIONS": {".jpg", ".png"},
            "SCENE_MEDIA_SEARCH_WORKERS": max_workers,
            "SCENE_SML_DOWNLOAD_WORKERS": max_workers,
            "_SML_DOWNLOAD_SLOTS": threading.BoundedSemaphore(max_workers),
            "_scene_duration": lambda scene: 3.0,
            "should_continue_previous_visual": lambda duration: False,
            "_scene_keywords": lambda scene: ["keyword"],
            "_scene_media_type_order": lambda scene: ["video", "image"],
            "_sml_media_type_order": lambda scene: ["image"],
            "sha256_file": lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "validate_media": lambda *args, **kwargs: {"handling": "ready", "duration": 0},
            "working_clip_target_duration": lambda duration: duration,
            "cancelled": cancelled or (lambda job_id: False),
            "update_job": lambda *args, **kwargs: None,
            "search_multi_provider": lambda *args, **kwargs: [],
        }
        exec(isolated("_SMLParallelReservations", "_try_sml_first_scene",
                      "_parallel_sml_scene_download", "_prefetch_first_scene_lookup",
                      "_parallel_scene_queries"), env)
        return env

    def test_five_simultaneous_real_file_downloads_distinct_assets_and_scene_order(self):
        barrier = threading.Barrier(5, timeout=8)
        active, peak = [0], [0]
        mutex = threading.Lock()

        def download(candidate):
            with mutex:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                barrier.wait()
            finally:
                with mutex:
                    active[0] -= 1

        client = FakeSML(count=5, on_download=download)
        env = self.make_env(client)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder); destination = target / "assigned"; origin = target / "sources"
            destination.mkdir(); origin.mkdir()
            scenes = [{"scene_code": f"SCENE_{i+1:03d}", "keyword": "football",
                       "_sml_first": True, "_sml_only": True, "pexels_enabled": False}
                      for i in range(5)]
            used_ids, used_hashes = set(), set()
            got = list(env["_parallel_scene_queries"](
                "job", scenes, client, 1280, 720,
                assignment_root=destination, source_root=origin,
                used_sml_ids=used_ids, used_hashes=used_hashes))
            resolved = [lookup[3] for _, _, lookup in got]
            self.assertEqual(peak[0], 5)
            self.assertEqual([i for i, _, _ in got], list(range(5)))
            self.assertTrue(all(lookup[0] == "sml_acquired" for _, _, lookup in got))
            self.assertEqual(len(used_ids), 5)
            self.assertEqual(len(used_hashes), 5)
            self.assertEqual(len({row["asset_id"] for row in resolved}), 5)
            self.assertTrue(all(row["status"] == "matched_stock" for row in resolved))
            for row in resolved:
                self.assertEqual(Path(row["media_path"]).read_bytes(),
                                 row["asset_id"].encode())
            self.assertEqual(sum(action == "commit" for _, action in client.transitions), 5)
            self.assertEqual(sum(action == "release" for _, action in client.transitions), 0)

    def test_duplicate_checksums_release_and_retry_another_asset(self):
        # A and B have different approved SML IDs but identical bytes.
        client = FakeSML(count=3, bytes_by_id={"IMG-0": b"duplicate", "IMG-1": b"duplicate",
                                              "IMG-2": b"other"})
        env = self.make_env(client, max_workers=2)
        with tempfile.TemporaryDirectory() as folder:
            dst=Path(folder)/"dst"; src=Path(folder)/"src"; dst.mkdir();src.mkdir()
            used_ids, used_hashes = set(), set()
            got=list(env["_parallel_scene_queries"](
                "job", [{"scene_code": "ONE", "_sml_first": True},
                        {"scene_code": "TWO", "_sml_first": True}],
                client, 1280, 720, assignment_root=dst, source_root=src,
                used_sml_ids=used_ids, used_hashes=used_hashes))
            self.assertEqual(len(used_ids), 2)
            self.assertEqual(len(used_hashes), 2)
            self.assertEqual(len({x[2][3]["media_hash"] for x in got}), 2)
            self.assertTrue(any(action == "release" for _, action in client.transitions))

    def test_cancel_before_commit_releases_reservation_and_temp_file(self):
        cancelled = threading.Event()
        def download(candidate):
            cancelled.set()
        client = FakeSML(count=1, on_download=download)
        env = self.make_env(client, cancelled=lambda job_id: cancelled.is_set())
        with tempfile.TemporaryDirectory() as folder:
            dst=Path(folder)/"dst";src=Path(folder)/"src";dst.mkdir();src.mkdir()
            with self.assertRaises(RuntimeError):
                list(env["_parallel_scene_queries"](
                    "job", [{"scene_code": "ONE", "_sml_first": True}],
                    client, 1280, 720, assignment_root=dst, source_root=src,
                    used_sml_ids=set(), used_hashes=set()))
            self.assertIn("release", [action for _, action in client.transitions])
            self.assertEqual(list(dst.iterdir()), [])
            self.assertEqual(list(src.iterdir()), [])

    def test_approved_and_tiny_skipped_and_existing_sml_only_policy_preserved(self):
        client=FakeSML()
        env=self.make_env(client)
        env["should_continue_previous_visual"] = lambda dur: True
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"approved.jpg";path.write_bytes(b"approved")
            scenes=[{"scene_code":"ONE","review_approved":True,"review_locked":True,
                     "media_path":str(path),"_sml_first":True},
                    {"scene_code":"TWO","_sml_first":True}]
            rows=list(env["_parallel_scene_queries"]("job",scenes,client,1280,720))
            self.assertEqual([lookup for _,_,lookup in rows], [None,None])
        resolver = next(n for n in TREE.body if isinstance(n, ast.FunctionDef)
                        and n.name == "_resolve_scene_stock_media")
        source=ast.get_source_segment(SOURCE,resolver)
        self.assertIn("strict_missing",source)
        self.assertIn("batch_lookup[0] == \"sml_acquired\"",source)
        self.assertIn("reviewed_locked",source)
        self.assertIn("continue_previous",source)
        self.assertIn("_sml_fallback",source)
        self.assertIn("def _delete_cancelled_drive_output(", SOURCE)


if __name__ == "__main__":
    unittest.main()

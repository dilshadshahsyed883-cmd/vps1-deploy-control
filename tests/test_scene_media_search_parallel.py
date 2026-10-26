"""Offline tests for bounded five-way Scene Builder stock search.

AST loading avoids importing the production worker and touching its SQLite/data.
Only the read-only lookup and bounded scheduling functions execute with stubs.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "apps/scene-builder/part02/api_server.py"
SOURCE = WORKER.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def extracted(*names):
    nodes = [n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(nodes) == len(names), f"Functions missing: {names}"
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *nodes,
    ], type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, str(WORKER), "exec")


class SceneMediaSearchConcurrencyTests(unittest.TestCase):
    def make_env(self, search, *, sml_enabled=False):
        class SML:
            enabled = sml_enabled

            def ranked(self, query, *, used_ids=None, media_type="video"):
                return [{"id": query, "type": media_type}]

        env = {
            "ThreadPoolExecutor": ThreadPoolExecutor,
            "SCENE_MEDIA_SEARCH_WORKERS": 5,
            "Path": Path,
            "SMLWorkerClient": SML,
            "_current_pexels_keys": lambda: ["test-key"],
            "_scene_keywords": lambda scene: [scene["keyword"]],
            "_scene_media_type_order": lambda scene: ["video", "image"],
            "_sml_media_type_order": lambda scene: ["image", "video"],
            "_scene_duration": lambda scene: 3.0,
            "should_continue_previous_visual": lambda duration: False,
            "cancelled": lambda job_id: False,
            "update_job": lambda *args, **kwargs: None,
            "search_multi_provider": search,
        }
        exec(extracted("_prefetch_first_scene_lookup", "_parallel_scene_queries"), env)
        return env

    def test_five_actual_inflight_pexels_searches_and_ordered_seven_scene_results(self):
        barrier = threading.Barrier(5, timeout=8)
        mutex = threading.Lock()
        active, peak, started = [0], [0], [0]

        def search(query, kind, *args, **kwargs):
            with mutex:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                started[0] += 1
                first_batch = started[0] <= 5
            try:
                if first_batch:
                    barrier.wait()
                return [{"asset_id": query, "type": kind}]
            finally:
                with mutex:
                    active[0] -= 1

        env = self.make_env(search)
        scenes = [{"keyword": f"scene_{i}", "pexels_enabled": True} for i in range(7)]
        got = list(env["_parallel_scene_queries"]("job", scenes, env["SMLWorkerClient"](), 1280, 720))
        self.assertEqual(peak[0], 5)
        self.assertEqual(started[0], 7)
        self.assertEqual([index for index, _, _ in got], list(range(7)))
        self.assertEqual([lookup[3][0]["asset_id"] for _, _, lookup in got],
                         [f"scene_{i}" for i in range(7)])

    def test_locked_and_tiny_scenes_skip_unnecessary_search(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as temporary:
            file = Path(temporary) / "approved.jpg"
            file.write_bytes(b"fixture")
            hits = []
            def search(query, kind, *args, **kwargs):
                hits.append(query)
                return []
            env = self.make_env(search)
            env["should_continue_previous_visual"] = lambda duration: duration < 1.0
            env["_scene_duration"] = lambda scene: scene.get("duration", 3.0)
            scenes = [
                {"keyword": "locked", "review_approved": True, "review_locked": True,
                 "media_path": str(file)},
                {"keyword": "tiny", "duration": 0.5},
                {"keyword": "normal"},
            ]
            out = list(env["_parallel_scene_queries"]("job", scenes, env["SMLWorkerClient"](), 1280, 720))
            self.assertEqual(hits, ["normal"])
            self.assertEqual([x[2] is None for x in out], [True, True, False])

    def test_sml_first_searches_image_without_performing_reservations(self):
        def forbidden_pexels(*args, **kwargs):
            raise AssertionError("SML-first must not issue a Pexels prefetch")
        env = self.make_env(forbidden_pexels, sml_enabled=True)
        scenes = [{"keyword": "football", "_sml_first": True}]
        results = list(env["_parallel_scene_queries"]("job", scenes, env["SMLWorkerClient"](), 1280, 720))
        lookup = results[0][2]
        self.assertEqual(lookup[:3], ("sml", "image", "football"))
        self.assertEqual(lookup[3], [{"id": "football", "type": "image"}])

    def test_provider_error_omits_secrets(self):
        def bad_search(*args, **kwargs):
            raise RuntimeError("test-secret-token")
        env = self.make_env(bad_search)
        lookup = env["_prefetch_first_scene_lookup"]({}, "pexels", "video", "test", 1280, 720)
        self.assertEqual(lookup[4], "RuntimeError")
        self.assertNotIn("test-secret-token", repr(lookup))

    def test_resolver_consumes_prefetch_but_keeps_serial_media_assignment(self):
        resolver = next(n for n in TREE.body if isinstance(n, ast.FunctionDef)
                        and n.name == "_resolve_scene_stock_media")
        calls = [n for n in ast.walk(resolver) if isinstance(n, ast.Call)]
        self.assertTrue(any(isinstance(n.func, ast.Name)
                            and n.func.id == "_parallel_scene_queries" for n in calls))
        pexels_calls = [n for n in calls if isinstance(n.func, ast.Name)
                        and n.func.id == "_download_scene_candidate"]
        sml_calls = [n for n in calls if isinstance(n.func, ast.Name)
                     and n.func.id == "_try_sml_first_scene"]
        self.assertTrue(any("prefetched_search" in {kw.arg for kw in n.keywords} for n in pexels_calls))
        self.assertTrue(any("prefetched_ranked" in {kw.arg for kw in n.keywords} for n in sml_calls))
        caps = [n for n in TREE.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "SCENE_MEDIA_SEARCH_WORKERS" for t in n.targets)]
        self.assertEqual(len(caps), 1)
        self.assertIsInstance(caps[0].value, ast.Call)
        self.assertEqual(caps[0].value.func.id, "min")
        self.assertEqual(caps[0].value.args[0].value, 5)


if __name__ == "__main__":
    unittest.main()

"""Scene Builder visual audit ~30 second navigation and scoped approval invariants."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "apps/scene-builder/part02/api_server.py").read_text()
WEB = (ROOT / "apps/scene-builder/part01/app.py").read_text()
UI = (ROOT / "apps/scene-builder/part01/templates/audit_media.html").read_text()


def batch_function():
    module = ast.parse(WORKER)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_audit_batch_ranges")
    namespace = {"Any": object, "AUDIT_WINDOW_SECONDS": 120.0}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<audit-batches>", "exec"), namespace)
    return namespace["_audit_batch_ranges"]


class SmartAuditBatchTests(unittest.TestCase):
    def test_scene_ends_snap_to_nearest_boundary_with_three_seconds_tolerance(self):
        calculate = batch_function()
        rows = [
            {"item_id": "s1", "final_start": 0, "final_end": 31},
            {"item_id": "s2", "final_start": 31, "final_end": 62},
            {"item_id": "s3", "final_start": 62, "final_end": 90},
            {"item_id": "s4", "final_start": 90, "final_end": 120},
        ]
        batches = calculate(rows, 120)
        self.assertEqual([(b["start"], b["end"]) for b in batches],
                         [(0.0, 31.0), (31.0, 62.0), (62.0, 90.0), (90.0, 120.0)])
        self.assertEqual([b["item_ids"] for b in batches], [["s1"], ["s2"], ["s3"], ["s4"]])

    def test_fallback_does_not_duplicate_scene_crossing_boundary(self):
        calculate = batch_function()
        scenes = [
            {"item_id": "a", "final_start": 0, "final_end": 35},
            {"item_id": "b", "final_start": 35, "final_end": 70},
            {"item_id": "c", "final_start": 70, "final_end": 120},
        ]
        batches = calculate(scenes, 120)
        self.assertEqual([b["end"] for b in batches], [30.0, 60.0, 90.0, 120.0])
        ids = [scene for batch in batches for scene in batch["item_ids"]]
        self.assertCountEqual(ids, ["a", "b", "c"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_final_short_batch_and_no_scene_render_changes(self):
        calculate = batch_function()
        rows = [{"item_id": "one", "final_start": 0, "final_end": 28},
                {"item_id": "two", "final_start": 28, "final_end": 49}]
        batches = calculate(rows, 49)
        self.assertEqual([(b["start"], b["end"]) for b in batches], [(0.0, 28.0), (28.0, 49.0)])
        self.assertEqual(rows, [{"item_id": "one", "final_start": 0, "final_end": 28},
                                {"item_id": "two", "final_start": 28, "final_end": 49}])

    def test_backend_requires_authorization_and_batch_scopes_approval(self):
        self.assertIn('def visual_audit_approve_batch(', WORKER)
        self.assertIn('with _audit_lock(job_id)', WORKER)
        self.assertIn('selected = set(batches[batch_index]["item_ids"])', WORKER)
        self.assertIn('if Path(str(item.get("media_path") or "")).is_file() and not bool(item.get("approved")):', WORKER)
        self.assertIn('response["batch_approved_count"] = changed', WORKER)
        self.assertIn('def api_audit_approve_batch(', WEB)
        self.assertIn('require_job_access(request, job_id)', WEB)
        self.assertIn('/jobs/{job_id}/audit/media/batches/{batch_index}/approve', WEB)

    def test_existing_project_review_actions_survive_lazy_batch_ui(self):
        for symbol in ('id="audit-approve-all"', 'id="audit-continue"',
                       'id="audit-approve-batch"', 'id="audit-batch-next"',
                       'id="audit-batch-previous"', 'data-action="scene-local"',
                       'data-action="keyword"', 'data-action="replace"',
                       'data-action="upload"', 'data-action="approve"',
                       '/scene-library-select', '/scene-library-auto-match', '/approve-all', '/continue'):
            self.assertIn(symbol, UI)
        self.assertNotIn('data-action="sml"', UI)
        self.assertNotIn('/sml-select', UI)
        self.assertIn('const included=new Set(current.item_ids||[])', UI)
        self.assertIn('grid.innerHTML=items.map(card).join', UI)
        self.assertIn("audit.batches", UI)


if __name__ == "__main__":
    unittest.main()

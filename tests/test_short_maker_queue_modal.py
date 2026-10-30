"""Short Maker queue UI regression: compact rows, modal outputs, backend unchanged."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps/short-maker/part01/app.py"


class ShortMakerQueueDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(cls.source)
        cls.html = next(node.value.value for node in tree.body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "HTML" for t in node.targets))

    def test_compact_queue_output_count_instead_of_inline_video_links(self):
        js = self.html.split("function renderQueue(){", 1)[1].split("async function loadQueue()", 1)[0]
        self.assertIn("queue-output-count", js)
        self.assertIn("shortJobDestination(j)", js)
        self.assertIn("data-short-job-details=", js)
        self.assertIn("data-short-job-delete=", js)
        self.assertNotIn("outputs||[]).map(o=>o.local_available", js)
        self.assertNotIn("outs=", js)
        self.assertNotIn('inline onclick=', js)
        self.assertIn("refreshShortJobDetails();", self.html)

    def test_modal_contains_accessible_and_scrollable_outputs(self):
        for marker in ('id="shortJobModal"', 'aria-modal="true"',
                       'aria-labelledby="shortJobTitle"', 'id="shortJobSummary"',
                       'id="shortJobOutputList"', 'role="list"', 'role="listitem"',
                       'id="copyShortJobNames"', 'id="closeShortJobModal"',
                       ".short-job-overlay[hidden]{display:none!important}",
                       ".short-job-output-list{overflow:auto",
                       "shortJobModal.addEventListener('click'",
                       "event.key==='Escape'", "event.key!=='Tab'",
                       "shortJobOutputList.replaceChildren(fragment)",
                       "shortJobOutputSignature"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.html)

    def test_safe_output_links_and_no_new_render_endpoints(self):
        self.assertIn("function shortJobDownloadHref(value)", self.html)
        self.assertIn("link.origin!==location.origin", self.html)
        self.assertIn("link.pathname.startsWith('/short-maker/outputs/')", self.html)
        self.assertIn("name.textContent=", self.html)
        self.assertIn("content.textContent=", self.html)
        self.assertIn("copyShortJobButton.addEventListener('click'", self.html)
        self.assertIn("function render_job(job_id: str, cfg: dict):", self.source)
        self.assertIn("executor.submit(render_job, job_id, cfg)", self.source)


if __name__ == "__main__":
    unittest.main()

"""Offline, no-upload Video Merger two-column and durable queue regressions."""
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "apps/video-merger/part01/templates/index.html"
APP = ROOT / "apps/video-merger/part01/app.py"


class VideoMergerLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = HTML.read_text(encoding="utf-8")

    def test_subtab_order_and_separate_queue_panel(self):
        page = self.page
        positions = [page.index('data-tab="' + value + '"') for value in
                     ("merge", "queue", "library", "proxy")]
        self.assertEqual(positions, sorted(positions))
        for value in ("merge", "queue", "library", "proxy"):
            self.assertEqual(page.count('id="tab-' + value + '"'), 1)
            self.assertIn('aria-controls="tab-' + value + '"', page)
        merge = page.split('id="tab-merge"', 1)[1].split('id="tab-queue"', 1)[0]
        queue = page.split('id="tab-queue"', 1)[1].split('id="tab-library"', 1)[0]
        self.assertIn('class="vm-merge-layout"', merge)
        self.assertIn('class="vm-process-column"', merge)
        self.assertIn('class="vm-settings-column"', merge)
        self.assertNotIn('id="queue"', merge)
        self.assertNotIn('id="pauseBtn"', merge)
        self.assertIn('id="queue"', queue)
        self.assertIn('id="pauseBtn"', queue)
        self.assertIn('id="sTotal"', queue)
        self.assertIn('id="sWaiting"', queue)
        self.assertIn('id="sActive"', queue)
        self.assertIn('id="sComplete"', queue)

    def test_existing_processing_and_delivery_controls_preserved(self):
        page = self.page
        for control in ("fname", "librarySelect", "urlVideos", "aspect",
                        "quality", "resolution", "fit", "uploadDrive",
                        "addCopyrightFolder", "folder", "queueMergeButton",
                        "libraryList", "proxyList", "queue"):
            self.assertEqual(page.count('id="' + control + '"'), 1, control)
        self.assertIn('grid-template-columns:minmax(0,3fr) minmax(0,2fr)', page)
        self.assertIn('@media(max-width:1040px)', page)
        self.assertIn('showTab(\'queue\')', page)
        self.assertIn("async function cancelJob(id)", page)
        self.assertIn("async function retryJob(id)", page)
        self.assertIn('/api/jobs/${encodeURIComponent(j.id)}/view', page)
        self.assertIn("'/api/jobs/'+id+'/cancel'", page)
        self.assertIn("'/api/jobs/'+id+'/retry'", page)
        self.assertIn("activeStatus(s){return !['waiting','complete','failed','cancelled']", page)

    def test_inline_javascript_syntax(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available")
        scripts = re.findall(r"<script>(.*?)</script>", self.page, re.S)
        self.assertGreaterEqual(len(scripts), 2)
        for script in scripts:
            result = subprocess.run([node, "--check", "-"], input=script,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)


class VideoMergerQueueRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        with patch.dict(os.environ, {
            "VIDEO_MERGER_ROOT": str(root),
            "VIDEO_MERGER_PROXY_CONFIG_DIR": str(root / "proxy"),
        }):
            spec = importlib.util.spec_from_file_location("vm_queue_layout_test", APP)
            self.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.app.init_db()
        with self.app.db() as c:
            now = self.app.utcnow()
            c.execute(
                "INSERT INTO library_items(id,name,filename,created_at,updated_at)"
                " VALUES(?,?,?,?,?)",
                ("librarytest", "Existing Library Video", "original.mp4", now, now),
            )

    def queue(self, owner="alice"):
        return self.app.add_job({
            "videos": [
                {"kind": "library", "library_id": "librarytest"},
                {"kind": "url", "url": "https://example.com/video.mp4"},
            ],
            "aspect": "9:16", "quality": "1080p",
            "fit_mode": "crop", "filename_mode": "manual",
            "filename": "manual-title.mp4",
            "folder": "", "upload_to_drive": False,
            "add_to_copyright_folder": False,
        }, owner=owner)[0]

    def test_authenticated_local_video_view_uses_existing_output_guard(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn("([A-Za-z0-9]+)/(download|view)", source)
        self.assertIn('if m.group(2)=="view" else "attachment"', source)
        self.assertIn("str(job.get('local_path') or '')", source)
        self.assertIn("job.get('owner_username')", source)

    def test_cancel_waiting_persists_and_unpins_library(self):
        jid = self.queue()
        self.assertTrue(self.app.library_in_use("librarytest"))
        with self.assertRaisesRegex(ValueError, "current account"):
            self.app.cancel_job(jid, owner="bob")
        self.app.cancel_job(jid, owner="alice")
        self.assertEqual(self.app.get_job(jid)["status"], "cancelled")
        self.assertEqual(self.app.get_job(jid)["progress"], 0)
        self.assertIsNone(self.app.next_waiting())
        self.assertFalse(self.app.library_in_use("librarytest"))
        self.app.delete_job(jid, owner="alice")
        self.assertIsNone(self.app.get_job(jid))

    def test_claim_blocks_cancel_and_delete(self):
        jid = self.queue()
        claimed = self.app.next_waiting()
        self.assertEqual(claimed["id"], jid)
        self.assertEqual(self.app.get_job(jid)["status"], "processing")
        with self.assertRaisesRegex(ValueError, "waiting"):
            self.app.cancel_job(jid, owner="alice")
        with self.assertRaisesRegex(ValueError, "Active"):
            self.app.delete_job(jid, owner="alice")

    def test_failed_retry_is_owner_scoped_idempotent_and_keeps_old_record(self):
        jid = self.queue()
        self.app.update_job(jid, status="failed", progress=100, error="Source failed")
        old = self.app.get_job(jid)
        path = self.app.LOCAL_OUTPUT_ROOT / jid / "manual-title.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"preserve verified existing bytes")
        with self.assertRaisesRegex(ValueError, "current account"):
            self.app.retry_job(jid, owner="bob")
        child_id, child_name = self.app.retry_job(jid, owner="alice")
        self.assertNotEqual(jid, child_id)
        self.assertEqual(child_name, "manual-title.mp4")
        self.assertEqual(self.app.retry_job(jid, owner="alice")[0], child_id)
        child = self.app.get_job(child_id)
        self.assertEqual(child["status"], "waiting")
        self.assertEqual(child["retry_of"], jid)
        self.assertEqual(child["videos_json"], old["videos_json"])
        self.assertEqual(child["drive_enabled"], old["drive_enabled"])
        self.assertEqual(child["copyright_enabled"], old["copyright_enabled"])
        self.assertEqual(self.app.get_job(jid)["status"], "failed")
        self.assertEqual(path.read_bytes(), b"preserve verified existing bytes")
        self.assertEqual(len(self.app.list_jobs("alice")), 2)

    def test_queue_migration_is_repeatable_without_losing_jobs(self):
        jid = self.queue()
        self.app.init_db()
        self.assertEqual(self.app.get_job(jid)["id"], jid)
        with self.app.db() as c:
            cols = {row["name"] for row in c.execute("PRAGMA table_info(jobs)")}
        self.assertIn("retry_of", cols)

    def test_other_account_cannot_reorder_queued_job(self):
        jid = self.queue()
        with self.assertRaisesRegex(ValueError, "waiting"):
            self.app.move_job(jid, "up", owner="bob")
        self.assertEqual(self.app.get_job(jid)["status"], "waiting")


class VideoMergerReleaseOverlayTests(unittest.TestCase):
    """The guarded release may update only the reviewed Merge panel and JS."""

    def pages(self):
        controls = ("fname", "librarySelect", "urlVideos", "aspect", "quality",
                    "resolution", "fit", "uploadDrive", "addCopyrightFolder",
                    "folder", "queueMergeButton")
        fields = "".join('<input id="' + name + '">' for name in controls)
        baseline_panel = ('  <div class="tabs">Original tabs</div>\n'
                          '  <div id="tab-merge">' + fields + '</div>\n')
        new_panel = ('  <div class="tabs">Merge Queue Library Proxy</div>\n'
                     '  <div id="tab-merge" class="vm-merge-layout">' + fields + '</div>\n'
                     '  <div id="tab-queue"><div id="queue"></div></div>\n')
        original_js = (
            "function showTab(tab){old}\n"
            "async function queueMerge(){old}\n"
            "function activeStatus(s){old}\n"
            "function renderQueue(){old}\n"
            "async function del(id){keep}\n"
        )
        revised_js = (
            "function showTab(tab){new}\n"
            "async function queueMerge(){new}\n"
            "function activeStatus(s){new}\n"
            "function renderQueue(){new}\n"
            "async function cancelJob(id){cancel}\n"
            "async function retryJob(id){retry}\n"
            "async function del(id){keep}\n"
        )
        stable_tail = ('  <div id="tab-library">Production Library preserved</div>\n'
                       '  <div id="tab-proxy">Production Proxy preserved</div>\n')
        before = '<style>Existing CSS\n</style>\n' + baseline_panel + stable_tail + original_js
        after = ('<style>Existing CSS\n/* VIDEO_MERGER_TWO_COLUMN_QUEUE_V2 */\n'
                 'New CSS\n</style>\n' + new_panel + stable_tail + revised_js)
        return before, after

    def test_release_overlay_preserves_unrelated_live_html(self):
        from production.video_merger_import_release import overlay_queue_layout_ui
        before, source = self.pages()
        live = before.replace('Production Library preserved',
                              'Production Library with independent active change')
        result = overlay_queue_layout_ui(live, source, before)
        self.assertIn('Production Library with independent active change', result)
        self.assertIn('Production Proxy preserved', result)
        self.assertIn('id="tab-queue"', result)
        self.assertIn('async function cancelJob(id)', result)
        self.assertIn('async function retryJob(id)', result)
        self.assertIn('/* VIDEO_MERGER_TWO_COLUMN_QUEUE_V2 */', result)

    def test_release_overlay_blocks_reviewed_panel_drift(self):
        from production.video_merger_import_release import overlay_queue_layout_ui
        before, source = self.pages()
        drift = before.replace('Original tabs', 'Unreviewed live UI change')
        with self.assertRaisesRegex(RuntimeError, 'drift'):
            overlay_queue_layout_ui(drift, source, before)


if __name__ == "__main__":
    unittest.main()

"""SML-YT-QUEUE-001: YouTube imports appear in the visible, durable queue."""
import ast
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"


class VisibleQueue(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details = 0
        self.visible = False
        self.ids = []

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.details += 1
        for key, value in attrs:
            if key == "id":
                self.ids.append(value)
                if value == "importHistory":
                    self.visible = self.details == 0

    def handle_endtag(self, tag):
        if tag == "details":
            self.details -= 1


class ProductionPackaging(unittest.TestCase):
    def test_production_catalog_installs_ytdlp_in_sml_venv(self):
        from production.runtime import CATALOG
        packages = CATALOG["self-media-library"]["packages"]
        dependency = [value for value in packages if value.startswith("yt-dlp")]
        self.assertEqual(dependency, ["yt-dlp[default]==2026.8.19"])
        requirements = (ROOT / "requirements.txt").read_text()
        self.assertIn(dependency[0], requirements)
        self.assertNotIn("yt-dlp", CATALOG["short-maker"]["packages"])

    def test_release_checks_real_downloader_before_switching_pointer(self):
        source = (ROOT.parents[2] / "production/runtime.py").read_text()
        before_pointer = source.index("    journal = BASE / 'transactions'")
        probe = source.index("        downloader = venv / 'bin/yt-dlp'")
        self.assertLess(probe, before_pointer)
        for token in ("PASS_DEPENDENCY_INSTALLED", "-m', 'yt_dlp', '--version",
                      "SML_YTDLP_MISSING"):
            self.assertIn(token, source)


class Contracts(unittest.TestCase):
    def test_queue_is_not_collapsed_and_refreshes(self):
        page = VisibleQueue()
        page.feed((ROOT / "static/index.html").read_text())
        self.assertTrue(page.visible)
        self.assertEqual(page.ids.count("importHistory"), 1)
        self.assertEqual(page.ids.count("refreshImports"), 1)
        js = (ROOT / "static/app.js").read_text()
        for token in ("renderImports(items,", "data-import-retry", "item.progress",
                      "await loadImports()", "if(state.page==='sources')loadImports()"):
            self.assertIn(token, js)

    def test_ytdlp_percent_comes_from_actual_output(self):
        tree = ast.parse((ROOT / "media_extensions.py").read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "parse_ytdlp_progress")
        namespace = {"re": re}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "progress", "exec"), namespace)
        parse = namespace["parse_ytdlp_progress"]
        self.assertEqual(parse("download: 43.5%"), 43)
        self.assertEqual(parse("[download] download:100%"), 100)
        self.assertIsNone(parse("connecting to YouTube"))
        self.assertIsNone(parse("unrelated 90%"))

    def test_proxy_direct_retry_requires_explicit_user_action(self):
        html = (ROOT / "static/index.html").read_text()
        js = (ROOT / "static/app.js").read_text()
        self.assertIn('id="importProxyState"', html)
        for token in ("proxy_configured", "data-import-direct", "Retry direct",
                      "direct_without_proxy:direct", "confirm(", "proxyUnavailable"):
            self.assertIn(token, js)
        backend = (ROOT / "media_extensions.py").read_text()
        self.assertIn("class ImportRetryRequest", backend)
        self.assertIn("if body.use_proxy and not sml_proxy_configured()", backend)

    def test_durable_progress_schema_and_retry(self):
        py = (ROOT / "media_extensions.py").read_text()
        for token in ("ALTER TABLE media_imports ADD COLUMN progress", "progress=100",
                      "status='queued',progress=0", "status='processing'",
                      "progress,source_id,created_at,updated_at"):
            self.assertIn(token, py)


@unittest.skipUnless(importlib.util.find_spec("fastapi") is not None and
                     shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "FastAPI/FFmpeg integration dependencies required")
class QueueAPI(unittest.TestCase):
    def test_enqueue_visibility_invalid_link_failure_and_retry(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory(prefix="sml-yt-queue-") as temporary:
            original = os.environ.get("SML_DATA_ROOT")
            os.environ["SML_DATA_ROOT"] = temporary
            sys.path.insert(0, str(ROOT))
            try:
                sys.modules.pop("app", None)
                import app as module
                with TestClient(module.app) as client:
                    route = "/api/v1/media/imports"
                    self.assertEqual(client.post(route, json={
                        "source": "youtube", "value": "https://example.com/video",
                        "rights_confirmed": True}).status_code, 400)
                    self.assertEqual(client.post(route, json={
                        "source": "youtube", "value": "https://youtu.be/abc",
                        "rights_confirmed": False}).status_code, 400)
                    with patch("media_extensions.shutil.which", return_value=None), \
                         patch("media_extensions.os.access", return_value=False):
                        res = client.post(route, json={
                            "source": "youtube", "value": "https://youtu.be/abc",
                            "rights_confirmed": True})
                        self.assertEqual(res.status_code, 200, res.text)
                        job = res.json()["job_id"]
                        rows = client.get(route).json()["items"]
                        self.assertEqual(rows[0]["id"], job)
                        self.assertEqual(rows[0]["status"], "queued")
                        self.assertEqual(rows[0]["progress"], 0)
                        deadline = time.monotonic() + 4
                        while time.monotonic() < deadline:
                            row = client.get(route).json()["items"][0]
                            if row["status"] == "failed":
                                break
                            time.sleep(.1)
                    self.assertEqual(row["status"], "failed")
                    self.assertIn("yt-dlp", row["message"])
                    self.assertEqual(client.post(route + "/" + job + "/retry").status_code, 200)
            finally:
                sys.modules.pop("app", None)
                sys.path.remove(str(ROOT))
                if original is None:
                    os.environ.pop("SML_DATA_ROOT", None)
                else:
                    os.environ["SML_DATA_ROOT"] = original


    def test_explicit_proxy_guard_and_direct_retry(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory(prefix="sml-proxy-queue-") as temporary:
            old_root = os.environ.get("SML_DATA_ROOT")
            os.environ["SML_DATA_ROOT"] = temporary
            sys.path.insert(0, str(ROOT))
            try:
                sys.modules.pop("app", None)
                import app as module
                with patch.dict(os.environ, {"SML_YTDLP_PROXY_URL": ""}):
                    with TestClient(module.app) as client:
                        route = "/api/v1/media/imports"
                        authorized_proxy = {
                            "source": "youtube",
                            "value": "https://www.youtube.com/shorts/fixture",
                            "rights_confirmed": True,
                            "use_proxy": True,
                        }
                        self.assertFalse(client.get("/api/v1/media/status").json()["proxy_configured"])
                        # Explicit unavailable proxy is rejected before queue persistence.
                        response = client.post(route, json=authorized_proxy)
                        self.assertEqual(response.status_code, 400)
                        self.assertIn("Proxy profile is not configured", response.text)
                        self.assertEqual(client.get(route).json()["items"], [])

                        job_id = "proxyfixture001"
                        now = module.now_iso()
                        with module.db() as conn:
                            conn.execute("""INSERT INTO media_imports
                              (id,source_type,name,status,message,created_at,updated_at,request_json)
                              VALUES (?,?,?,?,?,?,?,?)""",
                              (job_id, "youtube", authorized_proxy["value"], "failed",
                               "Proxy profile is not configured", now, now,
                               json.dumps(authorized_proxy)))
                        listing = client.get(route).json()
                        self.assertFalse(listing["proxy_configured"])
                        self.assertTrue(listing["items"][0]["use_proxy"])
                        self.assertNotIn("request_json", listing["items"][0])
                        # Plain Retry cannot silently expose a proxy-intended job.
                        denied = client.post(route + "/" + job_id + "/retry")
                        self.assertEqual(denied.status_code, 409)
                        self.assertIn("Retry direct", denied.text)
                        with module.db() as conn:
                            row = conn.execute("SELECT status,request_json FROM media_imports WHERE id=?", (job_id,)).fetchone()
                            self.assertEqual(row["status"], "failed")
                            self.assertTrue(json.loads(row["request_json"])["use_proxy"])
                        # The owner explicitly agrees to a direct VPS connection.
                        direct = client.post(route + "/" + job_id + "/retry",
                                             json={"direct_without_proxy": True})
                        self.assertEqual(direct.status_code, 200, direct.text)
                        self.assertTrue(direct.json()["direct_without_proxy"])
                        with module.db() as conn:
                            row = conn.execute("SELECT request_json FROM media_imports WHERE id=?", (job_id,)).fetchone()
                        saved = json.loads(row["request_json"])
                        self.assertFalse(saved["use_proxy"])
                        self.assertTrue(saved["rights_confirmed"])
            finally:
                sys.modules.pop("app", None)
                sys.path.remove(str(ROOT))
                if old_root is None:
                    os.environ.pop("SML_DATA_ROOT", None)
                else:
                    os.environ["SML_DATA_ROOT"] = old_root



if __name__ == "__main__":
    unittest.main()

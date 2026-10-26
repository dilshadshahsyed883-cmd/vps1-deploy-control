"""Regression coverage for the isolated Scene Builder fourth Media Library tab."""
from __future__ import annotations

import asyncio
import ast
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
spec = importlib.util.spec_from_file_location("scene_local_library", SCENE / "scene_media_library.py")
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


class Upload:
    def __init__(self, name: str, contents: bytes):
        self.filename = name
        self.contents = contents
        self.cursor = 0
        self.closed = False

    async def read(self, n: int):
        chunk = self.contents[self.cursor:self.cursor+n]
        self.cursor += len(chunk)
        return chunk

    async def close(self):
        self.closed = True


class SceneLocalLibraryTests(unittest.TestCase):
    def test_image_video_upload_duplicate_summary_ranking_and_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = media.SceneMediaLibrary(root / "scene-only")
            sibling = root / "sml-original.mp4"
            sibling.write_bytes(b"do not delete")
            jpeg = b"\xff\xd8\xff" + b"fake-test-image" * 3
            video = b"\x00\x00\x00\x18ftypisom" + b"fake-test-video" * 3
            first = asyncio.run(store.import_upload(Upload("C:\\clips\\Alexa 1.jpg", jpeg), 2048))
            second = asyncio.run(store.import_upload(Upload("Alexa 2.mp4", video), 2048))
            self.assertTrue(first["approved"])
            self.assertEqual(first["kind"], "image")
            self.assertEqual(first["original_name"], "Alexa 1.jpg")
            self.assertEqual(second["kind"], "video")
            self.assertEqual(store.summary()["approved"], 2)
            self.assertEqual(len(store.list(kind="image")), 1)
            self.assertIn("Alexa 2.mp4", store.report())
            self.assertIn("Alexa 1.jpg", store.report())
            self.assertEqual(store.ranked({"keyword_used": "Alexa vehicle"})[0]["id"], first["id"])
            self.assertEqual(store.file(first["id"])[0].read_bytes(), jpeg)
            duplicate = asyncio.run(store.import_upload(Upload("copy.jpg", jpeg), 2048))
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(store.summary()["approved"], 2)
            self.assertEqual(media.SceneMediaLibrary(root / "scene-only").summary()["videos"], 1)
            self.assertEqual(store.clean(), 2)
            self.assertEqual(store.summary()["approved"], 0)
            self.assertTrue(sibling.exists())

    def test_invalid_format_limit_path_and_unknown_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = media.SceneMediaLibrary(Path(tmp) / "scene-only")
            for name, contents, limit in [
                ("bad.txt", b"not media"*5, 1024),
                ("spoof.jpg", b"this is actually text"*3, 1024),
                ("huge.png", b"\x89PNG\r\n\x1a\n" + b"x"*300, 100),
            ]:
                upload = Upload(name, contents)
                with self.assertRaises(media.InvalidMedia):
                    asyncio.run(store.import_upload(upload, limit))
                self.assertTrue(upload.closed)
            self.assertEqual(store.summary()["approved"], 0)
            with self.assertRaises(media.InvalidMedia):
                store.file("../sml-secrets")
            with self.assertRaises(FileNotFoundError):
                store.file("0"*32)

    def test_settings_and_audit_contract_and_python_syntax(self):
        app = (SCENE / "app.py").read_text(encoding="utf-8")
        ast.parse(app)
        base = (SCENE / "templates/base.html").read_text(encoding="utf-8")
        settings = (SCENE / "templates/settings.html").read_text(encoding="utf-8")
        audit = (SCENE / "templates/audit_media.html").read_text(encoding="utf-8")
        fourth = (SCENE / "templates/scene_media_library.html").read_text(encoding="utf-8")
        self.assertIn('href="/scene-builder/media-library/"', base)
        self.assertIn('name="scene_library_enabled"', settings)
        self.assertIn('name="scene_library_priority"', settings)
        self.assertIn('/api/audit-media/{job_id}/scene-library-auto-match', app)
        self.assertIn('require_settings_csrf(request)', app)
        self.assertIn('CLEAN SCENE LIBRARY', fourth)
        self.assertIn('autoMatchLocal', audit)
        self.assertIn('scene-library-select', audit)

    def test_media_library_shared_tabs_and_upload_label_contrast(self):
        from types import SimpleNamespace
        try:
            from jinja2 import Environment, FileSystemLoader
        except ImportError:
            self.skipTest("Jinja2 not installed")
        templates = Environment(loader=FileSystemLoader(str(SCENE / "templates")))
        rendered = templates.get_template("scene_media_library.html").render(
            page="scene_media_library",
            request=SimpleNamespace(session={}, query_params={}),
            username="team", csrf_token="test", is_admin=False,
        )
        self.assertIn('aria-label="Video Studio"', rendered)
        self.assertIn('aria-label="Scene Builder"', rendered)
        for url in ("/queue", "/settings", "/audit-media", "/scene-builder/media-library/"):
            self.assertIn(f'href="{url}"', rendered)
        self.assertIn('aria-current="page" href="/scene-builder/media-library/"', rendered)
        self.assertIn("vps1-scene-parity", rendered)
        self.assertIn("scene_builder_kokoro_parity_20261020.css", rendered)
        self.assertIn('label.sl-button.sl-primary{background:#315fdf;border-color:#315fdf;color:#fff!important}', rendered)
        self.assertIn('for="sl-files" class="sl-button sl-primary"', rendered)

    def test_template_and_browser_script_are_valid(self):
        try:
            from jinja2 import Environment, DictLoader
        except ImportError:
            self.skipTest("Jinja2 not installed")
        import shutil
        import subprocess
        env = Environment(loader=DictLoader({
            "base.html": "{% block content %}{% endblock %}{% block scripts %}{% endblock %}"
        }))
        fourth = env.from_string((SCENE / "templates/scene_media_library.html").read_text())
        rendered = fourth.render(csrf_token="test", is_admin=True, page="scene_media_library")
        self.assertIn("Auto Approved", rendered)
        if shutil.which("node"):
            for page in (rendered, env.from_string((SCENE / "templates/audit_media.html").read_text()).render(
                    selected_job={"id": "j1", "project_name": "Project"},
                    audit={"items": [], "sml_settings": {"scene_library_enabled": True}}, waiting_jobs=[])):
                for js in [p.split("</script>", 1)[0] for p in page.split("<script>")[1:]]:
                    with tempfile.TemporaryDirectory() as folder:
                        script = Path(folder) / "frontend.js"
                        script.write_text(js)
                        subprocess.run(["node", "--check", str(script)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()

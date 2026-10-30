"""Copyright Studio sidebar contracts and source-mode UI stability."""
from pathlib import Path
import ast
import unittest
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]


class CopyrightSidebarStability(unittest.TestCase):
    def test_all_known_studio_documents_bust_old_copyright_sidebar_cache(self):
        files = (
            "apps/short-maker/part01/app.py",
            "apps/local-upload-manager/part01/templates/base.html",
            "apps/copyright-uploader/part01/templates/base.html",
            "apps/voice-studio/part01/templates/voice_studio.html",
            "apps/video-merger/part01/templates/index.html",
            "apps/scene-builder/part01/templates/base.html",
        )
        for path in files:
            with self.subTest(path=path):
                body = (ROOT / path).read_text()
                self.assertIn("&copyright-nav=20261030-v3", body)
                self.assertIn("vps1_native_site_shell.js", body)
        short = (ROOT/"apps/short-maker/part01/app.py").read_text()
        self.assertIn('<a href="/video-merger/">Copyright Studio</a>', short)

    def test_copyright_isolated_tabs_and_folder_dropdown(self):
        root = ROOT/"apps/copyright-uploader/part01"
        page=(root/"templates/app.html").read_text()
        base=(root/"templates/base.html").read_text()
        Environment().parse(page)
        Environment().parse(base)
        self.assertIn("engine_instance not in ['song', 'shortstudio', 'copyright']", page)
        self.assertIn('id="copyright-folder-file"', page)
        self.assertIn('id="copyright-folder-refresh"', page)
        self.assertIn('class="lum-copyright-workspace"', base.replace(
            '<body class="{% if embedded_view %}lum-embedded{% endif %}{% if engine_instance in [\'song\', \'shortstudio\', \'copyright\'] %} lum-song-ui{% endif %}{% if engine_instance == \'copyright\' %} lum-copyright-workspace{% endif %}">',
            '<body class="lum-copyright-workspace">'))
        self.assertIn("copyright_video_picker.js", base)
        shared = (ROOT/"apps/scene-builder/part01/static/vps1_native_site_shell.js").read_text()
        self.assertIn('["Copyright Studio", "/video-merger/", "copyright"]', shared)
        self.assertIn('app === "copyright-uploader"', shared)

    def test_python_source_files_parse(self):
        for path in ("apps/copyright-uploader/part01/lum_engine/project_store.py",
                     "apps/copyright-uploader/part01/lum_engine/worker.py",
                     "apps/copyright-uploader/part01/lum_engine/standalone_app.py",
                     "apps/copyright-uploader/part01/lum_engine/copyright_handoff.py"):
            with self.subTest(path=path):
                ast.parse((ROOT/path).read_text(), filename=path)


if __name__ == "__main__":
    unittest.main()

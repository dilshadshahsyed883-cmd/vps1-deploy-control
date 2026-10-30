"""Canonical Song Uploader alias: isolated route, auth, headers and links."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
spec = importlib.util.spec_from_file_location("song_uploader_alias", SCENE / "song_uploader_alias.py")
alias = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alias)


class CanonicalSongUploader(unittest.TestCase):
    def test_direct_url_resolves_to_existing_song_engine(self):
        url = urlsplit(alias.upstream_path("/song-uploader"))
        self.assertEqual(url.path, "/local-upload-manager/modes/song/app")
        self.assertEqual(parse_qs(url.query), {"tab": ["auto_engine"], "embedded": ["1"]})
        url = urlsplit(alias.upstream_path("/song-uploader/", "tab=channels"))
        self.assertEqual(parse_qs(url.query), {"tab": ["channels"], "embedded": ["1"]})
        url = urlsplit(alias.upstream_path("/song-uploader/app", "tab=logs&embedded=0"))
        self.assertEqual(parse_qs(url.query), {"tab": ["logs"], "embedded": ["1"]})

    def test_assets_and_post_actions_use_same_durable_engine(self):
        self.assertEqual(alias.upstream_path("/song-uploader/static/auto_engine_v6.js"),
                         "/local-upload-manager/modes/song/static/auto_engine_v6.js")
        self.assertEqual(alias.upstream_path("/song-uploader/actions/add-video"),
                         "/local-upload-manager/modes/song/actions/add-video")
        self.assertEqual(alias.upstream_path("/song-uploader/api/foo", "a=1"),
                         "/local-upload-manager/modes/song/api/foo?a=1")

    def test_prevent_traversal_and_cross_engine(self):
        for value in ("/song-uploader/../other", "/song-uploader/./app",
                      "/song-uploader/\\other", "/song-uploader/\x00", "/local-upload-manager/modes/engine2/app"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                alias.upstream_path(value)

    def test_rewrite_html_links_redirects_and_cookie_path(self):
        html = b'<a href="/local-upload-manager/modes/song/app?tab=channels"></a>'
        self.assertIn(b'/song-uploader/app?tab=channels', alias.public_html(html))
        self.assertEqual(alias.public_location("/local-upload-manager/modes/song/app?tab=logs"),
                         "/song-uploader/app?tab=logs")
        self.assertEqual(alias.public_cookie("lum_session_song=a; path=/local-upload-manager/modes/song; httponly"),
                         "lum_session_song=a; path=/song-uploader; httponly")

    def test_scene_routes_use_verified_identity_and_stream_proxy(self):
        source = (SCENE / "app.py").read_text()
        ast.parse(source)
        self.assertIn('@app.api_route("/song-uploader/{upstream_path:path}"', source)
        self.assertIn("require_local_upload_manager_access(request)", source)
        self.assertIn('http://127.0.0.1:8015', source)
        self.assertIn('content=request.stream() if request.method == "POST" else None', source)
        self.assertIn('return RedirectResponse("/song-uploader"', source)
        self.assertNotIn("http://127.0.0.1:8018", source)

    def test_candidate_gate_allows_only_song_proxy_not_scene_writes(self):
        src = SCENE / "precutover_policy.py"
        spec = importlib.util.spec_from_file_location("precutover_policy", src)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.mutation_allowed("POST", "/song-uploader/actions/sync", False))
        self.assertFalse(module.mutation_allowed("POST", "/song-maker/api/queue", False))
        self.assertFalse(module.mutation_allowed("DELETE", "/song-uploader/actions/sync", False))
        self.assertFalse(module.mutation_allowed("POST", "/song-uploader-evil/actions/sync", False))


if __name__ == "__main__":
    unittest.main()

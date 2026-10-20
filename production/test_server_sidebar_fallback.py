"""Real Short Maker fallback HTML contract (no external JS, no media jobs)."""
import ast
import html
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKER = ROOT / "apps/short-maker/part01/app.py"
MERGER = ROOT / "apps/video-merger/part01/app.py"
MERGER_TEMPLATE = ROOT / "apps/video-merger/part01/templates/index.html"
NGINX_SSO = ROOT / "production/scene_session_routes.py"
RELEASE = ROOT / ".github/workflows/vps1-native-studio-release.yml"


def load_maker_renderer():
    """Run only the actual pure markup renderer from Short Maker's AST."""
    tree = ast.parse(MAKER.read_text(encoding="utf-8"))
    template = next(
        ast.literal_eval(statement.value)
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "HTML"
                for t in statement.targets)
    )
    render = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "render_verified_sidebar"
    )
    module = ast.fix_missing_locations(ast.Module(body=[render], type_ignores=[]))
    globals_ = {"html": html, "HTML": template}
    exec(compile(module, str(MAKER), "exec"), globals_)
    return globals_["render_verified_sidebar"]


class ServerSidebarFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.render = staticmethod(load_maker_renderer())

    def test_admin_not_dependent_on_shared_javascript(self):
        document = self.render("owner", True)
        self.assertIn('class="vps1-server-admin" href="/admin"', document)
        self.assertIn('data-vps1-server-identity="1"', document)
        self.assertIn('class="sidebar-account-name">owner', document)
        self.assertIn('form action="/logout"', document)
        self.assertIn('data-unify-sidebar="1"', document)
        self.assertEqual(document.count('class="vps1-server-admin"'), 1)

    def test_non_admin_never_has_admin_link(self):
        document = self.render("member", False)
        self.assertNotIn('class="vps1-server-admin"', document)
        self.assertIn('class="sidebar-account-name">member', document)
        self.assertIn('form action="/logout"', document)

    def test_missing_identity_does_not_leave_blank_footer(self):
        document = self.render("", False)
        self.assertNotIn('class="vps1-server-admin"', document)
        self.assertIn('data-vps1-server-identity="0"', document)
        self.assertIn('href="/login">Sign in', document)
        self.assertNotIn('form action="/logout"', document)

    def test_user_name_escaped_and_role_cannot_be_inferred_from_name(self):
        document = self.render('<owner & "admin">', False)
        self.assertIn("&lt;owner &amp; &quot;admin&quot;&gt;", document)
        self.assertNotIn('class="vps1-server-admin"', document)

    def test_trusted_proxy_role_headers_and_no_store(self):
        source = MAKER.read_text()
        nginx = NGINX_SSO.read_text()
        self.assertIn('request.headers.get("x-auth-user"', source)
        self.assertIn('request.headers.get("x-is-admin") == "1"', source)
        self.assertIn('"Cache-Control": "private, no-store"', source)
        self.assertIn("X-Is-Admin $scene_sso_admin", nginx)
        self.assertIn("X-Auth-User $scene_sso_user", nginx)
        self.assertIn('    internal;\\n', nginx)

    def test_video_merger_has_identical_trusted_sidebar_fallback(self):
        source = MERGER.read_text()
        template = MERGER_TEMPLATE.read_text()
        for marker in ("X-Auth-User", "X-Is-Admin", "data-vps1-server-identity",
                       "html.escape(user,quote=True)", "href=\"/login\"",
                       "class=\"vps1-server-admin\""):
            self.assertIn(marker, source, marker)
        self.assertEqual(template.count("<!-- V1_SERVER_ACCOUNT_FALLBACK -->"), 1)
        self.assertIn("<!-- Admin appears only after verified Scene session role -->", template)

    def test_guarded_release_stages_app_documents_before_scene_readback(self):
        source = RELEASE.read_text()
        self.assertIn("for app in short-maker video-merger scene-builder; do", source)
        self.assertIn("UNIFIED_SIDEBAR_RELEASE=PASS", source)


if __name__ == "__main__":
    unittest.main()

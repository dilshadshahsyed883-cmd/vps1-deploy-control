"""Regression checks: tenant workspaces must not offer global-only controls.

UI guards complement backend 403 protections; they must never widen access
to shared uploader, scheduler, OAuth, queue, or Audio Downloader proxy state.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]
UPLOADER_TEMPLATES = (
    "apps/local-upload-manager/part01/templates/app.html",
    "apps/copyright-uploader/part01/templates/app.html",
)


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_uploader_templates_compile_and_preserve_owned_actions() -> None:
    for path in UPLOADER_TEMPLATES:
        source = _source(path)
        Environment().parse(source)
        for owned in (
            "/actions/add-video",
            "/actions/oauth-start",
            "/actions/sheets/sync",
            "/actions/auto-v6/profile/save",
            "/actions/retry/",
        ):
            assert owned in source, (path, owned)
        assert 'Shared OAuth client is managed by an administrator' in source
        assert '{% if user.is_admin' in source
        if "copyright-uploader" in path:
            # Copyright has no modes settings card for either role.
            assert "Save modes" not in source
        else:
            assert 'Save modes</button></form></section>{% endif %}' in source


def test_uploader_dashboard_shared_modes_hidden_for_nonadmin() -> None:
    for path, instance in (
        (UPLOADER_TEMPLATES[0], "shortstudio"),
        (UPLOADER_TEMPLATES[0], "song"),
        (UPLOADER_TEMPLATES[1], "copyright"),
    ):
        if instance == "copyright":
            from production.copyright_uploader_browser import fixture
            for is_admin in (False, True):
                html = fixture("dashboard", is_admin=is_admin)
                assert "Save modes" not in html
                assert "/actions/settings" not in html
                assert "Copyright Uploader" in html
            continue
        source = _source(path)
        start = source.index('<div class="lum-grid {{')
        end_marker = "Save modes</button></form></section>{% endif %}</div>"
        end = source.index(end_marker, start) + len(end_marker)
        dashboard = Environment().from_string(source[start:end])
        values = {
            "engine_instance": instance,
            "base_path": "/short-uploader",
            "stats": {
                "engine_enabled": True,
                "settings": {
                    "sheets_sync_enabled": "1",
                    "auto_engine_enabled": "1",
                    "bulk_import_enabled": "0",
                },
            },
            "csrf": "test",
        }
        hidden = dashboard.render(user={"is_admin": 0}, **values)
        shown = dashboard.render(user={"is_admin": 1}, **values)
        assert "Save modes" not in hidden
        assert "/actions/settings" not in hidden
        assert "Serial engine" in hidden
        if instance in ("song", "shortstudio"):
            # Song and Short retire their modes cards for both roles.
            assert "/actions/settings" not in shown
            assert "Save modes" not in shown
        else:
            assert "/actions/settings" in shown
            assert "Save modes" in shown


def test_copyright_queue_global_controls_hidden_but_personal_actions_preserved() -> None:
    path = "apps/copyright-uploader/part01/templates/copyright_queue.html"
    source = _source(path)
    environment = Environment()
    environment.filters["schedule_pkt"] = lambda value: value
    template = environment.from_string(source)
    context = {
        "base_path": "/copyright-uploader",
        "projects": [],
        "stats": {"engine_enabled": True, "settings": {"pause_after_current": "0"}},
        "health": {"active_project_id": None},
        "csrf": "test",
    }
    nonadmin = template.render(user={"is_admin": 0}, **context)
    admin = template.render(user={"is_admin": 1}, **context)
    assert "/actions/pause-after-current" not in nonadmin
    assert "/actions/resume" not in nonadmin
    assert "/actions/pause-after-current" in admin
    for personal in ("/actions/retry/", "/actions/cancel/", "/actions/authorize-fresh/"):
        assert personal in source


def test_audio_proxy_is_absent_for_nonadmin_but_available_for_admin() -> None:
    scene_path = ROOT / "apps/scene-builder/part01/app.py"
    tree = ast.parse(scene_path.read_text(encoding="utf-8"))
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_audio_downloader_hide_shared_proxy_controls"
    )
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(scene_path), "exec"), namespace)
    hide = namespace[helper.name]
    sample = (
        b'<button type="button" data-vsa-section="proxy" aria-selected="false">Proxy Settings</button>'
        b'<div id="vsaProxy" class="vsa-content" hidden>'
        b'<button id="vsaProxySave">Save Profile</button></div>'
        b'<!-- /AUDIO_DOWNLOADER_TAB_V1 --><div id="safe-downloader">Download</div>'
    )
    assert hide(sample, is_admin=True) == sample
    hidden = hide(sample, is_admin=False)
    assert b'Proxy Settings' not in hidden
    assert b'vsaProxySave' not in hidden
    assert b'id="vsaProxy"' in hidden
    assert b'safe-downloader' in hidden

class SharedControlsUITests(unittest.TestCase):
    def test_templates(self) -> None:
        test_uploader_templates_compile_and_preserve_owned_actions()

    def test_dashboard_roles(self) -> None:
        test_uploader_dashboard_shared_modes_hidden_for_nonadmin()

    def test_copyright_queue_roles(self) -> None:
        test_copyright_queue_global_controls_hidden_but_personal_actions_preserved()

    def test_audio_downloader_roles(self) -> None:
        test_audio_proxy_is_absent_for_nonadmin_but_available_for_admin()


if __name__ == "__main__":
    unittest.main()

"""Offline checks: no secrets committed, SML/Scene web token activation fail-closed."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("sml_scene_activation", ROOT/"production/sml_scene_activation.py")
module=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ActivationTests(unittest.TestCase):
    def test_pairing_generates_no_uncontrolled_overwrite(self):
        sml=b"RCLONE_CONFIG=/path\n"
        scene=b"WORKER_URL=http://127.0.0.1:8000\n"
        linked=module._patched(sml,{"SML_EDITOR_API_TOKEN":"a"*64})
        web=module._patched(scene,{
            "SCENE_SML_EDITOR_TOKEN":"a"*64,
            "SCENE_SML_URL":"http://127.0.0.1:8147",
        })
        self.assertEqual(module._entries(linked)["SML_EDITOR_API_TOKEN"], "a"*64)
        self.assertEqual(module._entries(web)["SCENE_SML_EDITOR_TOKEN"], "a"*64)
        self.assertEqual(web.count(b"SCENE_SML_URL="),1)

    def test_idempotent_with_matching_values(self):
        data=b"SML_EDITOR_API_TOKEN="+b"A"*64+b"\n"
        self.assertEqual(module._patched(data,{"SML_EDITOR_API_TOKEN":"A"*64}),data)

    def test_different_existing_token_fails(self):
        data=b"SML_EDITOR_API_TOKEN="+b"A"*64+b"\n"
        with self.assertRaises(RuntimeError):
            module._patched(data,{"SML_EDITOR_API_TOKEN":"B"*64})

    def test_existing_origin_must_not_be_silently_changed(self):
        data=b"SCENE_SML_URL=http://127.0.0.1:9012\n"
        with self.assertRaises(RuntimeError):
            module._patched(data,{"SCENE_SML_URL":"http://127.0.0.1:8147"})

    def test_duplicate_env_key_fails_closed(self):
        data=b"X=1\nX=2\n"
        with self.assertRaises(RuntimeError):
            module._entries(data)

    def test_atomic_write_private_and_recoverable(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"runtime.env"
            p.write_bytes(b"ORIGINAL=1\n")
            p.chmod(0o600)
            with patch.object(module.os,"geteuid",return_value=0):
                module._atomic_replace(p,b"ORIGINAL=2\n")
            self.assertEqual(p.read_bytes(),b"ORIGINAL=2\n")
            self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_regular_private_env_required(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/"env"
            p.write_bytes(b"X=1\n")
            p.chmod(0o644)
            with self.assertRaises(RuntimeError):
                module._ensure_private(p)
            p.chmod(0o600)
            link=Path(root)/"link"
            link.symlink_to(p)
            with self.assertRaises(RuntimeError):
                module._ensure_private(link)

    def test_owner_approved_media_source_contract_for_reactivation(self):
        source = (ROOT/"apps/self-media-library/part01/media_extensions.py").read_text()
        for required in ('/api/v1/media/inventory', '/api/v1/media/image/{asset_id}/content',
                         "status='approved'", 'x_sml_editor_token'):
            self.assertIn(required, source)
        browser = (ROOT/"apps/self-media-library/part01/static/app.js").read_text()
        self.assertIn("document.querySelectorAll('[data-bulk-status]')", browser)
        self.assertIn("document.querySelectorAll('#bulkBar button')", browser)
        scene = (ROOT/"apps/scene-builder/part02/sml_worker_bridge.py").read_text()
        self.assertIn('image', scene)
        self.assertIn('video', scene)

    def test_activation_never_restarts_scene_worker_admission(self):
        s=(ROOT/"production/sml_scene_activation.py").read_text()
        self.assertIn('SML_SERVICE = "vps1-production-self-media-library.service"',s)
        self.assertIn('SCENE_SERVICE = "vps1-scene-candidate.service"',s)
        self.assertNotIn('_restart("vps1-scene-worker-admission',s)
        self.assertNotIn("SML_EDITOR_API_TOKEN="+("S"*64),s)
        workflow=(ROOT/".github/workflows/vps1-sml-scene-activate.yml")
        self.assertTrue(workflow.is_file())
        self.assertIn("[vps1-sml-scene-activate]",workflow.read_text())

if __name__=="__main__":
    unittest.main()

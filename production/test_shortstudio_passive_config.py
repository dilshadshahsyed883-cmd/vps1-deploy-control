"""Zero-restart four-service passive enrollment contract tests (no VPS)."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from production import shortstudio_passive_config as mod


class PassiveConfigContract(unittest.TestCase):
    def test_env_preserves_secrets_and_adds_only_off_defaults(self):
        source=b"PRIVATE_OAUTH_VALUE=KEEP_UNTOUCHED\nOTHER_FIELD=keep\n"
        expected={"SHORT_STUDIO_VPS_FOLDER_ROOT": str(mod.ROOT),
                  "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED":"0"}
        actual=mod.env_patch(source, expected)
        self.assertTrue(actual.startswith(source))
        self.assertIn(b"PRIVATE_OAUTH_VALUE=KEEP_UNTOUCHED\n", actual)
        self.assertIn(b"SHORT_MAKER_COOPERATIVE_LOCK_ENABLED=0\n", actual)
        self.assertEqual(mod.env_patch(actual, expected),actual)
        self.assertNotIn(b"SHORT_MAKER_COOPERATIVE_LOCK_ENABLED=1",actual)

    def test_conflicting_existing_flag_or_root_refused(self):
        cfg={"SHORT_STUDIO_VPS_FOLDER_ROOT":str(mod.ROOT),
             "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED":"0"}
        for existing in (
            b"SHORT_MAKER_COOPERATIVE_LOCK_ENABLED=1\n",
            b"SHORT_STUDIO_VPS_FOLDER_ROOT=/tmp/untrusted\n",
            b"SHORT_MAKER_DAILY_PROFILES_ENABLED=1\n",
            b"SHORT_MAKER_UNUSED_FOLDER_DELETE_ENABLED=1\n",
            b"SHORT_MAKER_COOPERATIVE_LOCK_ENABLED=0\nSHORT_MAKER_COOPERATIVE_LOCK_ENABLED=0\n",
        ):
            with self.subTest(value=existing):
                with self.assertRaises(RuntimeError):
                    mod.env_patch(b"ANOTHER=keep\n"+existing,cfg)

    def test_systemd_changes_only_three_short_uploader_mounts(self):
        base="""[Unit]
Description=Short Uploader
[Service]
User=ubuntu
WorkingDirectory=/srv/fresh-website/current/local-upload-manager/part01
EnvironmentFile={env}
ProtectSystem=strict
ReadWritePaths=/var/lib/fresh-website/local-upload-manager -/home/ubuntu/.config/masterctl
""".format(env=mod.UPLOADER_ENV).encode()
        patched=mod.unit_patch(base,name="web",environment=mod.UPLOADER_ENV)
        self.assertEqual(patched.count(str(mod.ROOT).encode()),1)
        self.assertIn(str(mod.LOCK).encode(),patched)
        self.assertNotIn((str(mod.ROOT)+"\n").encode(),patched)
        worker_mount=base.replace(
            b"ReadWritePaths=/var/lib/fresh-website/local-upload-manager -/home/ubuntu/.config/masterctl",
            ("ReadWritePaths=/var/lib/fresh-website/local-upload-manager -/home/ubuntu/.config/masterctl "+str(mod.ROOT)).encode())
        self.assertEqual(
            mod.unit_patch(worker_mount,name="worker",environment=mod.UPLOADER_ENV),
            worker_mount)
        with self.assertRaises(RuntimeError):
            mod.unit_patch(base,name="worker",environment=mod.UPLOADER_ENV)
        self.assertIn(b"ProtectSystem=strict",patched)
        self.assertEqual(mod.unit_patch(patched,name="web",environment=mod.UPLOADER_ENV),patched)
        self.assertNotIn(b"Environment=SHORT_MAKER_COOPERATIVE_LOCK_ENABLED=1",patched)
        for bad in (
            base.replace(b"ProtectSystem=strict",b"ProtectSystem=off"),
            base.replace(b"User=ubuntu",b"User=root"),
            base.replace(str(mod.UPLOADER_ENV).encode(),b"/tmp/custom.env"),
            base+b"ReadWritePaths=/tmp/untrusted\n",
            base+b"Environment=SHORT_STUDIO_VPS_FOLDER_ROOT=/tmp/override\n",
        ):
            with self.assertRaises(RuntimeError):
                mod.unit_patch(bad,name="web",environment=mod.UPLOADER_ENV)

    def test_blocked_missing_live_root_before_any_file_write(self):
        with patch.object(mod.ROOT.__class__,"is_dir",return_value=False):
            with self.assertRaises(RuntimeError):
                mod._root_check()

    def test_protected_dispatch_is_exact_and_no_restart(self):
        root=Path(__file__).resolve().parents[1]
        runtime=(root/"production/runtime.py").read_text()
        workflow=(root/".github/workflows/vps1-short-studio-release.yml").read_text()
        self.assertIn("if '[vps1-shortstudio-passive-four-unit-prepare]' in intent:",runtime)
        self.assertIn("prepare(args.revision)",runtime)
        self.assertIn("args.apps != ['local-upload-manager']",runtime)
        selector="elif [[ \"$RELEASE_INTENT\" == *'[vps1-shortstudio-passive-four-unit-prepare]'* ]]; then"
        self.assertIn(selector,workflow)
        selected=workflow.split(selector,1)[1].split("          elif ",1)[0]
        self.assertIn("apps=(local-upload-manager)",selected)
        self.assertNotIn("apps=(short-maker)",selected)
        source=(root/"production/shortstudio_passive_config.py").read_text()
        self.assertNotIn("systemctl\", \"stop",source)
        self.assertNotIn("systemctl\", \"restart",source)
        self.assertNotIn("systemctl\", \"start",source)
        for permanent in ("services_restarted\": False","feature_flags_enabled\": False",
                          "running_mount_activation_verified", "active_upload_drain_verified",
                          "\"retirement_enabled\": False"):
            self.assertIn(permanent,source)


if __name__=="__main__":
    unittest.main()

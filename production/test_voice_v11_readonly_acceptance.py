"""Offline contracts for Voice's no-mutation production acceptance lane."""
from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

from production import voice_v11_readonly_acceptance as audit
from production import gateway, runtime


class VoiceReadOnlyTests(unittest.TestCase):
    def live_status_fetcher(self, *, anonymous=401, edge_ready=True, clone_configured=False):
        calls = []
        def fetch(path, cookie=""):
            calls.append((path, bool(cookie)))
            if not cookie:
                return anonymous, "application/json", b'{"detail":"denied"}'
            if path.endswith("/voices"):
                return 200, "application/json; charset=utf-8", b'{"voices":[{"id":"test"}]}'
            if path.endswith("/clone/status"):
                return 200, "application/json", json.dumps({
                    "ok": True, "worker_configured": clone_configured,
                    "worker_online": False, "profiles": 0, "counts": {},
                }).encode()
            return 200, "application/json", json.dumps({
                "ready": edge_ready, "reason": "worker_offline", "queue": {"completed": 17},
            }).encode()
        return fetch, calls

    def test_signed_kokoro_and_edge_without_exposing_owner_queue(self):
        for engine in ("kokoro", "edge"):
            with self.subTest(engine=engine):
                fetch, calls = self.live_status_fetcher()
                result = audit.probe_engine(engine, "signed-session", fetch)
                self.assertEqual(result, {
                    "ready": True, "generation_test": "NOT_RUN",
                    "catalog_nonempty": True,
                })
                self.assertEqual([authenticated for _, authenticated in calls],
                                 [False, True, False, True])
                self.assertNotIn("completed", str(result))
                self.assertNotIn("'id':", str(result))

    def test_clone_unconfigured_is_reported_not_falsely_ready(self):
        fetch, calls = self.live_status_fetcher()
        result = audit.probe_engine("clone", "signed-session", fetch)
        self.assertEqual(result, {"configured": False, "online": False,
                                  "generation_test": "NOT_RUN"})
        self.assertEqual(len(calls), 2)

    def test_anonymous_api_exposure_rejected(self):
        fetch, _ = self.live_status_fetcher(anonymous=200)
        with self.assertRaisesRegex(RuntimeError, "ANONYMOUS_ENGINE_EXPOSURE"):
            audit.probe_engine("edge", "signed-session", fetch)

    def test_invalid_scene_cookie_rejected_not_retried(self):
        def fetch(path, cookie=""):
            return (401, "application/json", b'{"detail":"unauthorized"}')
        with self.assertRaisesRegex(RuntimeError, "SIGNED_ENGINE_STATUS_MISSING"):
            audit.probe_engine("edge", "bad-session", fetch)

    def test_owner_identity_not_in_error_on_schema_drift(self):
        def fetch(path, cookie=""):
            if not cookie:
                return 401, "application/json", b'{}'
            return 200, "application/json", b'{"ready":"yes","queue":{"private":99}}'
        with self.assertRaisesRegex(RuntimeError, "ENGINE_READINESS_SCHEMA_DRIFT"):
            audit.probe_engine("kokoro", "signed-session", fetch)

    def test_exact_managed_revision_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sha = "a" * 40
            (base / "control/state").mkdir(parents=True)
            (base / "current").mkdir()
            target = base / "releases/voice-studio" / sha
            target.mkdir(parents=True)
            (base / "current/voice-studio").symlink_to(target, target_is_directory=True)
            (base / "control/state/voice-studio.json").write_text(
                json.dumps({"revision": sha, "status": "ACTIVE"}))
            with mock.patch.object(audit, "BASE", base):
                self.assertEqual(audit.installed_revision(), sha)
                (base / "control/state/voice-studio.json").write_text(
                    json.dumps({"revision": "b" * 40, "status": "ACTIVE"}))
                with self.assertRaisesRegex(RuntimeError, "POINTER_MISMATCH"):
                    audit.installed_revision()

    def test_report_never_starts_stops_or_mutates_any_service(self):
        fetch, _ = self.live_status_fetcher()
        fake_process = mock.Mock(returncode=0)
        with mock.patch.object(audit, "installed_revision", return_value="c" * 40), \
             mock.patch.object(audit, "http_check", return_value={"ok": True}), \
             mock.patch.object(audit.subprocess, "run", return_value=fake_process) as proc, \
             mock.patch.object(audit.scene_sso, "ready_cookie", return_value="signed") as login, \
             mock.patch.object(audit.scene_sso, "verify") as sso, \
             mock.patch.object(audit.scene_sso, "verify_voice_shell") as shell, \
             mock.patch.object(audit, "verify_voice_release_anonymous_denial") as denial, \
             mock.patch.object(audit, "probe_engine",
                               side_effect=lambda engine, cookie: {"engine": engine}) as probe:
            result = audit.report()
        self.assertEqual(result["result"], "PASS_READONLY_PROBES")
        self.assertEqual(result["admin_nonadmin_real_2_user_uat"], "PENDING")
        self.assertEqual(result["edge_real_generation"], "NOT_TESTED")
        self.assertEqual(result["drive_real_delivery"], "NOT_TESTED")
        self.assertEqual(result["mutation"], "NONE")
        self.assertEqual(proc.call_count, 1 + len(audit.UNITS))
        for call in proc.call_args_list:
            self.assertEqual(call.args[0][0] in ("nginx", "systemctl"), True)
            self.assertNotIn("restart", call.args[0])
            self.assertNotIn("reload", call.args[0])
        login.assert_called_once_with("voice-studio")
        sso.assert_called_once_with("voice-studio", "signed")
        shell.assert_called_once_with("signed")
        denial.assert_called_once_with()
        self.assertEqual(probe.call_count, 3)

    def test_runtime_readonly_selector_does_not_call_generic_installer(self):
        marker = ("audit [vps1-voice-readonly-acceptance] "
                  "[vps1-production-release:voice-studio]")
        with mock.patch("sys.argv", ["runtime", "install", "--revision", "a" * 40,
                                      "--apps", "voice-studio"]), \
             mock.patch.object(runtime, "check_host"), \
             mock.patch.object(runtime, "deployment_lock", return_value=nullcontext()), \
             mock.patch.object(runtime, "run", return_value=marker), \
             mock.patch.object(runtime, "install_app") as install, \
             mock.patch.object(runtime, "event") as event, \
             mock.patch.object(audit, "report", return_value={"result":"READ_ONLY"}):
            runtime.main()
        install.assert_not_called()
        self.assertEqual(event.call_args.kwargs["result"], "READ_ONLY")

    def test_gateway_never_rebuilds_legacy_nginx_for_audit(self):
        marker = ("audit [vps1-voice-readonly-acceptance] "
                  "[vps1-production-release:voice-studio]")
        with mock.patch("sys.argv", ["gateway", "--revision", "a" * 40,
                                      "--apps", "voice-studio"]), \
             mock.patch.object(gateway, "check_host"), \
             mock.patch.object(gateway, "run", return_value=marker), \
             mock.patch.object(gateway, "install") as rebuild, \
             mock.patch.object(gateway, "event") as event:
            gateway.main()
        rebuild.assert_not_called()
        self.assertEqual(event.call_args.kwargs["gateway"], "NO_MUTATION")

    def test_conflicting_mutation_intent_fails_closed_before_gateway(self):
        marker = ("[vps1-voice-readonly-acceptance] [vps1-song-audio-separate]"
                  " [vps1-production-release:voice-studio]")
        with mock.patch("sys.argv", ["runtime", "install", "--revision", "a" * 40,
                                      "--apps", "voice-studio"]), \
             mock.patch.object(runtime, "check_host"), \
             mock.patch.object(runtime, "deployment_lock", return_value=nullcontext()), \
             mock.patch.object(runtime, "run", return_value=marker), \
             mock.patch.object(runtime, "install_app") as install:
            with self.assertRaisesRegex(RuntimeError, "read-only acceptance requires exact"):
                runtime.main()
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()

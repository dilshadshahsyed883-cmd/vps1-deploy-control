"""No-mutation/secret-redaction tests for failed Song Audio rollback audit."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from production import song_audio_voice_diagnostic as diag


class ReadOnlyFailedRollbackTests(unittest.TestCase):
    def test_env_flag_never_discloses_secret_or_untrusted_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "voice.env"
            env.write_text("WEB_ACCESS_KEY=fixture-never-report\nSONG_AUDIO_BACKEND_SPLIT=1\n")
            self.assertEqual(diag._env_flag(env, "SONG_AUDIO_BACKEND_SPLIT",
                                            {"1": "SEPARATED"}), "SEPARATED")
            env.write_text("SONG_AUDIO_BACKEND_SPLIT=1\nSONG_AUDIO_BACKEND_SPLIT=0\n")
            self.assertEqual(diag._env_flag(env, "SONG_AUDIO_BACKEND_SPLIT",
                                            {"1": "SEPARATED"}), "DUPLICATE")
            env.write_text("SONG_AUDIO_BACKEND_SPLIT=untrusted-secret\n")
            self.assertEqual(diag._env_flag(env, "SONG_AUDIO_BACKEND_SPLIT",
                                            {"1": "SEPARATED"}), "OTHER")

    def test_unit_only_exposes_safe_metadata(self):
        message = ("ActiveState=failed\nSubState=failed\nResult=exit-code\n"
                   "MainPID=0\nExecMainStatus=1\nNRestarts=5\n")
        with patch.object(diag.subprocess, "run",
                          return_value=Mock(stdout=message, returncode=0)) as run:
            value = diag._unit(diag.service.VOICE_UNIT)
            self.assertEqual(value["active"], "failed")
            self.assertFalse(value["pid_present"])
            self.assertEqual(value["restarts"], "5")
            run.assert_called_once()
        with self.assertRaises(ValueError):
            diag._unit("unapproved.service")

    def test_journal_is_only_boolean_classification(self):
        raw = ("sensitive-password=never-print\n"
               "Traceback (most recent call last):\n"
               "ModuleNotFoundError: No module named 'private_module'\n"
               "sqlite3.OperationalError: another private value")
        with patch.object(diag.subprocess, "run",
                          return_value=Mock(stdout=raw)):
            result = diag._error_classes()
            self.assertTrue(result["ModuleNotFoundError"])
            self.assertTrue(result["SQLiteError"])
            self.assertNotIn("private_module", str(result))
            self.assertNotIn("never-print", str(result))

    def test_missing_module_only_emits_safe_identifier_not_log_details(self):
        raw = ("WEB_ACCESS_KEY=sensitive-fixture\\n"
               "ModuleNotFoundError: No module named 'voice_missing_fixture'\\n"
               "Call from /var/secret/users/record")
        with patch.object(diag.subprocess, "run",
                          return_value=Mock(stdout=raw)):
            name = diag._missing_module()
            self.assertEqual(name, "voice_missing_fixture")
            self.assertNotIn("sensitive", name)
            self.assertNotIn("/var/", name)
        with patch.object(diag.subprocess, "run",
                          return_value=Mock(stdout="ModuleNotFoundError: unsafe private data")):
            self.assertEqual(diag._missing_module(), "UNKNOWN")

    def test_pointer_reveals_only_owned_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            releases = root / "releases"
            current = root / "current"
            (releases / "old").mkdir(parents=True)
            current.symlink_to(releases / "old")
            self.assertEqual(diag._pointer(current, releases), "LEGACY_OWNED")
            current.unlink()
            current.symlink_to(root / "outside")
            self.assertEqual(diag._pointer(current, releases), "OUTSIDE_OWNER")

    def test_implementation_contains_no_mutation_helpers(self):
        source = Path(diag.__file__).read_text()
        for unsafe in ('systemctl", "start"', 'systemctl", "stop"',
                       'systemctl", "restart"', 'atomic_link(', 'DELETE FROM jobs',
                       'UPDATE jobs SET', 'os.replace('):
            self.assertNotIn(unsafe, source)

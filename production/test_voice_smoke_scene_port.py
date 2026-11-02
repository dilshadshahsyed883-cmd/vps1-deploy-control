"""Guarded Voice runtime smoke when Scene already owns its live SSO port.

Disposable fixture only: no production DB, credentials, queue or uploads.
"""
from __future__ import annotations

import errno
import socket
import unittest

from production.smoke import smoke


class VoiceOccupiedScenePortSmokeTest(unittest.TestCase):
    def test_voice_smoke_does_not_bind_real_scene_sso_port(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        try:
            blocker.bind(("127.0.0.1", 18080))
            blocker.listen(1)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise

        result = smoke("voice-studio")
        self.assertEqual(result["health"], "PASS")
        self.assertEqual(result["html_ui"], "PASS")
        self.assertEqual(result["unverified_scene_denied"], "PASS_401")
        self.assertEqual(result["voice_three_engine_apis"], "PASS")
        self.assertFalse(result["external_jobs"])


if __name__ == "__main__":
    unittest.main()

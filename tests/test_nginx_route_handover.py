"""Regression tests for VPS1 authenticated Nginx cutover worker handover."""
import unittest
from unittest import mock
from production import nginx_route_only as release


class HTTPSHandoverTests(unittest.TestCase):
    def test_stale_scene_redirect_can_transition_to_deny_401(self):
        probe = mock.Mock(side_effect=[
            (302, "text/html", b"login"),
            (401, "text/plain", b"denied"),
        ])
        with mock.patch.object(release, "tls_get", probe), mock.patch.object(release.time, "sleep"):
            ct, body = release.require_https_status("/short-maker/health",
                expected=401, app="short-maker", attempts=3, pause=0)
        self.assertEqual(body, b"denied")
        self.assertEqual(probe.call_count, 2)

    def test_transient_redirect_must_not_count_as_authenticated_200(self):
        probe = mock.Mock(side_effect=[
            (302, "text/html", b"login"),
            (200, "application/json", b'{"ok":true}'),
        ])
        with mock.patch.object(release, "tls_get", probe), mock.patch.object(release.time, "sleep"):
            ct, body = release.require_https_status("/short-maker/health",
                expected=200, app="short-maker", authorization="Basic test", attempts=3, pause=0)
        self.assertEqual(ct, "application/json")
        self.assertEqual(body, b'{"ok":true}')

    def test_stuck_scene_redirect_fail_closed(self):
        with mock.patch.object(release, "tls_get", return_value=(302, "text/html", b"login")), \
             mock.patch.object(release.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "expected=401 observed=302"):
                release.require_https_status("/short-maker/health", expected=401, app="short-maker", attempts=2, pause=0)

    def test_anonymous_http_200_is_never_accepted(self):
        with mock.patch.object(release, "tls_get", return_value=(200, "text/html", b"wrong UI")), \
             mock.patch.object(release.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "expected=401 observed=200"):
                release.require_https_status("/short-maker/health", expected=401, app="short-maker", attempts=8, pause=0)
        sleep.assert_not_called()

    def test_authenticated_http_401_fails_immediately(self):
        with mock.patch.object(release, "tls_get", return_value=(401, "", b"")), \
             mock.patch.object(release.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "expected=200 observed=401"):
                release.require_https_status("/short-maker/health", expected=200, app="short-maker", authorization="Basic test", pause=0)
        sleep.assert_not_called()

    def test_check_app_requires_health_json_and_html(self):
        statuses = [
            (302, "text/html", b"scene login"), (401, "", b"denied"),
            (302, "text/html", b"scene login"), (200, "application/json", b'{"ok":true}'),
            (401, "", b"denied"), (200, "text/html", b"<html>Short Maker</html>"),
        ]
        with mock.patch.object(release, "tls_get", side_effect=statuses) as probe, \
             mock.patch.object(release.time, "sleep"):
            release.check_app("short-maker", "Basic test")
        self.assertEqual(probe.call_count, 6)

    def test_bad_health_body_rolls_back_guard(self):
        responses = [
            (401, "", b"denied"),
            (200, "application/json", b'{"ok":false}'),
        ]
        with mock.patch.object(release, "tls_get", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "health payload invalid"):
                release.check_app("short-maker", "Basic test")


if __name__ == "__main__":
    unittest.main()

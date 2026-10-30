"""Skipped push workflows must not consume/cancel the production concurrency slot."""
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[1]
class SongReleaseScheduling(unittest.TestCase):
    def test_only_guarded_jobs_join_shared_production_concurrency(self):
        for name in ('vps1-song-maker-release.yml','vps1-song-studio-release.yml','vps1-production.yml'):
            with self.subTest(workflow=name):
                source = (ROOT/'.github/workflows'/name).read_text()
                before, jobs = source.split('jobs:\n', 1)
                self.assertNotIn('\nconcurrency:', before)
                self.assertIn('  deploy:\n', jobs)
                self.assertIn('    concurrency:\n      group: vps1-production-applications\n      cancel-in-progress: false\n', jobs)
                for guard in ("github.ref == 'refs/heads/main'", "github.actor == 'dilshadshahsyed883-cmd'", 'environment: production', 'StrictHostKeyChecking=yes', 'sudo -n /usr/local/sbin/vps1-production-deploy'):
                    self.assertIn(guard, jobs)

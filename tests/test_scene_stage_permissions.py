"""Prevent root umask 077 from blocking Scene worker imports in new releases."""

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from production import scene_candidate_release as candidate


class SceneStageDirectoryPermissionsTests(unittest.TestCase):
    @staticmethod
    def _sources():
        return [
            ("part01/concat_sso_issuer.py", b"ISSUER = True\n"),
            ("part02/smart_scroll/__init__.py", b""),
            ("part02/smart_scroll/lut_engine.py", b"LUT_SIZE = 17\n"),
        ]

    def test_root_umask_077_still_creates_worker_traversable_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            releases = Path(temporary) / "releases"
            releases.mkdir()
            with patch.object(candidate, "RELEASES", releases), \
                 patch("rebuild.fresh_vps.sources", return_value=self._sources()), \
                 patch("rebuild.stage_auto_script.validated_files", return_value={}):
                original_umask = os.umask(0o077)
                try:
                    stage = candidate._stage("a" * 40)
                finally:
                    os.umask(original_umask)

                for directory in (
                    stage,
                    stage / "part01",
                    stage / "part02",
                    stage / "part02/smart_scroll",
                ):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
                self.assertEqual(
                    stat.S_IMODE((stage / "part02/smart_scroll/lut_engine.py").stat().st_mode),
                    0o644,
                )

                # A failed attempt may leave the same immutable, verified
                # candidate in place. Re-entry must repair directory traversal,
                # never silently accept 0700 paths.
                for directory in (stage, stage / "part02", stage / "part02/smart_scroll"):
                    directory.chmod(0o700)
                again = candidate._stage("a" * 40)
                self.assertEqual(again, stage)
                for directory in (stage, stage / "part02", stage / "part02/smart_scroll"):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)

    def test_rejects_symlink_inside_immutable_staged_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "release"
            stage.mkdir()
            (stage / "external").symlink_to(Path(temporary), target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                candidate._make_staged_directories_traversable(stage)


if __name__ == "__main__":
    unittest.main()

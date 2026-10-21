"""SML no longer performs remote storage IO; other apps keep their Drive workflow."""
from pathlib import Path
import ast
import unittest
ROOT=Path(__file__).resolve().parents[1]/'apps/self-media-library/part01'
class LocalOnlyContract(unittest.TestCase):
    def test_no_remote_commands_or_retry_routes(self):
        source=(ROOT/'app.py').read_text()+(ROOT/'media_extensions.py').read_text()
        for retired in ('def run_rclone(', 'def sync_source_drive(', 'def upload_and_verify(', '/drive-sync', '/api/drive/test'):
            self.assertNotIn(retired,source)
        ast.parse((ROOT/'app.py').read_text())
    def test_no_drive_controls(self):
        html=(ROOT/'static/index.html').read_text()
        for retired in ('id="testDrive"','id="driveEnabled"','data-sml-import="drive"'):
            self.assertNotIn(retired,html)

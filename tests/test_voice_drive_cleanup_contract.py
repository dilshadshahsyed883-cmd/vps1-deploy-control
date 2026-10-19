"""Source-only checks: independent voice job cleanup and safe VPS rollout."""
import ast
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
class PerAppVoiceCleanupTests(unittest.TestCase):
    def test_kokoro_engines_gate_cleanup(self):
        for path in ("apps/kokoro/kokoro_worker.py","apps/voice-studio/part02/kokoro_worker.py"):
            source=(ROOT/path).read_text()
            ast.parse(source)
            self.assertIn("delivery_status='delivered' and drive_txt!='' and drive_wav!=''",source)
            self.assertIn("delivery_status not in ('delivered','cleaned')",source)
            self.assertIn("folder.resolve().parent!=(DATA/'jobs').resolve()",source)
            self.assertIn("cleanup_delivered()",source)
    def test_edge_delivery_and_cleanup(self):
        source=(ROOT/"apps/voice-studio/part03/edge_worker.py").read_text()
        ast.parse(source)
        self.assertIn("delivery_status='delivered' and drive_txt!='' and drive_mp3!=''",source)
        self.assertIn("delivery_status not in ('delivered','cleaned','blocked')",source)
        self.assertIn("if row['srt_file']",source)
        self.assertIn("folder.is_symlink()",source)
        self.assertIn("cleanup_delivered()",source)
    def test_rollout_is_scoped_and_has_idle_gate(self):
        workflow=(ROOT/".github/workflows/vps1-app-cleanup-release.yml").read_text()
        self.assertIn("PHASE=READ_ONLY_APP_IDLE_PREFLIGHT",workflow)
        self.assertIn("for app in short-maker self-media-library video-merger",workflow)
        self.assertIn("rollback $SOURCE_SHA $app",workflow)
        self.assertNotIn("install $SOURCE_SHA all",workflow)
        self.assertIn("AUTO_SCRIPT=OFFLINE",workflow)
if __name__=='__main__':
    unittest.main()

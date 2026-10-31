"""Release contract: PR #610 deploys only Song Maker and Audio Downloader.

Static checks run without contacting VPS, GitHub secrets, or Google Drive.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/vps1-audio-songmaker-handover-release.yml"
RUNTIME = ROOT / "production/runtime.py"


class AudioSongMakerHandoverReleaseTests(unittest.TestCase):
    def test_main_only_pinned_two_app_release(self):
        source = WORKFLOW.read_text()
        for required in (
            "branches: [main]",
            "github.ref == 'refs/heads/main'",
            "github.actor == 'dilshadshahsyed883-cmd'",
            "[vps1-audio-songmaker-handover-release]",
            "[vps1-audio-thumbnail-only]",
            "group: vps1-production-applications",
            "cancel-in-progress: false",
            "environment: production",
            "tailscale/github-action@v4",
            "UserKnownHostsFile=",
            "StrictHostKeyChecking=yes",
            "SOURCE_SHA: ${{ github.sha }}",
            "rollback:song-maker",
        ):
            self.assertIn(required, source)
        self.assertIn("release install song-maker", source)
        self.assertIn("release install voice-studio", source)
        self.assertLess(source.index("release install song-maker"),
                        source.index("release install voice-studio"))
        self.assertIn("release rollback song-maker", source)
        self.assertIn("PASS_AUDIO_THUMBNAIL_ONLY", source)
        self.assertNotIn("release install scene-builder", source)
        self.assertNotIn("release install local-upload-manager", source)
        self.assertNotIn("release install short-maker", source)
        self.assertNotIn("release install auto-script", source)
        self.assertNotIn("cancel-in-progress: true", source)

    def test_voice_dispatch_uses_audio_overlay_not_song_studio_web(self):
        dispatch = RUNTIME.read_text()
        self.assertIn("if '[vps1-audio-thumbnail-only]' in commit_message:", dispatch)
        self.assertIn("install_audio_thumbnail(revision, restore_proxy=False, include_thumbnail=True)", dispatch)
        # The merge marker deliberately excludes the broader Song Studio marker
        # and the separately triggered Audio thumbnail release marker.
        source = WORKFLOW.read_text()
        self.assertNotIn("'[vps1-song-studio-release]'", source)
        self.assertNotIn("'[vps1-audio-thumbnail-release]'", source)


if __name__ == "__main__":
    unittest.main()

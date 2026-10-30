"""Guarded rollout contracts; no network, service changes, or YouTube publishing."""
import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from production import song_folder_release as release

ROOT=Path(__file__).resolve().parents[1]


class FolderReleaseTests(unittest.TestCase):
    def test_audio_overlay_preserves_every_unapproved_engine_function(self):
        reviewed=(ROOT/'apps/voice-studio/part01/audio_downloader_api.py').read_bytes()
        # An extra installed engine function is retained even when absent from reviewed source.
        lines=reviewed.decode().splitlines(keepends=True)
        helper=next(n for n in ast.parse(reviewed).body if isinstance(n,ast.FunctionDef) and n.name=='_song_folder')
        del lines[helper.lineno-1:helper.end_lineno]
        installed=''.join(lines).encode()+b'\n\ndef deployed_engine_canary():\n    return "keep installed engine"\n'
        result=release.audio_overlay(installed,reviewed)
        self.assertIn(b'deployed_engine_canary',result)
        before={n.name:ast.dump(n,include_attributes=False) for n in ast.parse(installed).body if isinstance(n,ast.FunctionDef)}
        after={n.name:ast.dump(n,include_attributes=False) for n in ast.parse(result).body if isinstance(n,ast.FunctionDef)}
        for name in before:
            if name not in release.ALLOWED:self.assertEqual(before[name],after[name],name)
        self.assertIn(b'song_folder_id',result)

    def test_coordinated_rollout_installs_consumers_before_downloader(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old=root/'old';old.mkdir();state=root/'state';(state/'state').mkdir(parents=True)
            calls=[]
            with patch.object(release.runtime,'BASE',root),patch.object(release.maker,'STATE',state),\
                 patch.object(release.maker,'CURRENT',old),patch.object(release.audio,'CURRENT',old),\
                 patch.object(release.maker,'require_idle_jobs'),patch.object(release.runtime,'require_lum_release_idle'),\
                 patch.object(release,'require_idle_audio_downloader'),patch.object(release.runtime,'event'),\
                 patch.object(release.maker,'install',side_effect=lambda *a:calls.append('maker')),\
                 patch.object(release.runtime,'install_app',side_effect=lambda *a:calls.append('uploader')),\
                 patch.object(release.audio,'install',side_effect=lambda *a,**k:calls.append('downloader')),\
                 patch.object(release,'verify',return_value={'readback':'PASS'}):
                release.install('a'*40)
            self.assertEqual(calls,['maker','uploader','downloader'])
            self.assertTrue((root/'transactions/song-folder-workflow'/('a'*40)/'result.json').is_file())

    def test_internal_destructive_endpoint_is_outside_public_nginx_prefix(self):
        app=(ROOT/'apps/song-maker/part01/songmaker/app.py').read_text()
        self.assertIn("@app.post('/internal/song-folders/",app)
        self.assertIn("request.client.host not in {'127.0.0.1','::1'}",app)
        self.assertIn("request.headers.get('origin')",app)
        runtime=(ROOT/'production/runtime.py').read_text()
        self.assertIn("args.apps != ['local-upload-manager']",runtime)
        self.assertIn("[vps1-song-folder-flow]",runtime)

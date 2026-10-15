import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from rebuild import builder

class FreshVpsSafety(unittest.TestCase):
 def test_fixed_scope(self):
  c=builder.config()
  self.assertEqual(len(c['groups']),11)
  self.assertTrue(all(not c[k] for k in ('production_deploy_enabled','fresh_install_enabled','legacy_database_import','legacy_queue_import','legacy_media_import')))
 def test_never_import_algrow(self):
  self.assertNotIn('algrow-voice', [g['id'] for g in builder.config()['groups']])
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'algrow_voice.py').write_text('print(1)')
   with self.assertRaises(ValueError):builder.file_list(p)
 def test_secrets_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'.env').write_text('password=hidden')
   with self.assertRaises(ValueError):builder.file_list(p)
 def test_private_key_content_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'test.py').write_text('"-----BEGIN RSA PRIVATE KEY-----"')
   with self.assertRaises(ValueError):builder.file_list(p)
 def test_order(self):
  ordered=builder.ordered_groups();self.assertEqual(len(ordered),11)
  self.assertLess(ordered.index('vps-storage-manager'),ordered.index('scene-builder'))
 def test_missing_sources_fail_closed(self):
  self.assertFalse(builder.state()['ready'])
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(ValueError):builder.stage_rootfs(Path(d))
 def test_archive_sha256_and_tamper(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);app=root/'apps'/'video-merger';app.mkdir(parents=True);(app/'main.py').write_text('print("ok")\n')
   g={'id':'video-merger','parallel_owner':False}
   with patch.object(builder,'source_path',return_value=app):
    p=builder.artifact('video-merger','a'*40,root/'dist')
    self.assertEqual(builder.verify(p),'video-merger')
    m=json.loads(p.read_text());archive=p.parent/m['archive'];archive.write_bytes(archive.read_bytes()+b'tamper')
    with self.assertRaises(ValueError):builder.verify(p)
 def test_no_live_prod_install_command(self):
  body=Path(builder.__file__).read_text()
  self.assertNotIn('systemctl start',body)
  self.assertNotIn('shutil.rmtree',body)

if __name__=='__main__':unittest.main()

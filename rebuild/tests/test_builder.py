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
 def test_lum_python_db_package_allowed_only_for_verified_parts(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   app=root/'apps'/'local-upload-manager'
   for part in ('part01','part02','part03'):
    pkg=app/part/'desktop_core'/'modules'/'db'
    pkg.mkdir(parents=True)
    (pkg/'__init__.py').write_text('from .repository import QueueRepository\n')
    (pkg/'repository.py').write_text('class QueueRepository: pass\n')
   with patch.object(builder,'ROOT',root):
    names={name for name,_ in builder.file_list(app)}
   self.assertEqual(len(names),6)
   self.assertIn('part01/desktop_core/modules/db/repository.py',names)
   self.assertIn('part02/desktop_core/modules/db/repository.py',names)
   self.assertIn('part03/desktop_core/modules/db/repository.py',names)

 def test_runtime_db_and_other_banned_paths_remain_blocked(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);app=root/'apps'/'local-upload-manager'
   package=app/'part01/desktop_core/modules/db'
   package.mkdir(parents=True);(package/'__init__.py').write_text('pass\n')
   with patch.object(builder,'ROOT',root):
    for relative,body in (
      ('part01/desktop_core/modules/db/jobs.sqlite3','SQLite format 3'),
      ('part01/desktop_core/modules/db/config.json','{}'),
      ('part01/desktop_core/modules/db/.env','SECRET=x'),
      ('part01/desktop_core/modules/db/nested/module.py','pass'),
      ('part01/data/queue.py','pass'),
      ('part01/desktop_core/modules/secrets/token.py','pass'),
    ):
     p=app/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body)
     with self.subTest(path=relative),self.assertRaises(ValueError):
      builder.file_list(app)
     p.unlink()

 def test_lum_secret_store_module_is_code_only_not_credentials(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);app=root/'apps'/'local-upload-manager'
   pkg=app/'part02/desktop_core/modules';pkg.mkdir(parents=True)
   source=pkg/'secret_store.py'
   source.write_text('def secure_file_for_current_user(path): return path\n')
   with patch.object(builder,'ROOT',root):
    self.assertEqual(builder.file_list(app)[0][0],
                     'part02/desktop_core/modules/secret_store.py')
    source.write_text('access_token = "ghp_'+'a'*32+'"\n')
    with self.assertRaises(ValueError):builder.file_list(app)
    source.unlink()
    bad=pkg/'my_secret.py';bad.write_text('pass\n')
    with self.assertRaises(ValueError):builder.file_list(app)

 def test_other_apps_cannot_use_db_package_exception(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);app=root/'apps'/'voice-studio'
   pkg=app/'part01/desktop_core/modules/db'
   pkg.mkdir(parents=True);(pkg/'__init__.py').write_text('pass\n')
   with patch.object(builder,'ROOT',root),self.assertRaises(ValueError):
    builder.file_list(app)

 def test_source_db_package_secret_and_symlink_checks_still_apply(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);app=root/'apps'/'local-upload-manager'
   pkg=app/'part01/desktop_core/modules/db'
   pkg.mkdir(parents=True);p=pkg/'__init__.py'
   with patch.object(builder,'ROOT',root):
    p.write_text('token="ghp_'+'a'*32+'"\n')
    with self.assertRaises(ValueError):builder.file_list(app)
    p.unlink()
    target=root/'outside.py';target.write_text('pass\n');p.symlink_to(target)
    with self.assertRaises(ValueError):builder.file_list(app)

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

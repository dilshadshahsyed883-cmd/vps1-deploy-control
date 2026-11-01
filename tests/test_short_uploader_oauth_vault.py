"""Short Uploader only: owner-private JSON vault, admin/member parity, no file-count cap."""
from __future__ import annotations
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT/"apps/local-upload-manager/part01"

class ShortVaultContracts(unittest.TestCase):
    def test_source_manifest_and_template(self):
        app=(APP_ROOT/"lum_engine/standalone_app.py").read_text()
        ui=(APP_ROOT/"templates/app.html").read_text()
        rel=(ROOT/"production/short_uploader_group_release.py").read_text()
        run=(ROOT/"production/runtime.py").read_text()
        workflow=(ROOT/".github/workflows/vps1-short-studio-release.yml").read_text()
        js=(APP_ROOT/"static/short_vault_upload.js").read_text()
        Environment().parse(ui)
        self.assertIn('data-short-shared-client',ui)
        self.assertIn('data-short-vault-token-form',ui)
        self.assertIn('/actions/short/credentials/upload',ui)
        self.assertIn('/actions/oauth-client',ui)
        self.assertIn('async def short_credentials_upload',app)
        self.assertIn('if os.getenv("LUM_ENGINE_INSTANCE") != "shortstudio"',app)
        self.assertNotIn('len(files) > (1 if kind=="client" else 50)',app[app.index('async def short_credentials_upload'):])
        self.assertIn('chunkSize = 32',js)
        for path in ('lum_engine/oauth_web.py','lum_engine/song_vault.py','static/short_vault_upload.js'):
            self.assertIn('"'+path+'"',rel)
        self.assertIn('vault_only=True',run)
        self.assertIn('[vps1-shortstudio-oauth-vault]',workflow)
        self.assertIn('SHORT_VAULT_LIVE=PASS',rel)
        self.assertIn('AUTO_SCRIPT=OFFLINE',workflow)

    def test_real_http_admin_member_and_foreign_rejection(self):
        try:
            import fastapi, httpx, google_auth_oauthlib
        except ImportError:
            self.skipTest("full HTTP dependencies unavailable in this environment")
        # Run in a disposable subprocess so all OAuth folders and queue paths are isolated.
        script=r'''
import json,re
from pathlib import Path
from fastapi.testclient import TestClient
from lum_engine.standalone_app import app,db,studio_tenants
from lum_engine.song_vault import owner_vault_inventory,_token_root
from lum_engine.oauth_web import _owner_client_path
uc_a='UC'+'a'*22; uc_b='UC'+'b'*22
a=studio_tenants.create_channel('Alice',uc_a,'Alice','alice@example.com')
b=studio_tenants.create_channel('Bob',uc_b,'Bob','bob@example.com')
cfg=json.dumps({'installed':{'client_id':'offline.apps.googleusercontent.com',
'project_id':'offline','auth_uri':'https://accounts.google.com/o/oauth2/auth',
'token_uri':'https://oauth2.googleapis.com/token','client_secret':'dummy',
'redirect_uris':['http://localhost:8765/']}}).encode()
tok=json.dumps({'client_id':'offline.apps.googleusercontent.com','client_secret':'dummy',
'token_uri':'https://oauth2.googleapis.com/token','refresh_token':'offline-secret'}).encode()
def login(name,admin=False):
  return TestClient(app,follow_redirects=False,headers={
    'X-Auth-User':name,'X-Is-Admin':'1' if admin else '0','X-App-Access':'lum'})
def ready(client):
  r=client.get('/short-uploader/app?tab=channels')
  assert r.status_code==200,r.text[:500]
  for marker in ('data-short-shared-client','Permanent OAuth &amp; Token Vault',
    '/short-uploader/actions/short/credentials/upload'):
    assert marker in r.text,marker
  return re.search(r'name="csrf_token" value="([^"]+)"',r.text).group(1)
alice=login('Alice');bob=login('Bob');admin=login('Admin',True)
ca=ready(alice);cb=ready(bob);cad=ready(admin)
invalid=alice.post('/short-uploader/actions/short/credentials/upload',
  data={'csrf_token':'invalid','secret_kind':'token'},
  files={'files':(uc_a+'.json',tok,'application/json')})
assert invalid.status_code==403
foreign=alice.post('/short-uploader/actions/short/credentials/upload',
  data={'csrf_token':ca,'secret_kind':'token'},
  files={'files':(uc_b+'.json',tok,'application/json')})
assert foreign.status_code==303 and 'error=' in foreign.headers['location']
assert not db.get_channel(b)['token_file_path']
stored=alice.post('/short-uploader/actions/short/credentials/upload',
  data={'csrf_token':ca,'secret_kind':'token'},
  files={'files':(uc_a+'.json',tok,'application/json')})
assert stored.status_code==303 and 'error=' not in stored.headers['location'],stored.headers['location']
assert _token_root('Alice') in Path(db.get_channel(a)['token_file_path']).parents
client=alice.post('/short-uploader/actions/oauth-client',data={'csrf_token':ca},
  files={'client_json':('client.json',cfg,'application/json')})
assert client.status_code==303 and 'error=' not in client.headers['location']
assert _owner_client_path('Alice').is_file()
assert not _owner_client_path('Bob').exists()
# Verify many files in one request; no 50-file user cap.
many=[('files',(uc_a+'.json',tok,'application/json')) for _ in range(65)]
bulk=alice.post('/short-uploader/actions/short/credentials/upload',
  data={'csrf_token':ca,'secret_kind':'token'},files=many,headers={'Accept':'application/json'})
assert bulk.status_code==200,bulk.text
assert bulk.json()['kept']==65,bulk.text
assert owner_vault_inventory(db,studio_tenants,'Alice')['token_valid']==1
assert owner_vault_inventory(db,studio_tenants,'Bob')['token_valid']==0
admin_client=admin.post('/short-uploader/actions/oauth-client',data={'csrf_token':cad},
 files={'client_json':('admin.json',cfg,'application/json')})
assert admin_client.status_code==303
assert _owner_client_path('Admin').is_file()
assert _owner_client_path('Admin')!=_owner_client_path('Alice')
print('SHORT_VAULT_HTTP=PASS')
'''
        with tempfile.TemporaryDirectory() as temp:
            env={**os.environ,'LUM_DATA_DIR':temp,'LUM_SESSION_SECRET':'offline-test',
                 'LUM_SUITE_AUTH':'1','LUM_BASE_PATH':'/short-uploader',
                 'LUM_ENGINE_INSTANCE':'shortstudio',
                 'YOUTUBE_UPLOAD_MANAGER_DATA_DIR':str(Path(temp)/'core'),
                 'LUM_AUTO_ENGINE_V6_ROOT':str(Path(temp)/'v6'),
                 'LUM_GOOGLE_OAUTH_ROOT':str(Path(temp)/'private-oauth'),
                 'PYTHONDONTWRITEBYTECODE':'1'}
            result=subprocess.run([sys.executable,'-c',script],cwd=APP_ROOT,env=env,
                                  capture_output=True,text=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('SHORT_VAULT_HTTP=PASS',result.stdout)

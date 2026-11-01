"""Copyright PC JSON Vault: owner-only file storage, HTTP CSRF and role parity.

Runs with disposable data roots; never uses production OAuth or external APIs.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY = ROOT / "apps/copyright-uploader/part01"


class CopyrightOwnerVaultTests(unittest.TestCase):
    def test_vault_renders_for_admin_and_nonadmin(self):
        from production.copyright_uploader_browser import fixture
        for admin in (True, False):
            with self.subTest(admin=admin):
                html = fixture("channels", is_admin=admin)
                self.assertIn("Permanent OAuth &amp; Token Vault", html)
                self.assertIn("1. Shared OAuth Client", html)
                self.assertIn("Google Desktop OAuth client JSON", html)
                self.assertIn("Set / Replace Shared Client", html)
                self.assertIn('/actions/oauth-client', html)
                self.assertIn("Upload OAuth client JSON", html)
                self.assertIn("Upload authorized token JSONs", html)
                self.assertIn('name="replace_existing"', html)
                self.assertIn('/actions/copyright/credentials/upload', html)
                self.assertIn("Store &amp; Reconcile", html)
                self.assertNotIn("Administrator access is required to upload permanent credential files",html)

    def test_private_token_import_and_authenticated_http(self):
        script = r"""
import json,re
from pathlib import Path
from fastapi.testclient import TestClient
from lum_engine.standalone_app import app,db,studio_tenants,store
from lum_engine.copyright_vault import import_token,import_client,owner_vault_inventory,_token_root
from lum_engine.oauth_web import _owner_client_path

alice_id='UC'+'a'*22
bob_id='UC'+'b'*22
a=studio_tenants.create_channel('Alice',alice_id,'Alice Private','alice@example.com')
b=studio_tenants.create_channel('Bob',bob_id,'Bob Private','bob@example.com')
base={
  'client_id':'offline-test.apps.googleusercontent.com',
  'client_secret':'disposable-local-only',
  'token_uri':'https://oauth2.googleapis.com/token',
  'refresh_token':'disposable-refresh-token'
}
token=json.dumps(base).encode()
client_json=json.dumps({'installed':{
  'client_id':'offline-test.apps.googleusercontent.com',
  'project_id':'vault-fixture',
  'auth_uri':'https://accounts.google.com/o/oauth2/auth',
  'token_uri':'https://oauth2.googleapis.com/token',
  'client_secret':'dummy',
  'redirect_uris':['http://localhost:8765/']
}}).encode()

def web(name,is_admin=False):
  return TestClient(app,follow_redirects=False,headers={
    'X-Auth-User':name,'X-Is-Admin':'1' if is_admin else '0','X-App-Access':'lum'
  })
def csrf(client):
  r=client.get('/copyright-uploader/app?tab=channels')
  assert r.status_code==200,r.text[:450]
  assert 'Store &amp; Reconcile' in r.text
  assert 'Upload OAuth client JSON' in r.text
  m=re.search(r'name="csrf_token" value="([^"]+)"',r.text)
  assert m
  return m.group(1),r.text

alice=web('Alice')
bob=web('Bob')
admin=web('Admin',True)
tok_a,html_a=csrf(alice)
tok_b,html_b=csrf(bob)
tok_admin,html_admin=csrf(admin)
assert 'BOB_PRIVATE' not in html_a and 'Alice Private' in html_a
assert 'Bob Private' in html_b and 'Alice Private' not in html_b
assert 'Bob Private' not in html_admin and 'Alice Private' not in html_admin
for rendered in (html_a,html_b,html_admin):
  assert "1. Shared OAuth Client" in rendered
  assert "Set / Replace Shared Client" in rendered
  assert '/actions/oauth-client' in rendered

invalid=alice.post('/copyright-uploader/actions/copyright/credentials/upload',
  data={'csrf_token':'invalid','secret_kind':'token'},
  files={'files':(alice_id+'.json',token,'application/json')})
assert invalid.status_code==403

foreign=alice.post('/copyright-uploader/actions/copyright/credentials/upload',
  data={'csrf_token':tok_a,'secret_kind':'token'},
  files={'files':(bob_id+'.json',token,'application/json')})
assert foreign.status_code==303 and 'error=' in foreign.headers['location']
assert not db.get_channel(b)['token_file_path']

saved=alice.post('/copyright-uploader/actions/copyright/credentials/upload',
  data={'csrf_token':tok_a,'secret_kind':'token'},
  files={'files':(alice_id+'.json',token,'application/json')})
assert saved.status_code==303 and 'error=' not in saved.headers['location'],saved.headers['location']
alice_path=Path(db.get_channel(a)['token_file_path'])
assert alice_path.is_file()
assert _token_root('Alice') in alice_path.parents
assert _token_root('Bob') not in alice_path.parents

# Valid old token remains on imports without explicit replace.
other=json.dumps(dict(base,refresh_token='another-offline-refresh')).encode()
kept=import_token(db,studio_tenants,'Alice',alice_id+'.json',other)
assert kept['status']=='KEPT'
assert alice_path.read_bytes()==token
replacement=import_token(db,studio_tenants,'Alice',alice_id+'.json',other,replace_valid=True)
assert replacement['status']=='INSTALLED'
assert alice_path.read_bytes()==other
assert not db.get_channel(b)['token_file_path']

# Gmail filename allowed only if unique across all users/brands.
saved_b=import_token(db,studio_tenants,'Bob','bob@example.com.json',token)
assert saved_b['status']=='INSTALLED',saved_b
bob_path=Path(db.get_channel(b)['token_file_path'])
assert bob_path.is_file() and bob_path!=alice_path and bob_path.read_bytes()==token
assert owner_vault_inventory(db,studio_tenants,'Alice')['token_valid']==1
assert owner_vault_inventory(db,studio_tenants,'Bob')['token_valid']==1
assert owner_vault_inventory(db,studio_tenants,'Admin')['token_total']==0

# Regression: more than 50 distinct owned PC JSONs in one authenticated batch.
# No arbitrary 50-file limit; each token still passes file-size and owner checks.
batch_ids=['UC'+f'{i:022d}' for i in range(51)]
for idx,cid in enumerate(batch_ids):
  studio_tenants.create_channel('Alice',cid,f'Alice Batch {idx}',f'batch{idx}@example.com')
bulk=alice.post('/copyright-uploader/actions/copyright/credentials/upload',
  data={'csrf_token':tok_a,'secret_kind':'token'},
  files=[('files',(cid+'.json',token,'application/json')) for cid in batch_ids])
assert bulk.status_code==303 and 'error=' not in bulk.headers['location'],bulk.headers['location']
assert owner_vault_inventory(db,studio_tenants,'Alice')['token_valid']==52
assert owner_vault_inventory(db,studio_tenants,'Bob')['token_valid']==1
assert owner_vault_inventory(db,studio_tenants,'Admin')['token_total']==0
for cid in batch_ids:
  assert _token_root('Alice').joinpath(cid+'.json').is_file()
# Keep one-client-per-request contract intact, without changing existing state.
multi_client=admin.post('/copyright-uploader/actions/copyright/credentials/upload',
  data={'csrf_token':tok_admin,'secret_kind':'client'},
  files=[('files',('one.json',client_json,'application/json')),
         ('files',('two.json',client_json,'application/json'))])
assert multi_client.status_code==303 and 'error=' in multi_client.headers['location']
assert not _owner_client_path('Admin').exists()

c=admin.post('/copyright-uploader/actions/copyright/credentials/upload',
  data={'csrf_token':tok_admin,'secret_kind':'client'},
  files={'files':('desktop-client.json',client_json,'application/json')})
assert c.status_code==303 and 'error=' not in c.headers['location'],c.headers['location']
assert _owner_client_path('Admin').is_file()
assert not _owner_client_path('Alice').exists()
# Restore the original shared-client card upload action for non-admin, too.
shared_card=alice.post('/copyright-uploader/actions/oauth-client',
  data={'csrf_token':tok_a},
  files={'client_json':('shared_oauth_client.json',client_json,'application/json')})
assert shared_card.status_code==303 and 'error=' not in shared_card.headers['location'],shared_card.headers['location']
assert _owner_client_path('Alice').is_file()
assert _owner_client_path('Alice')!=_owner_client_path('Admin')
assert _owner_client_path('Admin').read_bytes()==client_json
assert _owner_client_path('Bob').exists() is False
# User-specific replacement must not modify the other user's configured client.
new_client_json=json.dumps({'installed':dict(json.loads(client_json)['installed'],
  client_id='replacement-disposable.apps.googleusercontent.com')}).encode()
changed=alice.post('/copyright-uploader/actions/oauth-client',
  data={'csrf_token':tok_a},
  files={'client_json':('replace.json',new_client_json,'application/json')})
assert changed.status_code==303 and 'error=' not in changed.headers['location'],changed.headers['location']
assert _owner_client_path('Alice').read_bytes()==new_client_json
assert _owner_client_path('Admin').read_bytes()==client_json
assert import_client('Admin',b'bad JSON')['status']=='KEPT'
assert _owner_client_path('Admin').is_file()
assert 'PRIVATE_VAULT_TEST' not in html_a
print('PRIVATE_VAULT_TEST=PASS')
"""
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "LUM_DATA_DIR":tmp,"LUM_SESSION_SECRET":"offline-vault-secret",
                   "LUM_SUITE_AUTH":"1","LUM_BASE_PATH":"/copyright-uploader",
                   "LUM_ENGINE_INSTANCE":"copyright",
                   "YOUTUBE_UPLOAD_MANAGER_DATA_DIR":str(Path(tmp)/"core"),
                   "LUM_AUTO_ENGINE_V6_ROOT":str(Path(tmp)/"v6"),
                   "LUM_GOOGLE_OAUTH_ROOT":str(Path(tmp)/"secure-oauth"),
                   "PYTHONDONTWRITEBYTECODE":"1"}
            run=subprocess.run([sys.executable,"-c",script],cwd=str(COPY),env=env,
                               capture_output=True,text=True,timeout=120)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertIn("PRIVATE_VAULT_TEST=PASS",run.stdout)


if __name__=="__main__":
    unittest.main()

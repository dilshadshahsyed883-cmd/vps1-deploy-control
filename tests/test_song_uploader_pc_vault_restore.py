"""Song PC JSON Vault: owner-only file storage, shared fallback and equal-role controls.

Runs with disposable data roots; never uses production OAuth or external APIs.
"""
from __future__ import annotations

import os
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SONG = ROOT / "apps/local-upload-manager/part01"


class SongOwnerVaultTests(unittest.TestCase):
    def test_ui_contract_and_release_files(self):
        template=(SONG/"templates/app.html").read_text(encoding="utf-8")
        release=(ROOT/"production/song_uploader_group_summary_release.py").read_text(encoding="utf-8")
        self.assertIn('data-song-shared-client',template)
        self.assertIn("1. Shared OAuth Client",template)
        self.assertIn("Permanent OAuth &amp; Token Vault",template)
        self.assertIn('/actions/song/credentials/upload',template)
        self.assertIn("Store &amp; Reconcile",template)
        self.assertIn('"lum_engine/oauth_web.py"',release)
        self.assertIn('"lum_engine/song_vault.py"',release)
        self.assertIn('NEW_FILES=frozenset({"lum_engine/studio_tenants.py","lum_engine/song_vault.py"})',release)

    @unittest.skipUnless(importlib.util.find_spec("fastapi") is not None, "Real HTTP dependencies required")
    def test_private_token_import_and_authenticated_http(self):
        script = r"""
import json,re
from pathlib import Path
from fastapi.testclient import TestClient
from lum_engine.standalone_app import app,db,studio_tenants,store
from lum_engine.song_vault import import_token,import_client,owner_vault_inventory,_token_root
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
  r=client.get('/song-uploader/app?tab=channels')
  assert r.status_code==200,r.text[:450]
  assert 'Store &amp; Reconcile' in r.text and '1. Shared OAuth Client' in r.text and 'data-song-shared-client' in r.text
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
assert '1. Shared OAuth Client' in html_a and 'Permanent OAuth &amp; Token Vault' in html_a
assert '1. Shared OAuth Client' in html_b and 'Permanent OAuth &amp; Token Vault' in html_b
assert '1. Shared OAuth Client' in html_admin and 'Permanent OAuth &amp; Token Vault' in html_admin
assert '/song-uploader/actions/oauth-client' in html_a
assert '/song-uploader/actions/oauth-client' in html_admin

invalid=alice.post('/song-uploader/actions/song/credentials/upload',
  data={'csrf_token':'invalid','secret_kind':'token'},
  files={'files':(alice_id+'.json',token,'application/json')})
assert invalid.status_code==403

foreign=alice.post('/song-uploader/actions/song/credentials/upload',
  data={'csrf_token':tok_a,'secret_kind':'token'},
  files={'files':(bob_id+'.json',token,'application/json')})
assert foreign.status_code==303 and 'error=' in foreign.headers['location']
assert not db.get_channel(b)['token_file_path']

saved=alice.post('/song-uploader/actions/song/credentials/upload',
  data={'csrf_token':tok_a,'secret_kind':'token'},
  files={'files':(alice_id+'.json',token,'application/json')})
assert saved.status_code==303 and 'error=' not in saved.headers['location'],saved.headers['location']
alice_path=Path(db.get_channel(a)['token_file_path'])
assert alice_path.is_file()
assert _token_root('Alice') in alice_path.parents
assert _token_root('Bob') not in alice_path.parents

# The Shared Client card must never let any account replace the global
# credential: uploads are isolated in that user's own private path.
user_client=alice.post('/song-uploader/actions/oauth-client',
  data={'csrf_token':tok_a},
  files={'client_json':('desktop-client.json',client_json,'application/json')})
assert user_client.status_code==303 and 'error=' not in user_client.headers['location']
assert _owner_client_path('Alice').is_file()
assert _owner_client_path('Alice')!=_owner_client_path('Bob')

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

c=admin.post('/song-uploader/actions/song/credentials/upload',
  data={'csrf_token':tok_admin,'secret_kind':'client'},
  files={'files':('desktop-client.json',client_json,'application/json')})
assert c.status_code==303 and 'error=' not in c.headers['location'],c.headers['location']
assert _owner_client_path('Admin').is_file()
assert _owner_client_path('Alice').exists() and _owner_client_path('Alice') != _owner_client_path('Admin')
assert import_client('Admin',b'bad JSON')['status']=='KEPT'
assert _owner_client_path('Admin').is_file()
assert 'PRIVATE_VAULT_TEST' not in html_a

# Regression: legitimate multi-channel imports are not arbitrarily rejected
# at 51 files (or 100+). Non-admin and admin both operate only on own channels.
bob_original = bob_path.read_bytes()
for owner, client, csrf_token, count in (
    ('Alice', alice, tok_a, 101),
    ('Admin', admin, tok_admin, 51),
):
    own_ids = []
    for index in range(count):
        channel_id = 'UC' + f'{index + (1000 if owner == "Admin" else 1):022d}'
        studio_tenants.create_channel(
            owner, channel_id, f'{owner} bulk {index}',
            f'{owner.lower()}-bulk-{index}@example.invalid'
        )
        own_ids.append(channel_id)
    response = client.post('/song-uploader/actions/song/credentials/upload',
        data={'csrf_token':csrf_token,'secret_kind':'token'},
        files=[('files',(channel_id+'.json',token,'application/json'))
               for channel_id in own_ids])
    assert response.status_code == 303, response.text[:300]
    assert 'error=' not in response.headers['location'],response.headers['location']
    assert f'installed+{count}' in response.headers['location'],response.headers['location']
    for channel_id in own_ids:
        with db.connect() as conn:
            row = conn.execute('SELECT token_file_path FROM channels WHERE channel_id=?',
                               (channel_id,)).fetchone()
        assert row is not None and row['token_file_path'], channel_id
        imported = Path(row['token_file_path'])
        assert imported.read_bytes() == token and _token_root(owner) in imported.parents
assert bob_path.read_bytes() == bob_original

# The OAuth-client credential itself remains exactly one file per request.
two_clients = alice.post('/song-uploader/actions/song/credentials/upload',
    data={'csrf_token':tok_a,'secret_kind':'client','replace_existing':'1'},
    files=[('files',('a.json',client_json,'application/json')),
           ('files',('b.json',client_json,'application/json'))])
assert two_clients.status_code == 303 and 'error=' in two_clients.headers['location']
assert _owner_client_path('Alice').read_bytes() == client_json
print('PRIVATE_VAULT_TEST=PASS')
"""
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "LUM_DATA_DIR":tmp,"LUM_SESSION_SECRET":"offline-vault-secret",
                   "LUM_SUITE_AUTH":"1","LUM_BASE_PATH":"/song-uploader",
                   "LUM_ENGINE_INSTANCE":"song",
                   "YOUTUBE_UPLOAD_MANAGER_DATA_DIR":str(Path(tmp)/"core"),
                   "LUM_AUTO_ENGINE_V6_ROOT":str(Path(tmp)/"v6"),
                   "LUM_GOOGLE_OAUTH_ROOT":str(Path(tmp)/"secure-oauth"),
                   "PYTHONDONTWRITEBYTECODE":"1"}
            run=subprocess.run([sys.executable,"-c",script],cwd=str(SONG),env=env,
                               capture_output=True,text=True,timeout=60)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertIn("PRIVATE_VAULT_TEST=PASS",run.stdout)


if __name__=="__main__":
    unittest.main()

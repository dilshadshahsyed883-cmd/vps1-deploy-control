"""Copyright Sheet Group column reconciliation: owner isolation, token safety and HTTP Sync All.

All tests run in disposable SQLite and use mocked sheet rows. No Google calls
or real YouTube uploads occur.
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


class CopyrightOwnedSheetGroups(unittest.TestCase):
    def test_owner_group_reconciliation_and_http_sync(self):
        script = r"""
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
import lum_engine.standalone_app as mod

db=mod.db
tenants=mod.studio_tenants
store=mod.store
a_id='UC'+'a'*22
b_id='UC'+'b'*22
c_id='UC'+'c'*22
a=tenants.create_channel('alice',a_id,'Alice Owned','alice@example.com')
b=tenants.create_channel('bob',b_id,'Bob Owned','bob@example.com')
c=tenants.create_channel('alice',c_id,'Alice Second','alice2@example.com')
db.update_channel_token_file(a,'/keep/alice-authorized.json','ACTIVE')
db.update_channel_token_file(b,'/keep/bob-authorized.json','ACTIVE')
with db.connect() as con:
    con.execute("INSERT INTO sheet_sources(sheet_name,sheet_url_or_id,gid,json_folder_path,enabled) VALUES ('Alice Source 1','alice-src-1','FIRST_TAB','/unused',1)")
    s1=con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute("INSERT INTO sheet_sources(sheet_name,sheet_url_or_id,gid,json_folder_path,enabled) VALUES ('Alice Source 2','alice-src-2','FIRST_TAB','/unused',1)")
    s2=con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute("INSERT INTO sheet_sources(sheet_name,sheet_url_or_id,gid,json_folder_path,enabled) VALUES ('Bob Source','bob-src','FIRST_TAB','/unused',1)")
    sb=con.execute("SELECT last_insert_rowid()").fetchone()[0]
tenants.bind('sheet',s1,'alice')
tenants.bind('sheet',s2,'alice')
tenants.bind('sheet',sb,'bob')
foreign_group=tenants.create_group('bob','Hindi',[b])
manual_group=tenants.create_group('alice','Manual',[a])

def parsed(key,cid,name,email,group):
    return SimpleNamespace(key=key,channel_id=cid,channel_name=name,gmail=email,
                           notes='',group=group)
alice_row=parsed('row-a',a_id,'Alice Owned','alice@example.com','Hindhi, English')
alice_other=parsed('row-c',c_id,'Alice Second','alice2@example.com','English')
bob_row=parsed('row-b',b_id,'Bob Owned','bob@example.com','Hindi')

result=tenants.sync_owned_sheet_rows('alice',s1,[alice_row],sync_sheet_groups=True)
assert result['rows']==1 and result['groups_touched']==2,result
assert db.get_channel(a)['token_file_path']=='/keep/alice-authorized.json'
assert db.get_channel(b)['token_file_path']=='/keep/bob-authorized.json'
assert tenants.owner('group',foreign_group)=='bob'
group_map=db.channel_group_names_map()
assert 'English' in group_map[a] and 'Hindi (alice)' in group_map[a],group_map
assert 'Manual' in group_map[a] and group_map[b]=='Hindi',group_map
with db.connect() as con:
    bob_members=con.execute("SELECT channel_db_id FROM channel_group_members WHERE group_id=?",(foreign_group,)).fetchall()
assert {row[0] for row in bob_members}=={b}

# Repeated sync is idempotent, and changing only Sheet group removes its
# previously tracked membership without deleting a manual group.
before_count=len(group_map)
tenants.sync_owned_sheet_rows('alice',s1,[alice_row],sync_sheet_groups=True)
assert len(db.channel_group_names_map())==before_count
changed=parsed('row-a',a_id,'Alice Owned','alice@example.com','English')
tenants.sync_owned_sheet_rows('alice',s1,[changed],sync_sheet_groups=True)
after=db.channel_group_names_map()
assert 'English' in after[a] and 'Manual' in after[a],after
assert 'Hindi (alice)' not in after[a] and after[b]=='Hindi',after

# Any foreign-channel row fails the whole transaction without changing groups.
try:
    tenants.sync_owned_sheet_rows('alice',s1,[changed,bob_row],sync_sheet_groups=True)
except PermissionError:
    pass
else:
    raise AssertionError('Cross-owner Sheet row was accepted')
assert db.channel_group_names_map()==after
assert db.get_channel(b)['token_file_path']=='/keep/bob-authorized.json'

# Both authenticated Sync Now and Sync All routes carry actual Sheet groups.
store.set_setting('sheets_sync_enabled','1')
client=TestClient(mod.app,follow_redirects=False,headers={
    'X-Auth-User':'alice','X-Is-Admin':'0','X-App-Access':'lum'})
page=client.get('/copyright-uploader/app?tab=sheets')
assert page.status_code==200,page.text[:400]
csrf=re.search(r'name="csrf_token" value="([^"]+)"',page.text)
assert csrf, 'CSRF token missing'
token=csrf.group(1)
rows_by_source={'alice-src-1':[alice_row],'alice-src-2':[alice_other],
                'bob-src':[bob_row]}
def fetch(src,_):
    return [{'sentinel':src}]
def convert(raw,folder):
    # Verify the correct source is fetched; return parsed groups as Google would.
    return rows_by_source[raw[0]['sentinel']]
with patch.object(mod,'tenant_fetch_sheet_csv',side_effect=fetch),patch.object(
        mod,'tenant_rows_to_sheet_rows',side_effect=convert):
    single=client.post('/copyright-uploader/actions/sheets/sync',
        data={'csrf_token':token,'source_id':str(s1)})
    assert single.status_code==303 and 'error=' not in single.headers['location'],single.headers['location']
    all_sync=client.post('/copyright-uploader/actions/sheets/sync-all',
        data={'csrf_token':token})
    assert all_sync.status_code==303 and 'error=' not in all_sync.headers['location'],all_sync.headers['location']
    denied=client.post('/copyright-uploader/actions/sheets/sync',
        data={'csrf_token':token,'source_id':str(sb)})
    assert denied.status_code==303 and 'error=' in denied.headers['location'],denied.headers['location']
final=db.channel_group_names_map()
assert 'Hindi (alice)' in final[a] and 'English' in final[a],final
assert 'English' in final[c] and final[b]=='Hindi',final
assert db.get_channel(a)['token_file_path']=='/keep/alice-authorized.json'
assert db.get_channel(b)['token_file_path']=='/keep/bob-authorized.json'
assert tenants.allowed_ids('group',{'username':'alice','is_admin':0})
page=client.get('/copyright-uploader/app?tab=channels')
assert page.status_code==200
assert 'Hindi (alice)' in page.text and 'English' in page.text
print('COPYRIGHT_OWNED_SHEET_GROUPS=PASS')
"""
        with tempfile.TemporaryDirectory() as tmp:
            env={**os.environ,
                 "LUM_DATA_DIR":tmp,
                 "LUM_SESSION_SECRET":"disposable-copyright-sheet-groups",
                 "LUM_SUITE_AUTH":"1",
                 "LUM_BASE_PATH":"/copyright-uploader",
                 "LUM_ENGINE_INSTANCE":"copyright",
                 "YOUTUBE_UPLOAD_MANAGER_DATA_DIR":str(Path(tmp)/"core"),
                 "LUM_GOOGLE_OAUTH_ROOT":str(Path(tmp)/"oauth"),
                 "LUM_AUTO_ENGINE_V6_ROOT":str(Path(tmp)/"v6"),
                 "PYTHONDONTWRITEBYTECODE":"1"}
            run=subprocess.run([sys.executable,"-c",script],
                               cwd=str(COPY),env=env,text=True,
                               capture_output=True,timeout=120)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertIn("COPYRIGHT_OWNED_SHEET_GROUPS=PASS",run.stdout)


if __name__=="__main__":
    unittest.main()

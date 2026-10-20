"""Owner correction regressions. No external APIs or livestreams."""
from pathlib import Path
import os
import sys
import tempfile
import shutil
import secrets
import subprocess
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'apps/short-stream/part01'))
import auto_stream_engine as engine
import auto_stream_integrations as integrations
import auto_stream_client_config as credentials
from auto_stream_ui import PAGE

CID='UC'+'A'*22
CLIENT='123456-client.apps.googleusercontent.com'
SECRET=secrets.token_urlsafe(24)
HEADERS={'host':'app.scenebuilder.online','origin':'https://app.scenebuilder.online',
         'x-auth-user':'fixture-admin','x-is-admin':'1','x-auto-stream-config-action':'1'}

class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.media=self.root/'media';self.media.mkdir()
        (self.media/'clip.mp4').write_bytes(b'fixture')
        self.engine=engine.StreamEngine(self.root/'engine',self.media)
        self.accounts=integrations.AutoStreamAccounts(self.root/'accounts')
        self.store=credentials.OAuthClientStore(self.root/'vault')
        env=patch.dict(os.environ,{'AUTO_STREAM_FERNET_KEY':Fernet.generate_key().decode()})
        env.start();self.addCleanup(env.stop)
        for target,name,value in ((engine,'service',self.engine),(integrations,'accounts',self.accounts),(credentials,'store',self.store)):
            ctx=patch.object(target,name,return_value=value);ctx.start();self.addCleanup(ctx.stop)
        app=FastAPI()
        for router in (engine.router,integrations.router,credentials.router):app.include_router(router)
        self.client=TestClient(app);self.addCleanup(self.client.close)

    def test_web_json_upload_encrypts_and_never_returns_secret(self):
        doc={'web':{'client_id':CLIENT,'client_secret':SECRET,'redirect_uris':[credentials.REDIRECT]}}
        response=self.client.post('/engine/oauth/client/json',json=doc,headers=HEADERS)
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn(SECRET,response.text)
        self.assertNotIn(SECRET.encode(),self.store.path.read_bytes())
        self.assertEqual(self.store.read_client(),(CLIENT,SECRET,credentials.REDIRECT))

    def test_reject_bad_credential_types_callback_and_nonadmin(self):
        for doc in ({'installed':{}},{'type':'service_account'},[],{'web':{'client_id':CLIENT,'client_secret':SECRET,'redirect_uris':['http://localhost']}}):
            r=self.client.post('/engine/oauth/client/json',json=doc,headers=HEADERS)
            self.assertEqual(r.status_code,422)
            self.assertNotIn(SECRET,r.text)
        self.assertEqual(self.client.post('/engine/oauth/client/json',json={},headers={}).status_code,403)
        self.assertFalse(self.store.path.exists())

    def test_selected_channel_bound_in_oauth_state(self):
        with patch.object(integrations,'_configuration',return_value=(CLIENT,SECRET,credentials.REDIRECT)):
            self.accounts.begin_oauth(expected_channel=CID)
        with self.accounts.connect() as con:
            row=con.execute('SELECT expected_channel FROM oauth_state').fetchone()
        self.assertEqual(row[0],CID)

    def test_wrong_brand_account_does_not_store_token(self):
        from urllib.parse import urlparse,parse_qs
        with patch.object(integrations,'_configuration',return_value=(CLIENT,SECRET,credentials.REDIRECT)):
            auth=self.accounts.begin_oauth(expected_channel=CID)
            state=parse_qs(urlparse(auth['authorization_url']).query)['state'][0]
            token={'scope':integrations.YOUTUBE_SCOPE,'refresh_token':'private-fixture','access_token':'access-fixture'}
            with patch.object(integrations,'_http_json',side_effect=[token,{'items':[{'id':'UC'+'B'*22,'snippet':{'title':'Wrong Brand'}}]}]):
                with self.assertRaisesRegex(ValueError,'does not match'):self.accounts.finish_oauth(state,'test-code')
        self.assertEqual(self.accounts.channel_catalog(),[])

    def test_named_sheet_discovers_worksheet_and_is_idempotent(self):
        data={'name':'Stream schedule','sheet_url':'https://docs.google.com/spreadsheets/d/'+'a'*20+'/edit#gid=17','channel_id':CID}
        self.accounts.import_metadata(CID,'Fixture')
        metadata={'sheets':[{'properties':{'sheetId':17,'title':"Owner's Schedule"}}]}
        with patch.object(self.accounts,'_access',return_value='fixture'),patch.object(integrations,'_http_json',return_value=metadata):
            one=self.accounts.link_named_sheet(data);two=self.accounts.link_named_sheet(data)
        self.assertEqual(one['id'],two['id'])
        rows=self.accounts.list_sheet_sources()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['name'],'Stream schedule')
        self.assertEqual(rows[0]['a1_range'],"'Owner''s Schedule'!A1:AZ201")

    def test_untrusted_sheet_url_rejected_before_network(self):
        with patch.object(integrations,'_http_json') as remote:
            for url in ('http://docs.google.com/spreadsheets/d/'+'a'*20,'https://evil.example/spreadsheets/d/'+'a'*20):
                with self.assertRaises(ValueError):self.accounts.link_named_sheet({'name':'Test','sheet_url':url,'channel_id':CID})
            remote.assert_not_called()

    def test_job_metadata_and_loop_options_persist(self):
        job=self.engine.create({'channel_id':CID,'media_path':'clip.mp4','title':'Fixture','idempotency_key':'fixture-job-1','loop_count':7,'unlimited':True,'privacy':'unlisted','description':'Keep this'})
        restored=self.engine.get(job['id'])
        self.assertEqual((restored['loop_count'],restored['unlimited'],restored['privacy'],restored['description']),(7,True,'unlisted','Keep this'))
        duplicate=self.client.post('/engine/jobs/'+job['id']+'/duplicate').json()
        self.assertNotEqual(job['id'],duplicate['id']);self.assertEqual(duplicate['loop_count'],7)
        cancelled=self.client.post('/engine/jobs/'+job['id']+'/cancel')
        self.assertEqual(cancelled.json()['status'],'CANCELLED')

    def test_scenes_persist_and_reject_unsupported_sources(self):
        config={'name':'Fixture','active_scene':0,'scenes':[{'name':'Intro','sources':[{'type':'video','media_path':'clip.mp4','volume':0.5}]}]}
        response=self.client.put('/engine/studio/config',json=config)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.get('/engine/studio/config').json()['scenes'][0]['name'],'Intro')
        config['scenes'][0]['sources'][0]['media_path']='../secret.mp4'
        self.assertGreaterEqual(self.client.put('/engine/studio/config',json=config).status_code,400)
        self.assertEqual(self.engine.processes,{})

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required for controlled encoder smoke test')
    def test_real_ffmpeg_encodes_locally_without_network(self):
        clip=self.media/'fixture.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=320x180:r=30',
                        '-f','lavfi','-i','anullsrc=r=44100:cl=stereo','-t','0.4',
                        '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(clip)],check=True,timeout=20,capture_output=True)
        job=self.engine.create({'channel_id':CID,'media_path':'fixture.mp4','title':'Offline FFmpeg','idempotency_key':'offline-ffmpeg'})
        command=self.engine._command(job,'synthetic-key')
        destination=self.root/'local-output.flv'
        command[-1]=str(destination)  # Always a local file; no YouTube socket.
        self.assertFalse(any(x.startswith('rtmp') for x in command))
        subprocess.run(command,check=True,timeout=20,capture_output=True)
        self.assertGreater(destination.stat().st_size,0)
        self.assertEqual(self.engine.processes,{})
        self.assertEqual(self.engine.get(job['id'])['status'],'DRAFT')

    def test_dashboard_aggregates_beyond_paginated_job_list(self):
        with self.engine._connect() as con:
            for i in range(260):
                con.execute("INSERT INTO jobs(id,idem,channel_id,media_path,title,status,created_at,updated_at) VALUES(?,?,?,?,?,'FAILED',?,?)",(str(i),'idem-'+str(i),CID,'clip.mp4','Fixture',engine.utc_now(),engine.utc_now()))
        data=self.client.get('/engine/dashboard').json()
        self.assertEqual(data['failed'],260)
        self.assertIsNone(data['live_verified'])
        self.assertEqual(data['active_encoders'],0)
        self.assertEqual(len(data['recent']),10)

    def test_user_facing_sheet_fields_and_admin_request_header(self):
        self.assertIn('Sheet Name',PAGE);self.assertIn('Google Sheet Link',PAGE)
        self.assertNotIn('A1 range',PAGE);self.assertNotIn('id="sheet-range"',PAGE)
        self.assertIn('X-Auto-Stream-Config-Action',PAGE)
        self.assertIn('Accounts &amp; Tokens',PAGE)
        self.assertIn('expected_channel=',PAGE)

if __name__=='__main__':unittest.main()

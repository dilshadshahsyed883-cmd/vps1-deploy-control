"""Real browser→API→FFmpeg acceptance with mocked Google, no external publishing."""
from pathlib import Path
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
import urllib.request
from cryptography.fernet import Fernet
from fastapi import FastAPI,Request
from fastapi.responses import HTMLResponse
import uvicorn
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'apps/short-stream/part01'))
import auto_stream_engine as e
import auto_stream_integrations as g
import auto_stream_client_config as credentials
import auto_stream_broadcasts as live
from auto_stream_ui import PAGE
CID='UC'+'E'*22

@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg is required')
class BrowserWorkflowTests(unittest.TestCase):
    def test_real_full_local_workflow(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);media=root/'media';media.mkdir()
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:s=320x180:r=30','-t','0.5','-c:v','libx264',str(media/'clip.mp4')],check=True,capture_output=True)
            env={'AUTO_STREAM_FERNET_KEY':Fernet.generate_key().decode(),**{key:'0' for key in ('AUTO_STREAM_PUBLISH_ENABLED','AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED','AUTO_STREAM_LIVE_TRANSITIONS_ENABLED','AUTO_STREAM_GO_LIVE_ENABLED','AUTO_STREAM_SCHEDULER_ENABLED')}}
            engine=e.StreamEngine(root/'engine',media);accounts=g.AutoStreamAccounts(root/'engine');store=credentials.OAuthClientStore(root/'engine');controller=live.BroadcastController(engine=engine,acct=accounts)
            def google(url,**kwargs):
                if url==g.TOKEN_URL:return {'access_token':'fixture-access','refresh_token':'fixture-refresh','scope':g.YOUTUBE_SCOPE+' '+g.SHEETS_SCOPE}
                if '/channels?' in url:return {'items':[{'id':CID,'snippet':{'title':'Fixture Brand Channel'}}]}
                if '/values/' in url:return {'values':[['YouTube Channel ID','Video Path','Stream Title','Scheduled Start'],[CID,'clip.mp4','Sheet stream draft','2026-12-01T10:00:00+05:00']]}
                if url.startswith(g.SHEETS_BASE):return {'sheets':[{'properties':{'sheetId':17,'title':'Schedule'}}]}
                raise AssertionError('Unexpected network provider action')
            app=FastAPI()
            # Trusted gateway simulation is confined to this test app.
            @app.middleware('http')
            async def fixture_gateway(request:Request,call_next):
                request.scope['headers']=[(k,v) for k,v in request.scope['headers'] if k not in (b'host',b'origin',b'x-auth-user',b'x-is-admin')]+[(b'host',b'app.scenebuilder.online'),(b'origin',b'https://app.scenebuilder.online'),(b'x-auth-user',b'fixture-admin'),(b'x-is-admin',b'1')]
                return await call_next(request)
            for router in (e.router,g.router,credentials.router,live.router):app.include_router(router,prefix='/short-stream')
            @app.get('/auto-stream/',response_class=HTMLResponse)
            def studio():return PAGE
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
            with patch.dict(os.environ,env),patch.object(e,'_service',engine),patch.object(e,'ENGINE_ROOT',engine.root),patch.object(g,'_instance',accounts),patch.object(credentials,'_instance',store),patch.object(live,'_controller',controller),patch.object(g,'_http_json',side_effect=google):
                thread=threading.Thread(target=server.run,daemon=True);thread.start()
                try:
                    deadline=time.monotonic()+10
                    while not server.started and time.monotonic()<deadline:time.sleep(0.05)
                    self.assertTrue(server.started)
                    with sync_playwright() as pw:
                        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1280,'height':1000});errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        base=f'http://127.0.0.1:{port}'
                        page.goto(base+'/auto-stream/')
                        page.get_by_role('button',name='Channels',exact=True).click()
                        page.get_by_role('button',name='Accounts & Tokens',exact=True).click()
                        page.get_by_text('1. Shared OAuth Client',exact=True).click()
                        keyfile=root/'web-client.json';keyfile.write_text(json.dumps({'web':{'client_id':'1234-fixture.apps.googleusercontent.com','client_secret':'synthetic-value','redirect_uris':[credentials.REDIRECT]}}))
                        page.locator('#oauth-json').set_input_files(str(keyfile));page.locator('#upload-oauth-json').click()
                        page.locator('#oauth-state').filter(has_text='Encrypted client saved').wait_for()
                        page.locator('#import-channel-id').fill(CID);page.locator('#import-channel-title').fill('Selected Channel');page.locator('#import-channel').click()
                        page.locator('#expected-channel option').wait_for(state='attached')
                        def consent(route):
                            state=parse_qs(urlparse(route.request.url).query)['state'][0]
                            route.fulfill(body='<a href="'+base+'/short-stream/engine/oauth/callback?state='+state+'&code=fixture-code">Approve test channel</a>',content_type='text/html')
                        page.route('https://accounts.google.com/**',consent)
                        page.locator('#connect-selected').click();page.get_by_role('link',name='Approve test channel').click()
                        page.get_by_role('link',name='Return to Auto Stream').click()
                        page.get_by_role('button',name='Sheets Sync',exact=True).click()
                        page.locator('#sheet-channel option').wait_for(state='attached');page.locator('#sheet-name').fill('Fixture schedule');page.locator('#sheet-id').fill('https://docs.google.com/spreadsheets/d/'+'a'*24+'/edit#gid=17')
                        page.locator('#add-sheet').click();page.locator('#sheet-list').get_by_role('button',name='Preview',exact=True).click()
                        page.locator('#sheet-preview').filter(has_text='Sheet stream draft').wait_for()
                        page.locator('#sheet-list').get_by_role('button',name='Sync DRAFTs',exact=True).click()
                        page.get_by_role('button',name='Live Studio',exact=True).click()
                        page.locator('#studio-profile').select_option('480p');page.locator('#studio-duration').fill('1')
                        page.locator('#existing-media').select_option('clip.mp4');page.locator('#add-source').click()
                        page.locator('#source-type').select_option('text');page.locator('#source-text').fill('Real composed preview');page.locator('#add-source').click()
                        page.locator('#preview-scene').click();page.locator('#scene-preview-image').wait_for(state='visible',timeout=30000)
                        page.wait_for_function('document.getElementById("scene-preview-image").naturalWidth>0')
                        self.assertEqual(page.locator('#scene-preview-image').evaluate('(image)=>image.naturalWidth'),854)
                        page.locator('#render-scene').click();page.locator('#render-state').filter(has_text=re.compile(r'^READY$')).wait_for(timeout=30000)
                        self.assertTrue(page.locator('#job-media').input_value().startswith('auto-stream/renders/'))
                        page.wait_for_function('document.getElementById("composition-preview").videoWidth>0',timeout=15000)
                        page.locator('#job-title').fill('Composed draft');page.locator('#create-job').click()
                        page.get_by_role('button',name='Live Queue',exact=True).click()
                        page.locator('#job-list').get_by_text('Composed draft',exact=False).wait_for()
                        self.assertEqual(len(engine.list_jobs()),2)
                        self.assertTrue(all(j['status']=='DRAFT' for j in engine.list_jobs()))
                        self.assertEqual(engine.processes,{})
                        self.assertFalse(engine.health()['publishing_enabled'])
                        self.assertEqual(errors,[])
                        artifacts=Path(os.environ.get('AUTO_STREAM_TEST_ARTIFACTS',str(root/'screenshots')));artifacts.mkdir(parents=True,exist_ok=True)
                        for width in (360,390,768,1280,1920):
                            page.set_viewport_size({'width':width,'height':1000})
                            for tab in ('Dashboard','Channels','Sheets Sync','Live Studio','Live Queue'):
                                page.get_by_role('button',name=tab,exact=True).click()
                                overflow=page.evaluate('document.documentElement.scrollWidth>window.innerWidth')
                                if overflow:
                                    page.screenshot(path=str(artifacts/f'overflow-{tab.replace(" ","-")}-{width}.png'),full_page=True)
                                self.assertFalse(overflow,f'{tab} overflow at {width}')
                            page.get_by_role('button',name='Live Studio',exact=True).click()
                            page.screenshot(path=str(artifacts/f'studio-{width}.png'),full_page=True)
                        browser.close()
                finally:
                    server.should_exit=True;thread.join(timeout=10)

if __name__=='__main__':unittest.main()

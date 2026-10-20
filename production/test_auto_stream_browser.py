"""Offline browser acceptance: no Google/VPS credentials and no publishing."""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'apps/short-stream/part01'))
from auto_stream_ui import PAGE
from playwright.sync_api import sync_playwright

class BrowserCorrectionTests(unittest.TestCase):
    def test_five_widths_scenes_and_sheet_form(self):
        with sync_playwright() as p:
            browser=p.chromium.launch()
            try:
                for width in (360,390,768,1280,1920):
                    with self.subTest(width=width):
                        page=browser.new_page(viewport={'width':width,'height':900})
                        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                        config={'name':'Test','active_scene':0,'scenes':[{'name':'Scene 1','sources':[]}]}
                        def handler(route):
                            suffix=route.request.url.split('/engine',1)[-1]
                            if suffix=='/studio':route.fulfill(body=PAGE,content_type='text/html');return
                            if suffix=='/studio/config':
                                if route.request.method=='PUT':config.update(route.request.post_data_json)
                                body=config
                            elif suffix=='/channels':body={'channels':[]}
                            elif suffix=='/jobs':body={'jobs':[]}
                            elif suffix=='/sheets':body={'sources':[]}
                            elif suffix=='/media':body={'media':[]}
                            elif suffix=='/dashboard':body={'authorized_channels':0,'queued':0,'scheduled':0,'completed':0,'failed':0,'active_encoders':0,'live_verified':None,'upcoming':[],'recent':[]}
                            else:body={'ok':True,'channels_connected':0,'encoder_sessions':0,'vault_ready':True}
                            route.fulfill(body=json.dumps(body),content_type='application/json')
                        page.route('**/*',handler)
                        page.goto('https://fixture.invalid/engine/studio')
                        page.get_by_role('button',name='Sheets Sync',exact=True).click()
                        self.assertEqual(page.locator('#sheet-range').count(),0)
                        self.assertTrue(page.locator('#sheet-name').is_visible())
                        self.assertTrue(page.locator('#sheet-id').is_visible())
                        page.get_by_role('button',name='Channels',exact=True).click()
                        page.get_by_role('button',name='Accounts & Tokens',exact=True).click()
                        self.assertTrue(page.locator('#expected-channel').is_visible())
                        page.get_by_role('button',name='Live Studio',exact=True).click()
                        page.locator('#add-scene').click()
                        page.locator('#duplicate-scene').click()
                        self.assertEqual(page.locator('#scene-list button').count(),3)
                        page.locator('#save-studio').click()
                        page.get_by_text('Saved on server',exact=True).wait_for()
                        self.assertEqual(len(config['scenes']),3)
                        overflow=page.evaluate('document.documentElement.scrollWidth>window.innerWidth')
                        self.assertFalse(overflow,f'overflow at {width}')
                        self.assertEqual(errors,[])
                        page.close()
            finally:browser.close()

if __name__=='__main__':unittest.main()

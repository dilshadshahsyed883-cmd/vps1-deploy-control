"""Full app HTML + real CSS through simulated production prefix rewriting."""
from pathlib import Path
import json
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from production.video_template_release import patch_assets,idle

class VideoOverlayTests(unittest.TestCase):
    def test_overlay_changes_only_asset_urls(self):
        original=b'<script>backend=42</script>'+b''.join(b'<link href="/static/'+a+b'?v=old">' for a in [b'vps1_native_site_shell.css',b'vps1_native_site_shell.js',b'vps1_video_studio_geometry.css'])
        updated=patch_assets(original)
        self.assertEqual(updated.replace(b'/&#115;tatic/',b'/static/').replace(b'&layout=video-v2',b''),original)
        self.assertEqual(patch_assets(updated),updated)
        with self.assertRaises(RuntimeError):patch_assets(b'unknown')
    def test_active_or_unknown_jobs_block_restart(self):
        for status in ('queued','running','processing','importing','unknown'):
            with self.subTest(status=status),patch('production.video_template_release.read_json',return_value={'jobs':[{'status':status}]}):
                with self.assertRaises(RuntimeError):idle('short-maker')
        with patch('production.video_template_release.read_json',return_value={'jobs':[],'library':[{'status':'importing'}]}):
            with self.assertRaises(RuntimeError):idle('video-merger')
        with patch('production.video_template_release.read_json',return_value={'jobs':[],'library':[]}):idle('video-merger')

    def test_failed_overlay_restores_previous_pointer_and_preserves_backend(self):
        import production.video_template_release as release
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); base=root/'host'; repo=root/'repo'; sha='a'*40
            previous=base/'releases/short-maker/previous'
            previous.mkdir(parents=True)
            page=Path(release.PAGES['short-maker'])
            original=b'backend=42\n'+b''.join(b'<link href="/static/'+a.encode()+b'?v=old">' for a in release.ASSETS)
            (previous/page).parent.mkdir(parents=True)
            (previous/page).write_bytes(original)
            (previous/'_production_manifest.json').write_text(json.dumps({'app':'short-maker','files':{str(page):'old'}}))
            current=base/'current/short-maker';current.parent.mkdir(parents=True);current.symlink_to(previous)
            source=repo/'apps/short-maker'/page;source.parent.mkdir(parents=True);source.write_bytes(patch_assets(original))
            with patch.object(release,'BASE',base),patch.object(release,'REPO',repo),patch.object(release,'run') as commands,patch.object(release,'idle'),patch.object(release,'http_check'),patch.object(release,'event'),patch.object(release,'verify',side_effect=RuntimeError('controlled HTTPS failure')):
                with self.assertRaisesRegex(RuntimeError,'controlled HTTPS failure'):
                    release.install('short-maker',sha)
            self.assertEqual(current.resolve(),previous)
            self.assertEqual((previous/page).read_bytes(),original)
            self.assertEqual([c.args[0] for c in commands.call_args_list if 'restart' in c.args[0]],
                [['systemctl','restart','vps1-production-short-maker.service']]*2)



def browser_test():
    from jinja2 import Environment,FileSystemLoader,ChainableUndefined
    from playwright.sync_api import sync_playwright
    scene=ROOT/'apps/scene-builder/part01'
    env=Environment(loader=FileSystemLoader(scene/'templates'),undefined=ChainableUndefined)
    scenehtml=env.get_template('queue.html').render(request={'session':{'is_admin':True,'username':'fixture','app_access':'all'}},page='queue',account={'is_admin':True,'username':'fixture'},scene_presets={'default':{'label':'Default'}},default_scene_preset='default',max_upload_mb=5000,queue_mode='normal',serial_reason='Normal queue active',jobs=[])
    maker=(ROOT/'apps/short-maker/part01/app.py').read_text().split("HTML = r'''",1)[1].split("'''",1)[0]
    pages={'/scene-builder/':scenehtml,'/short-maker/':maker,'/video-merger/':(ROOT/'apps/video-merger/part01/templates/index.html').read_text(),'/auto-caption-studio/':(ROOT/'apps/auto-caption-studio/part01/web_backend/acs_studio.html').read_text()}
    # Production Nginx rewrites raw root static URLs on non-native documents.
    for path in ('/short-maker/','/auto-caption-studio/'):
        for quote in ('"',"'",'`'):
            pages[path]=pages[path].replace(quote+'/static/',quote+path+'static/')
    failures=[];writes=[]
    def route(r):
        path=r.request.url.split('http://video.test',1)[-1].split('?')[0]
        if r.request.method!='GET':writes.append((path,r.request.method))
        if path=='/short-maker':path='/short-maker/'
        if path == '/short-maker':return r.fulfill(status=308,headers={'Location':'/short-maker/'},body='')
        if path in pages:return r.fulfill(content_type='text/html',body=pages[path])
        if path.startswith('/static/'):
            file=scene/'static'/path.removeprefix('/static/')
            if file.is_file():return r.fulfill(content_type='text/css' if file.suffix=='.css' else 'application/javascript',body=file.read_text())
        if '/static/' in path:
            failures.append(path);return r.fulfill(status=404,body='Not found')
        return r.fulfill(content_type='application/json',body=json.dumps({'ok':True,'jobs':[],'projects':[],'batches':[],'folders':[],'library':[],'profiles':[],'channels':['General'],'counts':{},'queue_mode':'normal','workers':{},'pipeline_counts':{},'proxy':{'enabled':False,'profiles':[]},'user':{'username':'fixture','is_admin':True},'is_admin':True,'username':'fixture'}))
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for width in (360,390,768,1280,1920):
            page=browser.new_page(viewport={'width':width,'height':960},is_mobile=width<761,has_touch=width<761)
            errors=[]
            page.on('pageerror',lambda error: errors.append(str(error)))
            page.route('**/*',route)
            rails=[]
            for path in pages:
                page.goto('http://video.test'+path)
                rail=page.locator('[data-video-studio-subnav],.vps1-native-video-tabs').first
                rail.wait_for()
                page.locator('#app-sidebar,#vps1-native-site-sidebar').first.wait_for()
                page.evaluate('document.fonts.ready')
                box=rail.bounding_box();rails.append(box)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,path,'overflow')
                outer=page.locator('nav[aria-label="Experiment Studio"]')
                if outer.count():
                    outer_box=outer.bounding_box()
                    assert abs(outer_box['y']-(72 if width<761 else 26))<2,(width,path,outer_box)
                    assert box['y']>=outer_box['y']+outer_box['height'],(width,path,'overlapping navigation')
                else:
                    assert abs(box['y']-(72 if width<761 else 26))<2,(width,path,box)
                assert abs(box['height']-62)<2,(width,path,box)
                main=page.locator('.workspace' if 'caption' in path else 'main')
                if width<761:
                    assert page.locator('#app-sidebar,#vps1-native-site-sidebar').first.bounding_box()['x']<0,(width,path,'sidebar visible')
                else:
                    for button in page.locator('.vps1-video-mobile-menu,.vps1-native-toggle,#mobile-nav-toggle').all():
                        assert not button.is_visible(),(width,path,'desktop mobile menu')
                if path=='/short-maker/' or path=='/video-merger/':
                    field=page.locator('main input:not([type=checkbox]):not([type=radio])').first
                    assert field.bounding_box()['height']>=44
                if path=='/short-maker/':
                    field=page.locator('input[name="base_name"]')
                    if field.count():
                        field.fill('retained-draft.mp4')
                        rail.locator('a[aria-current="page"]').click()
                        assert field.input_value()=='retained-draft.mp4'
                if width==390:
                    qa=ROOT.parent/'qa';qa.mkdir(exist_ok=True)
                    page.screenshot(path=str(qa/('video-'+path.strip('/')+'-390.png')),full_page=True)
                print('PASS',width,path,'actual HTML + production rewrite')
            assert max(b['x'] for b in rails)-min(b['x'] for b in rails)<2,(width,rails)
            assert max(b['width'] for b in rails)-min(b['width'] for b in rails)<2,(width,rails)
            # Actual link navigation, refresh and history through independent documents.
            # The mobile sidebar is an off-canvas drawer; users must open it
            # before selecting Short Studio. This also exercises drawer behavior.
            if width < 761:
                page.locator('.vps1-native-toggle').click()
            page.locator('nav[aria-label="Applications"] a[href="/short-maker"]').click()
            # Native navigation may complete asynchronously; never reload the previous page.
            page.wait_for_url('**/short-maker/', timeout=10000)
            page.reload()
            actual_title=page.locator('title').text_content()
            assert actual_title=='Short Maker',(width,page.url,actual_title)
            page.go_back()
            assert not errors,errors
            page.close()
        assert not failures,failures
        assert not writes,writes
        browser.close()

if __name__=='__main__':
    if '--browser' in sys.argv:browser_test()
    else:unittest.main()

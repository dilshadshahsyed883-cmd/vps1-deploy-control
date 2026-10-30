"""Browser regressions for the approved Kokoro UI audit, with real Voice HTML.
No VPS, user data or external services. Run --browser for visual DOM contracts.
"""
from pathlib import Path
import sys, unittest
ROOT=Path(__file__).resolve().parents[1]
STATIC=ROOT/'apps/scene-builder/part01/static'
class AuditSourceTests(unittest.TestCase):
    def test_ui_module_has_no_api_or_storage_mutations(self):
        s=(STATIC/'vps1_native_site_shell.js').read_text().split('/* KOKORO_UI_AUDIT_12_V1:')[1]
        for forbidden in ['fetch(', 'localStorage', 'sessionStorage', 'XMLHttpRequest', 'innerHTML', 'eval(']:
            self.assertNotIn(forbidden,s)
        self.assertIn('audio.append(proxy)',s)
        self.assertIn('Math.floor(i/8)',s)

def browser_acceptance():
    import json
    from jinja2 import Template
    from playwright.sync_api import sync_playwright,expect
    voice=Template((ROOT/'apps/voice-studio/part01/templates/voice_studio.html').read_text()).render(max_text=1000000,max_batch=500)
    # The direct Song Studio URL proxies this same Voice document but selects
    # Audio Downloader and preserves its canonical URL, even on refresh.
    canonical=voice
    experiment=Template((ROOT/'apps/scene-builder/part01/templates/base.html').read_text()).render(
        request={'session':{'is_admin':True,'app_access':'all'}},page='experiment_studio',username='fixture')
    for old,new,expected in (
        ("(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/'+'tts/api/audio-downloader'",
         "'/voice-studio/tts/api/audio-downloader'",1),
        ("(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/'+'tts/api/audio-downloader/proxy'",
         "'/voice-studio/tts/api/audio-downloader/proxy'",1),
        ("document.documentElement.dataset.vsEngine='audio-downloader';history.replaceState(null,'','#vs-audio');tab('downloader')",
         "document.documentElement.dataset.vsEngine='audio-downloader';history.replaceState(null,'','/audio-downloader/');tab('downloader')",1),
        ("if(location.hash==='#vs-audio')choose(true);",
         "if(location.pathname==='/audio-downloader/' || location.hash==='#vs-audio')choose(true);",1),
        ('<title>Voice Studio · VPS1</title>', '<title>Audio Downloader · Song Studio</title>',1),
        ('vps1_native_site_shell.js?v=20261029-song-studio-v1',
         'vps1_native_site_shell.js?v=20261030-audio-url-v1',1),
    ):
        assert canonical.count(old)==expected,(old,canonical.count(old))
        canonical=canonical.replace(old,new)
    def route(req):
        url=req.request.url; path=url.split('http://audit.test')[-1].split('?')[0]
        if path=='/voice-studio/app':return req.fulfill(content_type='text/html',body=voice)
        if path=='/experiment-studio/':return req.fulfill(content_type='text/html',body=experiment)
        if path=='/song-studio/':return req.fulfill(content_type='text/html',body='<script>location.replace("/audio-downloader/")</script>')
        if path=='/audio-downloader/':return req.fulfill(content_type='text/html',body=canonical)
        if path.startswith('/static/'):
            file=STATIC/path.rsplit('/',1)[-1]
            if file.is_file():return req.fulfill(content_type='text/css' if file.suffix=='.css' else 'application/javascript',body=file.read_text())
        if req.request.method!='GET':raise AssertionError('UI inspection must never submit: '+url)
        data={'authenticated':True,'is_admin':True,'username':'fixture','ready':True,'mode':'cpu','queue':{'completed':2},'jobs':[],'folders':[],'profiles':[], 'voices':[{'id':'bf_alice','name':'Alice','language':'en-gb','locale':'en-GB'},{'id':'bf_emma','name':'Emma','language':'en-gb','locale':'en-GB'}]}
        return req.fulfill(content_type='application/json',body=json.dumps(data))
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for width in [390,768,1363]:
            page=browser.new_page(viewport={'width':width,'height':936});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)));page.route('**/*',route)
            page.goto('http://audit.test/voice-studio/app#vs-kokoro')
            expect(page.get_by_role('tab',name='Kokoro TTS',exact=True)).to_have_attribute('aria-selected','true')
            expect(page.locator('#vps1-kokoro-audit-css')).to_have_count(1)
            # Reproduce the legacy asynchronous startup overwriting the hash.
            page.evaluate("history.replaceState(null,'','#omnivoice');document.querySelector('.page').classList.toggle('active')")
            expect(page).to_have_url('http://audit.test/voice-studio/app#vs-kokoro')
            page.reload()
            expect(page.get_by_role('tab',name='Kokoro TTS',exact=True)).to_have_attribute('aria-selected','true')
            page.get_by_role('tab',name='Kokoro TTS',exact=True).click()
            expect(page.locator('#vskEngineState')).to_have_text('Kokoro ready')
            expect(page.locator('#vskMessage')).not_to_contain_text('not yet verified')
            expect(page.locator('#vskBatchName')).to_have_css('min-height','44px')
            left=page.locator('#vsKokoroWorkspace').bounding_box()['x']
            page.locator('#vsKokoroWorkspace').get_by_role('button',name='Voices',exact=True).click()
            expect(page.locator('#vskVoiceCards [role=listitem]')).to_have_count(2)
            page.get_by_role('searchbox',name='Search Kokoro voices').fill('Emma')
            expect(page.locator('#vskVoiceCards [role=listitem]:visible')).to_have_count(1)
            page.locator('#vsKokoroWorkspace').get_by_role('button',name='System',exact=True).click()
            expect(page.locator('#vskRuntimeDetails dl')).to_be_visible()
            assert page.locator('#vsKokoroWorkspace').bounding_box()['x']==left
            if width < 761:
                page.locator('.vps1-native-toggle').click()
            page.locator('#vps1-native-site-sidebar nav a[href="/song-studio/"]').click()
            expect(page).to_have_url('http://audit.test/audio-downloader/')
            page.get_by_role('button',name='Proxy Settings',exact=True).click()
            expect(page.locator('#vsaProxy')).to_be_visible()
            page.locator('#vsaProxyName').fill('Unsaved local profile')
            # The Voice engine selector is intentionally hidden inside Song Studio.
            expect(page.locator('.vs-engine-selector')).not_to_be_visible()
            if width < 761:
                page.locator('.vps1-native-toggle').click()
            page.locator('#vps1-native-site-sidebar nav a[href="/experiment-studio/"]').click()
            expect(page).to_have_url('http://audit.test/experiment-studio/')
            page.locator('nav[aria-label="Experiment Studio"] a[href^="/voice-studio/app"]').click()
            expect(page.locator('.vs-engine-selector')).to_be_visible()
            expect(page.locator('#vsaProxy')).not_to_be_visible()
            page.locator('[data-vps1-tab="clone"]').click()
            page.get_by_role('button',name='Open OmniVoice production',exact=True).click()
            expect(page).to_have_url('http://audit.test/voice-studio/app#vps1-production')
            expect(page.locator('[data-vps1-tab="production"]')).to_have_class('engine-tab active')
            expect(page.locator('#omniProductionPanel')).to_be_visible()
            expect(page.locator('#page-gpu')).not_to_be_visible()
            if width < 761:
                page.locator('.vps1-native-toggle').click()
            page.locator('#vps1-native-site-sidebar nav a[href="/song-studio/"]').click()
            expect(page).to_have_url('http://audit.test/audio-downloader/')
            page.get_by_role('button',name='Proxy Settings',exact=True).click()
            expect(page.locator('#vsaProxyName')).to_have_value('')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,'overflow')
            assert not errors,errors
            print('PASS actual Voice',width,'geometry, switching, values, status, catalog, no writes')
            page.close()
        fixture='''<html><head><link rel="stylesheet" href="/static/vps1_native_site_shell.css"><script defer src="/static/vps1_native_site_shell.js" data-unify-sidebar="1"></script></head><body class="vps1-native-site vps1-native-acs"><aside id="app-sidebar"><nav></nav><div class="sidebar-account" data-vps1-server-identity="1"><div class="sidebar-account-name">fixture</div></div></aside><style>.field{display:grid!important}</style><main><select id="activity-filter"><option>All activity states</option></select><section class="sm-hero"><h2>Storage command center</h2><p>Live resources</p></section><div class="field"><label>Text Width</label><input type="range" value="71"><span class="small">85%</span></div><div class="field"><label>Preset Default FG Library</label><select id="presetFgSelect"></select></div><div class="field"><label>Preset Default FG Library</label><select id="presetFgLibrary"><option>None</option></select></div><div id="aiJobs">'''+''.join('<div class="sml-ai-job">Job '+str(i)+'</div>' for i in range(20))+'''</div><nav aria-label="Fixture sections"><button class="active">Production</button><button>Queue</button></nav></main></body></html>'''
        page=browser.new_page(viewport={'width':1363,'height':936});page.route('**/*',route)
        page.route('http://audit.test/fixture',lambda req:req.fulfill(content_type='text/html',body=fixture));page.goto('http://audit.test/fixture')
        expect(page.locator('.field span.small')).to_have_text('71%')
        expect(page.locator('#activity-filter')).to_have_accessible_name('All activity states')
        expect(page.locator('#presetFgSelect')).not_to_be_visible()
        expect(page.locator('#presetFgLibrary')).to_have_accessible_name('Preset Default FG Library')
        expect(page.locator('#aiJobs .sml-ai-job:visible')).to_have_count(8)
        page.get_by_role('button',name='Next',exact=True).click();expect(page.locator('#vps1-ai-pager')).to_contain_text('Page 2 of 3')
        expect(page.locator('#aiJobs .sml-ai-job:visible')).to_have_count(8)
        page.get_by_role('button',name='Next',exact=True).click();expect(page.locator('#aiJobs .sml-ai-job:visible')).to_have_count(4)
        expect(page.locator('.sm-hero h2')).to_have_css('color','rgb(16, 24, 40)')
        expect(page.get_by_role('button',name='Production')).to_have_attribute('aria-pressed','true')
        page.get_by_role('button',name='Production').focus();page.keyboard.press('ArrowRight');expect(page.get_by_role('button',name='Queue')).to_be_focused()
        print('PASS Storage contrast, duplicate binding, range display, 3-page queue, labels, keyboard navigation')
        lum_css='\n'.join((ROOT/'apps/local-upload-manager/part01/static'/name).read_text() for name in ['standalone.css','local_upload_manager.css','lum_desktop_theme_v4.css'])
        uploader='<html><head><style>'+lum_css+'</style><link rel="stylesheet" href="/static/vps1_native_site_shell.css"><script defer src="/static/vps1_native_site_shell.js" data-workspace="uploader"></script></head><body class="lum-embedded"><main class="app-main"><div class="lum-shell"><nav class="lum-tabs" aria-label="Uploader workspace"><a class="active" href="#">Dashboard</a></nav><section class="lum-card"><h2>Uploader</h2><p>Readable workflow text</p><label class="lum-field"><span>Project</span><input></label><div class="lum-table-wrap"><table style="min-width:1500px"><tr><td>Wide records</td></tr></table></div></section></div></main></body></html>'
        for width in [390,768,1363]:
            u=browser.new_page(viewport={'width':width,'height':936});u.route('**/*',route)
            u.route('http://audit.test/uploader',lambda req:req.fulfill(content_type='text/html',body=uploader));u.goto('http://audit.test/uploader')
            expect(u.locator('.lum-card p')).to_have_css('font-size','13px')
            expect(u.locator('.lum-tabs>a')).to_have_css('background-color','rgb(99, 85, 223)')
            rail=u.locator('.vps1-native-upload-tabs').bounding_box();tabs=u.locator('.lum-tabs').bounding_box()
            assert abs(rail['x']-tabs['x'])<1,(width,rail,tabs)
            assert u.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,'uploader overflow')
            print('PASS actual Uploader CSS',width,'shared inset, readable text, purple tabs, contained wide records')
            u.close()
        browser.close()
if __name__=='__main__':
    if '--browser' in sys.argv:browser_acceptance()
    else:unittest.main()

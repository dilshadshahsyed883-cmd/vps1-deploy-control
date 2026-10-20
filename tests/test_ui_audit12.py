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
    def route(req):
        url=req.request.url; path=url.split('http://audit.test')[-1].split('?')[0]
        if path=='/voice-studio/app':return req.fulfill(content_type='text/html',body=voice)
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
            page.goto('http://audit.test/voice-studio/app')
            expect(page.locator('#vps1-kokoro-audit-css')).to_have_count(1)
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
            page.get_by_role('tab',name='Audio Downloader',exact=True).click()
            page.get_by_role('button',name='Proxy Settings',exact=True).click()
            expect(page.locator('#vsaProxy')).to_be_visible()
            page.locator('#vsaProxyName').fill('Unsaved local profile')
            page.get_by_role('tab',name='OmniVoice',exact=True).click()
            expect(page.locator('#vsaProxy')).not_to_be_visible()
            page.get_by_role('tab',name='Audio Downloader',exact=True).click()
            page.get_by_role('button',name='Proxy Settings',exact=True).click()
            expect(page.locator('#vsaProxyName')).to_have_value('Unsaved local profile')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,'overflow')
            assert not errors,errors
            print('PASS actual Voice',width,'geometry, switching, values, status, catalog, no writes')
            page.close()
        fixture='''<html><head><link rel="stylesheet" href="/static/vps1_native_site_shell.css"><script defer src="/static/vps1_native_site_shell.js" data-unify-sidebar="1"></script></head><body><aside id="app-sidebar"><nav></nav><div class="sidebar-account" data-vps1-server-identity="1"><div class="sidebar-account-name">fixture</div></div></aside><main><section class="sm-hero"><h2>Storage command center</h2><p>Live resources</p></section><div class="field"><label>Text Width</label><input type="range" value="71"><span class="small">85%</span></div><div class="field"><label>Preset Default FG Library</label><select id="presetFgSelect"></select></div><div class="field"><label>Preset Default FG Library</label><select id="presetFgLibrary"><option>None</option></select></div><div id="aiJobs">'''+''.join('<div class="sml-ai-job">Job '+str(i)+'</div>' for i in range(20))+'''</div><nav aria-label="Fixture sections"><button class="active">Production</button><button>Queue</button></nav></main></body></html>'''
        page=browser.new_page(viewport={'width':1363,'height':936});page.route('**/*',route)
        page.route('http://audit.test/fixture',lambda req:req.fulfill(content_type='text/html',body=fixture));page.goto('http://audit.test/fixture')
        expect(page.locator('.field span.small')).to_have_text('71%')
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
        browser.close()
if __name__=='__main__':
    if '--browser' in sys.argv:browser_acceptance()
    else:unittest.main()

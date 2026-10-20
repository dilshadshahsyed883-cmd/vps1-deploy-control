"""Voice engine layout acceptance using real HTML, shell and provider fixtures.
Run browser coverage with: python tests/test_voice_responsive.py --browser
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from production.voice_template_release import RESPONSIVE_ID, responsive_overlay


class OverlayTests(unittest.TestCase):
    def test_only_styles_change_and_overlay_is_idempotent(self):
        source = (ROOT / 'apps/voice-studio/part01/templates/voice_studio.html').read_bytes()
        original = b'<html><head></head><body><script>workerSettings=1</script></body></html>'
        result = responsive_overlay(original, source)
        self.assertEqual(responsive_overlay(result, source), result)
        self.assertEqual(result[result.index(b'</head>'):], original[original.index(b'</head>'):])
        self.assertIn(RESPONSIVE_ID, result)
        with self.assertRaises(RuntimeError):
            responsive_overlay(original, source + RESPONSIVE_ID)
        with self.assertRaises(RuntimeError):
            responsive_overlay(original.replace(b'</head>', b''), source)


def browser_acceptance():
    import json
    from jinja2 import Template
    from playwright.sync_api import sync_playwright, expect
    html = Template((ROOT / 'apps/voice-studio/part01/templates/voice_studio.html').read_text()).render(max_text=1000000, max_batch=500)
    static = ROOT / 'apps/scene-builder/part01/static'
    jobs = [{'id':'job-1','job_id':'job-1','source_filename':'long-recording-'+'x'*160+'.txt','filename':'long-recording-'+'x'*160+'.txt','status':'completed','delivery_status':'verified','audio_url':'/tts/api/kokoro/audio/job-1', 'files':{}, 'outputs':{},'mp3_url':'/test.mp3','wav_url':'/test.wav','txt_url':'/test.txt','srt_url':'/test.srt'},
            {'id':'job-2','job_id':'job-2','filename':'retry.txt','status':'processing','error':'controlled provider failure'}]
    writes = []
    def route(r):
        path = r.request.url.split('http://voice.test')[-1].split('?')[0]
        if r.request.method != 'GET':
            writes.append((path,r.request.method))
        if path == '/voice-studio/app':
            return r.fulfill(content_type='text/html',body=html)
        if path.startswith('/static/'):
            file = static / path.rsplit('/',1)[-1]
            if file.is_file():
                return r.fulfill(content_type='text/css' if file.suffix=='.css' else 'application/javascript',body=file.read_text())
        data = {'ready':True,'jobs':jobs,'voices':[], 'profiles':[], 'folders':[], 'user':{'username':'fixture','role':'admin'}}
        return r.fulfill(content_type='application/json',body=json.dumps(data))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width in (360,390,768,1280,1920):
            page = browser.new_page(viewport={'width':width,'height':900})
            errors=[]
            page.on('pageerror',lambda e: errors.append(str(e)))
            page.route('**/*',route)
            page.goto('http://voice.test/voice-studio/app')
            for engine,prefix in [('Kokoro TTS','vsk'),('Edge TTS','vse')]:
                page.get_by_role('tab',name=engine,exact=True).click()
                workspace=page.locator('#vsKokoroWorkspace' if prefix=='vsk' else '#vsEdgeWorkspace')
                for section in ('Production','Queue'):
                    workspace.get_by_role('button',name=section,exact=True).click()
                    if section=='Queue':
                        page.locator('#'+prefix+'QueueRows tr').first.wait_for()
                        expect(page.locator('#'+prefix+'QueueRows')).to_contain_text('long-recording')
                    size=page.evaluate('({viewport:innerWidth,scroll:document.documentElement.scrollWidth})')
                    assert size['scroll']<=width, (width,engine,section,size)
                    bounds=workspace.bounding_box()
                    assert bounds['x']>=0 and bounds['x']+bounds['width']<=width+1
                    if section=='Production':
                        field=page.locator('#vskBatchName' if prefix=='vsk' else '#vseBatchName')
                        assert field.bounding_box()['height']>=44
                    if width<761:
                        assert page.locator('#vps1-native-site-sidebar').bounding_box()['x']<0
                        assert page.locator('.vps1-native-toggle').is_visible()
                print('PASS',width,engine,'production + populated queue')
            if width == 390:
                shot = ROOT.parent / 'qa' / 'voice-edge-queue-390.png'
                shot.parent.mkdir(exist_ok=True)
                page.screenshot(path=str(shot), full_page=True)
            assert not errors, errors
            page.close()
        assert not writes, writes
        browser.close()


if __name__=='__main__':
    if '--browser' in sys.argv:
        browser_acceptance()
    else:
        unittest.main()

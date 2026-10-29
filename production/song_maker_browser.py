"""Disposable real-browser/worker acceptance; NOT production login or Drive proof."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from playwright.sync_api import sync_playwright
from PIL import Image

REPO=Path(__file__).resolve().parents[1]
SOURCE=REPO/'apps/song-maker/part01'


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        env={**os.environ,'SONG_MAKER_DATA':str(root/'data'),'RCLONE_CONFIG':str(root/'absent.conf')}
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        processes=[]
        with (root/'runtime.log').open('w+') as log:
            try:
                processes.append(subprocess.Popen([sys.executable,'-m','uvicorn','songmaker.app:app','--host','127.0.0.1','--port',str(port)],cwd=SOURCE,env=env,stdout=log,stderr=log))
                import urllib.request
                for attempt in range(80):
                    try:
                        urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=1)
                        break
                    except OSError:time.sleep(.1)
                else:raise RuntimeError('Disposable backend startup failed')
                for lane in ('render','upload','sync'):
                    processes.append(subprocess.Popen([sys.executable,'-m','songmaker.worker',lane],cwd=SOURCE,env=env,stdout=log,stderr=log))
                Image.new('RGB',(100,200),'#2b75ba').save(root/'image.png')
                subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=1.27',str(root/'Voice name.wav')],check=True)
                with sync_playwright() as p:
                    browser=p.chromium.launch()
                    context=browser.new_context(extra_http_headers={'X-Auth-User':'disposable-browser','X-Is-Admin':'1'})
                    context.route('**/static/**',lambda route:route.fulfill(path=str(REPO/'apps/scene-builder/part01/static'/Path(route.request.url.split('?',1)[0]).name)))
                    context.route('**/api/session/identity',lambda route:route.fulfill(json={'authenticated':True,'username':'disposable-browser','is_admin':True}))
                    page=context.new_page()
                    for width in (360,390,768,1024,1440):
                        page.set_viewport_size({'width':width,'height':950})
                        page.goto(f'http://127.0.0.1:{port}/song-maker/')
                        page.wait_for_selector('#vps1-native-site-sidebar')
                        assert page.locator('#vps1-native-site-sidebar').count()==1
                        assert page.locator('iframe').count()==0
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'page overflow at {width}'
                        for tab in ('pairs','queue','output','input'):
                            page.locator(f'[data-tab="{tab}"]').click()
                            assert page.locator('#'+tab).is_visible()
                        assert page.locator('nav[aria-label="Video Studio"] a[href="/song-maker/"]').count()==1
                    page.locator('input[name="image"]').set_input_files(root/'image.png')
                    page.locator('input[name="audio"]').set_input_files(root/'Voice name.wav')
                    page.locator('#manual select[name="resolution"]').select_option('720p')
                    page.locator('#manual button').click()
                    page.wait_for_function("document.querySelector('#jobs').textContent.includes('Ready to Upload') || document.querySelector('#jobs').textContent.includes('Waiting for Drive')",timeout=60000)
                    page.reload()
                    assert page.locator('#queue').is_visible()
                    page.locator('[data-tab="output"]').click()
                    page.wait_for_selector('video')
                    with page.expect_download() as info:page.get_by_text('Download MP4',exact=True).click()
                    assert info.value.suggested_filename=='Voice name.mp4'
                    page.locator('[data-tab="queue"]').click()
                    page.get_by_text('View Details',exact=True).click()
                    assert page.locator('#details').is_visible()
                    page.locator('#closeDetails').click()
                    browser.close()
                print(json.dumps({'browser':'PASS','viewports':[360,390,768,1024,1440],'real_render_download':'PASS','production_auth':'NOT_TESTED','real_drive':'NOT_TESTED'}))
            finally:
                for proc in processes:proc.terminate()
                for proc in processes:
                    try:proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait()

if __name__=='__main__':main()

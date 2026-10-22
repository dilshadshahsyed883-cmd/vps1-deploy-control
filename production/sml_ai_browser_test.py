"""Actual five-tab app HTML/JS/API responsive tests in an isolated local fixture.

Model correctness is separately tested by sml_ai_verify against actual pixels.
This fixture deliberately disables inference to exercise truthful unavailable UI.
"""
from pathlib import Path
import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'apps/self-media-library/part01'


def fixture():
    sys.path.insert(0,str(APP));import app
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(outer):
        await app.app.router.startup()
        yield
        await app.app.router.shutdown()
    outer=FastAPI(lifespan=lifespan)
    @outer.get('/api/session/identity')
    def identity():return {'authenticated':True,'username':'fixture','is_admin':True}
    outer.mount('/static',StaticFiles(directory=ROOT/'apps/scene-builder/part01/static'))
    outer.mount('/self-media-library',app.app)
    return outer


def browser_test():
    from PIL import Image
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory(prefix='sml-ai-browser-') as tmp:
        root=Path(tmp);sample=root/'fixture.png';Image.new('RGB',(384,256),(35,100,180)).save(sample)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{port}'
        env={**os.environ,'SML_DATA_ROOT':str(root/'catalog'),'SML_AI_WORKER':'0'}
        server=subprocess.Popen([sys.executable,'-m','uvicorn','production.sml_ai_browser_test:fixture','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(url+'/self-media-library/api/health',timeout=1);break
                except OSError:time.sleep(.1)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or None)
                for width in (360,768,1366):
                    page=browser.new_page(viewport={'width':width,'height':960})
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(url+'/self-media-library/')
                    page.locator('#aiModelState').filter(has_text='AI Disabled').wait_for(state='attached')
                    for label in ('Dashboard','Input','Process','Library & Preview','API & Settings'):
                        page.locator('#nav button').filter(has_text=label).click()
                        page.locator('#pageTitle').filter(has_text=label).wait_for()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,label,'overflow',page.evaluate("Array.from(document.querySelectorAll('body *')).filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>[e.tagName,e.id,e.className,e.getBoundingClientRect().right]).slice(-10)"))
                        assert page.locator('#vps1-native-site-sidebar').count()==1
                    if width<761:
                        menu=page.get_by_role('button',name='Open navigation menu',exact=True)
                        menu.click();assert page.locator('.vps1-native-toggle').get_attribute('aria-expanded')=='true'
                        page.keyboard.press('Escape');assert page.locator('.vps1-native-toggle').get_attribute('aria-expanded')=='false'
                    else:assert round(page.locator('#vps1-native-site-sidebar').bounding_box()['width'])==214
                    page.locator('#nav button').filter(has_text='Input').click()
                    # Exercise the actual approved Input controls before uploading.
                    page.locator('[data-sml-import="youtube"]').click()
                    assert page.locator('#importSource').input_value()=='youtube'
                    assert 'YouTube' in page.locator('#smlImportLabel').inner_text()
                    assert page.locator('[data-sml-import="drive"]').count()==0
                    page.locator('#smlBatchDetails summary').click()
                    assert page.locator('#aiBatchUrls').is_visible()
                    page.locator('#smlBatchDetails summary').click()
                    page.locator('[data-sml-upload="folder"]').click()
                    page.locator('#sml-upload-folder').wait_for(state='visible',timeout=5000)
                    page.locator('[data-sml-upload="video"]').click()
                    assert page.locator('#dropPanel').is_visible()
                    page.locator('[data-sml-upload="image"]').click()
                    page.locator('#sml-upload-image').wait_for(state='visible',timeout=5000)
                    page.locator('#imageFiles').set_input_files(sample)
                    page.locator('#imageUpload').click() if page.locator('#imageUpload').count() else page.get_by_role('button',name='Add selected images',exact=True).click()
                    page.get_by_text('imported',exact=False).first.wait_for()
                    page.locator('#nav button').filter(has_text='Library & Preview').click()
                    page.get_by_role('button',name='Search media',exact=True).click()
                    page.locator('#aiResults [data-inspect]').first.wait_for()
                    page.locator('#aiResults [data-inspect]').first.click()
                    page.locator('#aiInspector').wait_for(state='visible')
                    page.locator('#aiDescription').fill('Human correction survives')
                    page.get_by_role('button',name='Save corrections',exact=True).click()
                    page.locator('#aiDescription').filter(visible=True).wait_for()
                    page.get_by_role('button',name='Close AI inspector',exact=True).click()
                    page.locator('#aiQuery').fill('a mountain landscape')
                    # Query text requires the local semantic model; fixture deliberately disables it.
                    with page.expect_response(lambda response: '/api/v1/ai/search?' in response.url) as search_response:
                        page.get_by_role('button',name='Search media',exact=True).click()
                    assert search_response.value.status==503,search_response.value.status
                    if width==1366:
                        video=root/'comparison.mp4'
                        subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=96x64:rate=10:duration=10','-c:v','libx264','-threads','1',str(video)],check=True)
                        page.request.patch(url+'/self-media-library/api/settings',data={'values':{'quality_filter':'false','ai_auto':'false'}})
                        response=page.request.post(url+'/self-media-library/api/sources/upload',multipart={'file':{'name':'comparison.mp4','mimeType':'video/mp4','buffer':video.read_bytes()}})
                        source=response.json()['id']
                        page.request.post(url+'/self-media-library/api/sources/'+source+'/process')
                        page.locator('#nav button').filter(has_text='Process').click()
                        page.locator('[data-review="'+source+'"]').wait_for(timeout=60000)
                        # Analysis action remains accessible; long queue stays collapsed below review.
                        assert page.locator('#aiQueueDisclosure').count()==1
                        assert not page.locator('#aiJobs').is_visible()
                        assert page.evaluate("document.querySelector('#pairReviews').compareDocumentPosition(document.querySelector('#aiQueueDisclosure')) & Node.DOCUMENT_POSITION_FOLLOWING")
                        page.locator('#aiQueueDisclosure summary').click()
                        assert page.locator('#aiJobs').is_visible()
                        page.locator('#aiQueueDisclosure summary').click()
                        assert not page.locator('#aiJobs').is_visible()
                        page.locator('[data-review="'+source+'"]').click()
                        page.locator('.local-pair video').first.wait_for()
                        assert page.locator('.local-pair video').count()==3
                        assert 'Batch 1 of 1' in page.locator('.local-batch-toolbar').inner_text()
                        page.locator('[data-bulk-all]').check()
                        page.locator('[data-bulk-keep="left"]').click()
                        assert page.locator('.local-pair-choice button[aria-pressed="true"]').count()==2
                        page.get_by_role('button',name='Save decisions & finish',exact=True).click()
                        page.get_by_text('No pending pair reviews.',exact=True).wait_for()
                        page.locator('#nav button').filter(has_text='API & Settings').click()
                        page.locator('#localStorage').filter(has_text='Available').wait_for()
                        assert page.locator('#testDrive').count()==0
                        assert page.locator('#beginReplacement').count()==0
                        assert page.get_by_role('heading',name='Clear & Replace Library',exact=True).count()==0
                    assert not errors,errors
                    output=ROOT.parent/'qa';output.mkdir(exist_ok=True)
                    page.screenshot(path=str(output/f'sml-ai-{width}.png'))
                    print('PASS_SML_AI_UI',width,'five tabs, upload, metadata, AI unavailable, sidebar, overflow',flush=True)
                    page.close()
                browser.close()
        finally:
            server.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):server.wait(timeout=5)
            if server.poll() is None:server.kill();server.wait()

if __name__=='__main__':browser_test()

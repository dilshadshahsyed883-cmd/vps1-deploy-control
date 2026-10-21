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
                browser=p.chromium.launch()
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
                    page.locator('[data-sml-import="drive"]').click()
                    assert page.locator('#importSource').input_value()=='drive'
                    page.locator('#smlBatchDetails summary').click()
                    assert page.locator('#aiBatchUrls').is_visible()
                    page.locator('#smlBatchDetails summary').click()
                    page.locator('[data-sml-upload="folder"]').click()
                    assert page.locator('#sml-upload-folder').is_visible()
                    page.locator('[data-sml-upload="video"]').click()
                    assert page.locator('#dropPanel').is_visible()
                    page.locator('[data-sml-upload="image"]').click()
                    assert page.locator('#sml-upload-image').is_visible()
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
                    page.get_by_role('button',name='Search media',exact=True).click()
                    page.locator('#aiSearchStatus').filter(has_text='AI Disabled').wait_for()
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

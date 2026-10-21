"""Render actual Scene routes in temporary state; verify idle load and a persisted settings edit."""
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch
import json
import os
import secrets
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]


def main():
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix='reaudit-scene-browser-') as tmp:
        state=Path(tmp)
        for key,name in {'WEB_DATA_DIR':'web','SCENE_PERMANENT_ROOT':'permanent','SCRIPT_WORK_DATA_DIR':'script','MASTER_CONTROLLER_DB_PATH':'master.db','ATOMIC_FLOW_DB_PATH':'atomic.db','AUTOFLOW_FLOW_RESULT_ROOT':'flow','SCENE_CHANNELS_FILE':'channels.json'}.items():os.environ[key]=str(state/name)
        os.environ.update(FRESH_WEBSITE_INSTALL='1',FRESH_AUTO_SCRIPT_READY='0',ATOMIC_FLOW_ENABLED='0',WEB_USERNAME='fixture',WEB_ACCESS_KEY=secrets.token_urlsafe(32),SESSION_SECRET=secrets.token_urlsafe(32),WORKER_URL='http://127.0.0.1:1')
        sys.path.insert(0,str(ROOT/'apps/scene-builder/part01'))
        import app
        presets={'ok':True,'version':'fixture-internal','presets':{'documentary':{'name':'Documentary Voice','defaults':{'target_lufs':-16,'duck_db':11,'true_peak':-1.5}}}}
        with patch.object(app,'get_health',return_value={'ok':True,'drive_upload_enabled':True}),patch.object(app,'worker_request',return_value=SimpleNamespace(json=lambda:presets)),TestClient(app.app,base_url='https://localhost') as client:
            login=client.post('/login',data={'username':'fixture','secret_key':os.environ['WEB_ACCESS_KEY']});assert login.status_code==200
            with sync_playwright() as p:
                browser=p.chromium.launch()
                for width in (1363,390):
                    page=browser.new_page(viewport={'width':width,'height':936});errors=[];writes=[];failures=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('requestfailed',lambda req:failures.append((req.url,req.failure)))
                    page.add_init_script("localStorage.setItem('sceneBuilderAudioEngineTabV2','studio')")
                    def route(handle):
                        request=handle.request;parts=urlsplit(request.url);path=parts.path+('?' + parts.query if parts.query else '')
                        # Production font packs are provisioned outside Git. A system font is enough for this form/route fixture.
                        if parts.path.startswith('/static/fonts/'):
                            font=next(Path('/usr/share/fonts').rglob('*.ttf'))
                            return handle.fulfill(content_type='font/ttf',body=font.read_bytes())
                        if request.method!='GET':writes.append((request.method,parts.path))
                        response=client.request(request.method,path,content=request.post_data_buffer,headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','x-settings-csrf')},follow_redirects=True)
                        if response.status_code>=400:failures.append((path,response.status_code,response.text[:160]))
                        headers={k:v for k,v in response.headers.items() if k.lower() not in ('content-length','content-encoding','transfer-encoding','set-cookie')}
                        if 'location' in headers:
                            target=urlsplit(headers['location']);headers['location']=target.path+('?' + target.query if target.query else '')
                        handle.fulfill(status=response.status_code,headers=headers,body=response.content)
                    page.route('**/*',route);page.goto('https://audit.test/settings');page.wait_for_timeout(1200)
                    assert not writes,('idle load wrote settings',writes)
                    expect(page.locator('#preset-save-state')).to_have_text('✓ Saved')
                    expect(page.locator('#aev2-health')).to_contain_text('Internal engine available')
                    assert not errors,errors
                    page.locator('.sb-subtabs button[data-tab="timing"]').click()
                    field=page.locator('input[name="intro_media_min_seconds"]')
                    value='1.75' if width==1363 else '2'
                    field.fill(value);field.press('Tab')
                    expect(page.locator('#preset-save-state')).to_have_text('✓ Saved',timeout=6000)
                    assert ('POST','/settings') in writes,writes
                    writes.clear();page.reload();page.wait_for_timeout(900)
                    assert float(field.input_value())==float(value);assert not writes,writes
                    assert not failures,failures
                    assert not errors,errors
                    print('PASS Scene actual Settings',width,'no idle writes, Studio restore, authenticated save and reload')
                    page.close()
                browser.close()


if __name__=='__main__':main()

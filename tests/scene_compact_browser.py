"""Real Scene UI: compact geometry, all tabs, isolation and saved-state regression."""
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
        audit={'items':[{'item_id':'scene1','scene_code':'SCENE_001','has_media':False,'narration':'Synthetic fixture narration','original_query':'mountains','final_start':0,'final_end':8,'revision':1}], 'sml_settings':{'sml_enabled':False},'scene_count':1,'missing_count':1}
        def worker(method,path,*args,**kwargs):
            data=audit if path.endswith('/audit/media') else {'id':'compact-fixture','project_name':'Compact UI fixture','status':'waiting_for_audit'} if path=='/jobs/compact-fixture' else presets
            return SimpleNamespace(json=lambda:data)
        with patch.object(app,'get_health' ,return_value={'ok':True,'drive_upload_enabled':True}),patch.object(app,'worker_request',side_effect=worker),TestClient(app.app,base_url='https://localhost') as client:
            login=client.post('/login',data={'username':'fixture','secret_key':os.environ['WEB_ACCESS_KEY']});assert login.status_code==200
            app.record_job_owner('compact-fixture','fixture','Compact UI fixture',{'sml_enabled':False})
            with sync_playwright() as p:
                browser=p.chromium.launch()
                for width in (1440,1024,768,390,360):
                    page=browser.new_page(viewport={'width':width,'height':936});errors=[];writes=[];failures=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('requestfailed',lambda req:failures.append((req.url,req.failure)))
                    page.add_init_script("localStorage.setItem('sceneBuilderAudioEngineTabV2','studio')")
                    def route(handle):
                        request=handle.request;parts=urlsplit(request.url);path=parts.path+('?' + parts.query if parts.query else '')
                        # Production font packs are provisioned outside Git. A system font is enough for this form/route fixture.
                        if parts.path.startswith('/static/fonts/'):
                            font=next(Path('/usr/share/fonts').rglob('DejaVuSans.ttf'))
                            return handle.fulfill(content_type='font/ttf',body=font.read_bytes())
                        if request.method!='GET':writes.append((request.method,parts.path))
                        response=client.request(request.method,path,content=request.post_data_buffer,headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','x-settings-csrf')},follow_redirects=True)
                        if response.status_code>=400:failures.append((path,response.status_code,response.text[:160]))
                        headers={k:v for k,v in response.headers.items() if k.lower() not in ('content-length','content-encoding','transfer-encoding','set-cookie')}
                        if 'location' in headers:
                            target=urlsplit(headers['location']);headers['location']=target.path+('?' + target.query if target.query else '')
                        handle.fulfill(status=response.status_code,headers=headers,body=response.content)
                    page.route('**/*',route);page.goto('https://audit.test/settings');page.wait_for_timeout(400)
                    assert not writes,('idle load wrote settings',writes)
                    expect(page.locator('#preset-save-state')).to_have_text('✓ Saved')
                    expect(page.locator('#aev2-health')).to_contain_text('Internal engine available')
                    assert not errors,errors
                    assert page.locator('body').get_attribute('data-scene-ui')=='sml-compact'
                    def geometry(label):
                        measure=page.evaluate("""() => ({width:innerWidth,scroll:document.documentElement.scrollWidth})""")
                        assert measure['scroll']<=measure['width']+1,(width,label,measure)
                    for tab in ('presets','timing','media','creative','captions','audio','output'):
                        page.locator(f'.sb-subtabs button[data-tab="{tab}"]').click()
                        expect(page.locator(f'.sb-tab-panel[data-panel="{tab}"]')).to_be_visible()
                        geometry(tab)
                        assert page.locator('.sb-tab-panel.active').count()==1
                        if tab=='timing':
                            row=page.locator('.sb-control-grid > label:visible').first
                            compact_height=row.bounding_box()['height']
                            page.locator('link[href*="scene_compact_ui.css"]').evaluate('(e)=>e.disabled=true')
                            legacy_height=row.bounding_box()['height']
                            page.locator('link[href*="scene_compact_ui.css"]').evaluate('(e)=>e.disabled=false')
                            assert compact_height<legacy_height,(width,compact_height,legacy_height)
                            page.wait_for_timeout(150)
                            if os.environ.get('SCENE_COMPACT_SCREENSHOTS'):
                                folder=Path(os.environ['SCENE_COMPACT_SCREENSHOTS']);folder.mkdir(parents=True,exist_ok=True)
                                page.screenshot(path=str(folder/f'timing-{width}.png'),full_page=True)
                        if tab=='audio':
                            for sub in ('processing','ducking','library','studio'):
                                page.locator(f'[data-aev2-tab="{sub}"]').click()
                                expect(page.locator(f'[data-aev2-panel="{sub}"]')).to_be_visible()
                                geometry('audio '+sub)
                        if tab=='captions':
                            expect(page.locator('#caption-size')).to_be_visible()
                            assert page.locator('#caption-size').input_value()
                    assert not writes,('tab navigation wrote settings',writes)
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
                    for route_path in ('/queue','/audit-media'):
                        page.goto('https://audit.test'+route_path)
                        geometry(route_path)
                        assert page.locator('body').get_attribute('data-scene-ui')=='sml-compact'
                    page.goto('https://audit.test/audit-media?job=compact-fixture')
                    expect(page.locator('.audit-card')).to_have_count(1)
                    geometry('selected audit')
                    page.locator('[data-filter="changed"]').click()
                    expect(page.locator('#audit-empty')).to_be_visible()
                    page.locator('[data-filter="attention"]').click()
                    expect(page.locator('.audit-card')).to_have_count(1)
                    page.locator('[data-action="keyword"]').click()
                    expect(page.locator('#keyword-modal')).to_be_visible()
                    page.locator('#keyword-cancel').click()
                    expect(page.locator('#keyword-modal')).to_be_hidden()
                    expect(page.locator('#audit-continue')).to_be_disabled()
                    # Shared base also serves unrelated apps; they must never opt in.
                    page.goto('https://audit.test/settings?acs_audio_embed=1')
                    assert page.locator('body').get_attribute('data-scene-ui') is None
                    assert page.locator('link[href*="scene_compact_ui.css"]').count()==0
                    assert not errors,errors
                    assert not writes,('read-only navigation wrote settings',writes)
                    print('PASS Scene compact UI',width,'7 settings + 4 audio tabs, Queue/Audit, density, no overflow, save/reload, ACS isolation')
                    page.close()
                browser.close()


if __name__=='__main__':main()

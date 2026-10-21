"""Chromium canonical Dashboard/Storage lifecycle regression; sanitized counts only."""
from pathlib import Path
import json
import os
import shutil
ROOT=Path(__file__).resolve().parents[1]
SCENE=ROOT/'apps/scene-builder/part01'
WIDTHS=(360,390,768,1024,1440)

def fixture_html(admin=True):
    from jinja2 import Environment, FileSystemLoader
    from types import SimpleNamespace
    env=Environment(loader=FileSystemLoader(SCENE/'templates'),autoescape=True)
    manifest=json.loads((SCENE/'static/shell-v2/manifest.json').read_text())
    return env.get_template('persistent_workspace.html').render(
        request=SimpleNamespace(session={'is_admin':admin},query_params={}),
        page='shell_workspace',username='fixture',storage_csrf='',drive_connected=False,shell_entry=manifest['entry'])

def run(production=False):
    from playwright.sync_api import sync_playwright
    origin='https://app.scenebuilder.online'
    cookies=[]
    if production:
        from production.route_uploader_scene_https import scene_cookie
        from http.cookies import SimpleCookie
        parsed=SimpleCookie();parsed.load(scene_cookie())
        cookies=[{'name':k,'value':v.value,'url':origin,'secure':True} for k,v in parsed.items()]
        if not cookies:raise RuntimeError('authenticated cookie unavailable')
    with sync_playwright() as pw:
        launch={'headless':True}
        executable=os.environ.get('VPS1_CHROMIUM_EXECUTABLE')
        if production:executable=shutil.which('google-chrome-stable') or shutil.which('chromium')
        if executable:launch['executable_path']=executable
        if os.geteuid()==0:launch['args']=['--no-sandbox']
        browser=pw.chromium.launch(**launch)
        try:
            for width in (*WIDTHS,720):
                context=browser.new_context(viewport={'width':width,'height':1000},device_scale_factor=2 if width==720 else 1)
                if production:context.add_cookies(cookies)
                context.add_init_script('''window.__shellTimers=new Set();
const start=window.setInterval.bind(window),stop=window.clearInterval.bind(window);
window.setInterval=(...args)=>{const id=start(...args);window.__shellTimers.add(id);return id;};
window.clearInterval=id=>{window.__shellTimers.delete(id);return stop(id);};''')
                writes=[];failures=[];role={'admin':True,'authenticated':True};api_fail={'value':False}
                page=context.new_page()
                page.on('pageerror',lambda e:failures.append(str(e)))
                page.on('request',lambda req:writes.append(req.method) if req.method not in ('GET','HEAD') else None)
                if not production:
                    def fulfill(route):
                        path=route.request.url.split(origin,1)[-1].split('?',1)[0]
                        if path in ('/','/admin/storage') or path.startswith('/workspace/'):
                            route.fulfill(status=200,content_type='text/html',body=fixture_html(role['admin']))
                        elif path.startswith('/static/'):
                            file=SCENE/path.lstrip('/')
                            if file.is_file():route.fulfill(status=200,content_type='application/javascript' if file.suffix=='.js' else 'text/css',body=file.read_bytes())
                            else:route.fulfill(status=404,body='')
                        elif path=='/api/session/identity':
                            route.fulfill(json={'authenticated':role['authenticated'],'is_admin':role['admin'],'username':'fixture'})
                        elif path=='/api/dashboard/v1':
                            route.fulfill(status=503 if api_fail['value'] else 200,json={'ok':True,'vps':{'cpu_percent':7},'apps':[],'services':[]})
                        elif path=='/api/admin/storage/metrics':route.fulfill(json={'ok':True,'cpu':{'percent':7,'cores':2},'memory':{'percent':10},'disk':{'percent':20}})
                        elif path=='/api/admin/storage/snapshot':route.fulfill(json={'ok':True,'read_only':True,'partial':True,'deployment_cleanup':{'status':'unknown'}})
                        else:route.fulfill(status=200,content_type='application/json',body='{}')
                    context.route('**/*',fulfill)
                try:
                    response=page.goto(origin+'/',wait_until='networkidle',timeout=45000)
                    assert response and response.status==200,'canonical host route unavailable'
                    page.locator('#control-dashboard').wait_for()
                    page.evaluate("window.__shellNode=document.getElementById('app-sidebar')")
                    assert page.locator('#app-sidebar, #vps1-native-site-sidebar').count()==1,'duplicate sidebar'
                    assert page.evaluate('window.__shellTimers.size')==1,'dashboard timer count'
                    page.locator('#dashboard-refresh-rate').select_option('30')
                    page.locator('#dashboard-auto-refresh').uncheck()
                    assert page.evaluate('window.__shellTimers.size')==0,'disabled polling retained'
                    page.locator('nav[aria-label="Persistent workspace"] a[href="/admin/storage"]').click()
                    page.locator('#cpu-value').wait_for()
                    page.go_back(wait_until='domcontentloaded');page.locator('#control-dashboard').wait_for()
                    assert not page.locator('#dashboard-auto-refresh').is_checked(),'dashboard preferences lost'
                    assert page.locator('#dashboard-refresh-rate').input_value()=='30'
                    assert page.evaluate('window.__shellTimers.size')==0,'polling preference ignored'
                    page.locator('#dashboard-auto-refresh').check()
                    for _ in range(2):
                        page.locator('nav[aria-label="Persistent workspace"] a[href="/admin/storage"]').click()
                        page.locator('#cpu-value').wait_for()
                        assert page.evaluate("window.__shellNode===document.getElementById('app-sidebar')"),'host replaced'
                        assert page.evaluate('performance.getEntriesByType("navigation").length')==1,'document reloaded'
                        assert page.locator('[data-app-root="storage-manager"]').count()==1
                        assert page.evaluate('window.__shellTimers.size')==2,'old dashboard timer leaked'
                        page.locator('#refresh-cleanup-status').click()
                        page.wait_for_function("!document.querySelector('[data-app-root]').shadowRoot.querySelector('#refresh-cleanup-status').disabled")
                        page.go_back(wait_until='domcontentloaded');page.locator('#control-dashboard').wait_for()
                        assert page.evaluate('window.__shellTimers.size')==1,'storage timer leaked'
                        assert page.locator('[data-app-root="storage-manager"]').count()==0,'adapter did not unmount'
                    page.go_forward(wait_until='domcontentloaded');page.locator('#cpu-value').wait_for()
                    page.reload(wait_until='networkidle');page.locator('#cpu-value').wait_for()
                    assert not page.evaluate('document.documentElement.scrollWidth>document.documentElement.clientWidth+2'),'storage overflow'
                    page.locator('nav[aria-label="Persistent workspace"] a[href="/"]').click()
                    page.locator('#control-dashboard').wait_for()
                    assert not page.evaluate('document.documentElement.scrollWidth>document.documentElement.clientWidth+2'),'dashboard overflow'
                    if width>760:assert abs(page.locator('#app-sidebar').bounding_box()['width']-214)<2,'geometry'
                    assert not writes,'unexpected writes'
                    assert not failures,'JavaScript errors'
                    page.keyboard.press('Tab');assert page.evaluate('document.activeElement!==document.body'),'keyboard focus'
                    # Canonical alias and hard refresh both mount the same adapter.
                    page.goto(origin+'/workspace/storage/',wait_until='networkidle');page.locator('#cpu-value').wait_for()
                    if not production:
                        role['admin']=False
                        page.locator('nav[aria-label="Persistent workspace"] a[href="/"]').click()
                        page.locator('#control-dashboard').wait_for()
                        page.locator('nav[aria-label="Persistent workspace"] a[href="/admin/storage"]').click()
                        page.locator('#vps1-shell-outlet [role="alert"]').wait_for()
                        assert page.locator('[data-app-root]').count()==0,'revoked admin view retained'
                        page.goto(origin+'/',wait_until='networkidle');page.locator('#control-dashboard').wait_for()
                        assert page.locator('#vps1-storage-seed').count()==0,'member received admin seed'
                        assert page.locator('nav[aria-label="Persistent workspace"] a[href="/admin/storage"]').count()==0
                        api_fail['value']=True;page.locator('#dashboard-refresh-now').click()
                        page.wait_for_function("document.querySelector('[data-app-root]').shadowRoot.querySelector('#dashboard-updated').textContent.startsWith('Refresh failed')")
                        api_fail['value']=False;page.locator('#dashboard-refresh-now').click()
                        page.wait_for_function("document.querySelector('[data-app-root]').shadowRoot.querySelector('#dashboard-updated').textContent.startsWith('Updated')")
                        role['authenticated']=False;page.locator('nav[aria-label="Persistent workspace"] a[href="/workspace/"]').click()
                        page.locator('#vps1-shell-outlet [role="alert"]').wait_for()
                finally:context.close()
        finally:browser.close()
    print('SHELL_V2_BROWSER=PASS adapters=2 canonical_routes=PASS timer_cleanup=PASS viewports=5 zoom_layout_equivalent=200% host_persistence=PASS back_refresh=PASS writes=0 auth='+('PRODUCTION' if production else 'FIXTURE'))
if __name__=='__main__':
    import sys
    run('--production' in sys.argv)

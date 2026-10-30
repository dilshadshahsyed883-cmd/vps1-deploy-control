"""Disposable rendered Copyright UI checks; browser requests cannot reach production."""
from pathlib import Path
from urllib.parse import urlsplit
from jinja2 import Environment, FileSystemLoader, ChainableUndefined

ROOT = Path(__file__).resolve().parents[1]
PART = ROOT / 'apps/copyright-uploader/part01'

def fixture(tab, paused=False, projects=None):
    env = Environment(loader=FileSystemLoader(PART/'templates'), undefined=ChainableUndefined, autoescape=True)
    import importlib.util
    spec=importlib.util.spec_from_file_location('copyright_ui',PART/'lum_engine/copyright_ui.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    env.filters['schedule_pkt']=module.schedule_pkt
    projects = projects if projects is not None else [dict(id=1,title='Ready video',channel_db_id=7,status='QUEUED',progress=0,app_upload_at='2026-11-01 18:00',source_file_name='merge.mp4'),dict(id=2,title='Failed video',channel_db_id=7,status='FAILED',progress=35,error_message='<unsafe> Network issue'),dict(id=3,title='Review video',channel_db_id=7,status='REVIEW_REQUIRED',progress=45),dict(id=4,title='Uploaded video',channel_db_id=7,status='COMPLETED',progress=100,youtube_url='https://www.youtube.com/watch?v=fixture')]
    return env.get_template('app.html').render(engine_instance='copyright',mode='heavy',base_path='/copyright-uploader',embedded_view=True,tab=tab,user=dict(username='fixture',is_admin=True),csrf='fixture-csrf',health=dict(worker_alive=True,active_project_id=None),stats=dict(engine_enabled=not paused,counts=dict(TOTAL=4,WAITING=1,COMPLETED=1,FAILED=1),channels=1,settings=dict(pause_after_current='0')),copyright_files=['merge.mp4'],copyright_profiles=[dict(id=3,profile_name='Main profile',group_name='Group A',metadata_source='manual')],channels=[dict(id=7,channel_name='My Channel',channel_id='UCfixture',token_status='OK')],projects=projects)

def main():
    from playwright.sync_api import sync_playwright
    writes=[]
    with sync_playwright() as p:
        # Use the installed runtime browser when running in Work Mode.
        cached=list((Path.home()/'.cache/ms-playwright').glob('chromium_headless_shell-*/chrome-linux/headless_shell'))
        browser=p.chromium.launch(**({'executable_path':str(cached[-1])} if cached else {}))
        for width in (1440,1024,768,390,360):
            page=browser.new_page(viewport=dict(width=width,height=1050))
            errors=[]
            page.on('pageerror',lambda e: errors.append(str(e)))
            def route(handle):
                req=handle.request; path=urlsplit(req.url).path
                if req.method!='GET': writes.append(path)
                if path=='/copyright-uploader/app':
                    from urllib.parse import parse_qs
                    tab=parse_qs(urlsplit(req.url).query).get('tab',['add_video'])[0]
                    return handle.fulfill(content_type='text/html',body=fixture(tab))
                if path.startswith('/copyright-uploader/static/'):
                    file=PART/'static'/path.rsplit('/',1)[1]
                    if file.is_file(): return handle.fulfill(content_type='text/css' if file.suffix=='.css' else 'application/javascript',body=file.read_text())
                if req.resource_type in ('script','stylesheet'):
                    return handle.fulfill(content_type='application/javascript' if req.resource_type=='script' else 'text/css',body='')
                return handle.fulfill(content_type='application/json',body='{"files":["merge.mp4"]}')
            page.route('**/*',route)
            for tab in ('dashboard','add_video','profiles','queue'):
                page.goto('https://fixture.test/copyright-uploader/app?tab='+tab)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,tab,'overflow')
                assert not page.get_by_role('link',name='Auto Engine',exact=True).count()
                assert page.locator('.lum-tabs [aria-current=page]').count()==1
                if tab=='add_video':
                    assert page.locator('[name=privacy_status]').input_value()=='public'
                    page.locator('#copyright-source-mode').select_option('folder')
                    assert page.locator('#copyright-folder-file').is_visible()
                    assert page.locator('#copyright-folder-file').is_enabled()
                    assert page.locator('[name=drive_url]').is_disabled()
                    page.locator('#copyright-source-mode').select_option('pc_upload')
                    assert page.locator('[name=video_file]').is_visible()
                    assert page.locator('#copyright-folder-file').is_disabled()
                    cols=page.locator('.copyright-upload').evaluate("e=>getComputedStyle(e).gridTemplateColumns.split(' ').length")
                    assert cols==(2 if width>760 else 1),(width,cols)
                elif tab=='queue':
                    page.locator('[data-copyright-status]').select_option('attention')
                    assert page.locator('[data-copyright-job]:visible').count()==2
                    page.locator('[data-copyright-search]').fill('failed')
                    assert page.locator('[data-copyright-job]:visible').count()==1
                    page.locator('[data-copyright-search]').fill('no match here')
                    assert page.locator('[data-copyright-no-match]').is_visible()
                    page.locator('[data-copyright-status]').select_option('all')
                    page.locator('[data-copyright-search]').fill('my channel')
                    assert page.locator('[data-copyright-job]:visible').count()==4
                    assert not page.get_by_role('button',name='Stop Current').count()
            page.goto('https://fixture.test/copyright-uploader/app?tab=add_video&profile_id=3')
            assert page.locator('#copyright-upload-profile').input_value()=='3'
            assert not errors,errors
            page.close()
            print(f'COPYRIGHT_BROWSER=PASS WIDTH={width} TABS=4 FILTERS=PASS SOURCE_MODES=PASS PROFILE=PASS')
        assert not writes,writes
        browser.close()

if __name__=='__main__': main()

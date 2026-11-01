"""Rendered Short Uploader profile, runtime badges and schedule slot checks."""
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
PART=ROOT/'apps/local-upload-manager/part01'

def main():
    source=(PART/'templates/app.html').read_text()
    a=source.index('    <form id="lum-ae-profile-form"');b=source.index('</form>',a)+7
    form=source[a:b].replace('data-ae-panel="profiles" hidden','data-ae-panel="profiles"')
    start=source.index("    {% if engine_instance in ['shortstudio','song'] %}<div class=\"lum-short-engine-tabbar\">")
    nav=source[start:source.index('    <section class="lum-card lum-ae-profile-manager"',start)]
    manager=source[source.index('    <section class="lum-card lum-ae-profile-manager"'):source.index('    <section class="lum-card lum-ae-automation-controls"')]
    body='<div class="lum-shell" data-engine-instance="shortstudio">'+nav+manager+form+'</div>'
    template='{% extends "base.html" %}{% block content %}'+body+'{% endblock %}'
    ae=SimpleNamespace(groups=[dict(id=7,group_name='Test',channel_count=2)],short_folders=['Test'],profiles=[],scheduler_ui=SimpleNamespace(worker_status='Idle',scheduler_status='Waiting'),engine=SimpleNamespace(running=True,scheduler_armed=True))
    env=Environment(loader=FileSystemLoader(PART/'templates'))
    values=dict(engine_instance='shortstudio',p=None,src={},csrf='test',
                base_path='/short-uploader',ae=ae,embedded_view=False)
    html=env.from_string(template).render(**values,user={'is_admin': True, 'username': 'fixture'})
    member_html=env.from_string(template).render(**values,user={'is_admin': False, 'username': 'fixture'})
    assert html==member_html,'Short admin/member profile controls must render identically'
    writes=[]
    def route(handle):
        req=handle.request;path=urlsplit(req.url).path
        if req.method!='GET': writes.append(path)
        if path=='/fixture': return handle.fulfill(content_type='text/html',body=html)
        if path.startswith('/short-uploader/static/'):
            file=PART/'static'/path.rsplit('/',1)[1]
            if file.is_file(): return handle.fulfill(content_type='text/css' if file.suffix=='.css' else 'application/javascript',body=file.read_text())
        return handle.fulfill(content_type='application/json',body='{}')
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for width in (1440,1024,768,390,360):
            page=browser.new_page(viewport=dict(width=width,height=1000));page.route('**/*',route)
            page.goto('https://fixture.test/fixture')
            page.locator('[data-ae-panel-target=profiles]').click()
            grid=page.locator('[data-short-profile-grid]')
            cols=grid.evaluate("e=>getComputedStyle(e).gridTemplateColumns.split(' ').length")
            assert cols==(2 if width>760 else 1),(width,cols)
            slots=page.locator('[data-short-profile-schedule] input[type=time]')
            assert slots.count()==1
            assert slots.first.input_value()=='06:00'
            page.locator('[name=daily_upload_count]').fill('4')
            assert slots.count()==4
            slots.nth(3).fill('19:45');slots.nth(3).dispatch_event('change')
            assert '19:45' in page.locator('[data-short-schedule-csv]').input_value()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
            toolbar=page.locator('.lum-short-profile-toolbar')
            assert toolbar.count()==1,(width,'short profile toolbar missing')
            assert page.locator('#lum-ae-profile-select').count()==1,(width,'duplicate profile dropdown')
            assert page.locator('.lum-ae-profile-summary').count()==0,(width,'redundant summary')
            assert page.locator('.lum-ae-profile-browser').count()==0,(width,'redundant profile browser')
            assert toolbar.locator('[data-ae-open-schedules]').count()==0,(width,'removed Profiles manage button still visible')
            assert toolbar.locator('button.lum-ae-schedule-toggle').count()==0,(width,'unsafe global scheduler action exposed')
            assert toolbar.locator('.lum-short-scheduler-state').inner_text()=='Armed'
            if width>=1024:
                elements=[toolbar.locator('#lum-ae-profile-select'),toolbar.get_by_text('+ New Profile'),
                          toolbar.get_by_text('Create Profile'),toolbar.locator('.lum-short-scheduler-state')]
                heights=[item.bounding_box()['y'] for item in elements]
                assert max(heights)-min(heights)<12,(width,'profile actions wrapped',heights)
            if width<=760:
                assert page.locator('.lum-short-engine-tabbar nav').evaluate('e=>e.scrollWidth<=e.clientWidth'),width
                for button in page.locator('.lum-short-engine-tabbar nav button').all():
                    assert button.evaluate('e=>e.scrollWidth<=e.clientWidth'),(width,button.inner_text())
            if width>=1024:
                nav=page.locator('.lum-short-engine-tabbar nav').bounding_box()
                badges=page.locator('.lum-short-engine-status').bounding_box()
                assert abs(nav['y']-badges['y'])<20,(width,nav,badges)
            print(f'SHORT_BROWSER=PASS WIDTH={width} COLUMNS={cols} SLOTS=4 EDITABLE=PASS')
            page.close()
        assert not writes,writes
        browser.close()

if __name__=='__main__': main()

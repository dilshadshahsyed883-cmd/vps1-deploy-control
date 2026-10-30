"""Offline rendered Song Uploader mobile layout and independent badge presentation."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
PART=ROOT/'apps/local-upload-manager/part01'


def main():
    source=(PART/'templates/app.html').read_text()
    begin=source.index('    {% if engine_instance in [\'shortstudio\',\'song\'] %}<div class="lum-short-engine-tabbar">')
    nav=source[begin:source.index('    <section class="lum-card lum-ae-profile-manager"',begin)]
    auto_start=source.index('    <section class="lum-card lum-ae-automation-controls"')
    automation=source[auto_start:source.index('</section>',auto_start)+len('</section>')]
    markup='{% extends "base.html" %}{% block content %}<div class="lum-shell" data-engine-instance="song">'+nav+automation+'</div>{% endblock %}'
    profile=dict(id=1,category_name="music",group_name="Music Group",group_channel_count=4,
                 enabled=1,v6_video_count=7,schedule_times="06:00,12:00",
                 schedule_enabled=1,work_mode="auto",channel_db_id=None,
                 channel_paused=False)
    ae=SimpleNamespace(
        profiles=[profile],
        engine=SimpleNamespace(running=True,scheduler_armed=True),
        scheduler_ui=SimpleNamespace(worker_status="Idle",scheduler_status="Error",
                                    worker_note="",scheduler_note="Scheduler reported an error; check Logs."),
    )
    env=Environment(loader=FileSystemLoader(PART/'templates'))
    html=env.from_string(markup).render(engine_instance='song',ae=ae,p=None,src={},
        csrf='test',base_path='/song-uploader',user=None,suite_auth=False,embedded_view=False)

    def route(handle):
        req=handle.request
        path=urlsplit(req.url).path
        if req.method!='GET':
            raise AssertionError('Browser layout test must never perform writes')
        if path=='/fixture':
            return handle.fulfill(content_type='text/html',body=html)
        if path.startswith('/song-uploader/static/'):
            file=PART/'static'/path.rsplit('/',1)[1]
            if file.is_file():
                typ='text/css' if file.suffix=='.css' else 'application/javascript'
                return handle.fulfill(content_type=typ,body=file.read_text())
        return handle.fulfill(status=404,body='')

    with sync_playwright() as p:
        browser=p.chromium.launch()
        for width in (1440,1024,768,740,390,360):
            page=browser.new_page(viewport=dict(width=width,height=1100))
            page.route('**/*',route)
            page.goto('https://fixture.test/fixture')
            page.wait_for_load_state('domcontentloaded')
            root=page.locator('.lum-shell[data-engine-instance="song"]')
            assert root.count()==1,width
            assert page.get_by_text('Worker Idle').count()==1,width
            assert page.get_by_text('Scheduler Error').count()==1,width
            assert page.get_by_text('Engine RUNNING').count()==1,width
            table=page.locator('.lum-ae-automation-profiles')
            rows=table.locator('tbody > tr')
            assert rows.count()==1,width
            assert rows.first.get_by_text('Music Group').count()==1,width
            if width<=760:
                assert rows.first.evaluate("e=>getComputedStyle(e).display")=='grid',width
                assert table.evaluate("e=>e.getBoundingClientRect().width<=innerWidth"),width
                assert page.locator('[data-mobile-label="Schedule (PKT)"]').is_visible(),width
                assert page.locator('[data-mobile-label="Actions"]').is_visible(),width
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'),width
                action=page.locator('.lum-ae-bulk-profile-controls')
                assert action.evaluate("e=>getComputedStyle(e).flexWrap")=='nowrap',width
            else:
                assert rows.first.evaluate("e=>getComputedStyle(e).display")!='grid',width
            print(f'SONG_MOBILE=PASS WIDTH={width} WORKER=IDLE SCHEDULER=ERROR CARDS={width<=760}')
            page.close()
        browser.close()


if __name__=="__main__":
    main()

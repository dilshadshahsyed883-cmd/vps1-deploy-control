"""Actual app HTML/JS with synthetic API data: all local tabs and UI invariants."""
from pathlib import Path
import json
import os
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
STATIC=ROOT/'apps/scene-builder/part01/static'

def main():
    from playwright.sync_api import sync_playwright,expect
    maker=(ROOT/'apps/short-maker/part01/app.py').read_text().split("HTML = r'''",1)[1].split("'''",1)[0]
    pages={
        '/short-maker/':maker,
        '/video-merger/':(ROOT/'apps/video-merger/part01/templates/index.html').read_text(),
        '/auto-caption-studio/':(ROOT/'apps/auto-caption-studio/part01/web_backend/acs_studio.html').read_text(),
    }
    for path in ('/short-maker/','/auto-caption-studio/'):
        for quote in ('"',"'",'`'):
            pages[path]=pages[path].replace(quote+'/static/',quote+path+'static/')
    writes=[];failures=[]
    def route(handle):
        req=handle.request;path=urlsplit(req.url).path
        if req.method!='GET':writes.append((path,req.method))
        if path in pages:return handle.fulfill(content_type='text/html',body=pages[path])
        if path == '/short-maker/resumable_client.js':
            script=(ROOT/'apps/short-maker/part01/resumable_client.js').read_text()
            return handle.fulfill(content_type='application/javascript',body=script)
        if path.startswith('/static/'):
            file=STATIC/path.removeprefix('/static/')
            if file.is_file():return handle.fulfill(content_type='text/css' if file.suffix=='.css' else 'application/javascript',body=file.read_text())
        if '/static/' in path:
            failures.append(path);return handle.fulfill(status=404,body='Not found')
        # No preset is created and no worker/API mutation is permitted by browsing.
        data={'ok':True,'authenticated':True,'principal':{'is_admin':True,'username':'fixture'},'presets':[],'user_state':{},'fonts':{'families':[]},'jobs':[],'projects':[],'items':[],'batches':[],'folders':[],'library':[],'profiles':[],'channels':['General'],'counts':{},'workers':{},'pipeline_counts':{},'proxy':{'enabled':False,'profiles':[]},'user':{'username':'fixture','is_admin':True},'is_admin':True,'username':'fixture'}
        return handle.fulfill(content_type='application/json',body=json.dumps(data))
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for width in (1440,1024,768,390,360):
            page=browser.new_page(viewport={'width':width,'height':936});errors=[]
            page.on('pageerror',lambda e:errors.append(str(e)));page.route('**/*',route)
            def geometry(label):
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,label,'page overflow')
                assert page.locator('link[href*="vps1_native_site_shell.css"]').count()>=1
                assert page.evaluate("getComputedStyle(document.querySelector('.vm-app,#makerPanel,.workspace')).getPropertyValue('--studio-accent').trim()")=='#315edb'
            for path in pages:
                page.goto('https://studio.test'+path);page.wait_for_timeout(200)
                geometry(path)
                rail=page.locator('[data-video-studio-subnav],.vps1-native-video-tabs').first
                box=rail.bounding_box();assert abs(box['height']-62)<2,(path,width,box)
                if path=='/short-maker/':
                    field=page.locator('input[name="image_duration"]');field.fill('4.5')
                    page.locator('#queueTabBtn').click();expect(page.locator('#queuePanel')).to_be_visible();geometry('maker queue')
                    page.locator('#makerTabBtn').click();expect(field).to_have_value('4.5')
                    row=page.locator('#makerPanel .panel').first
                elif path=='/video-merger/':
                    page.locator('[name="filenameMode"][value="manual"]').check();page.locator('#fname').fill('retained-name')
                    for tab in ('library','proxy','merge'):
                        page.locator(f'.vm-app .tab[data-tab="{tab}"]').click();expect(page.locator('#tab-'+tab)).to_be_visible();geometry('merger '+tab)
                    expect(page.locator('#fname')).to_have_value('retained-name')
                    page.locator('.trim-check').first.check();expect(page.locator('.trim-fields').first).to_be_visible();geometry('video trim')
                    row=page.locator('.vm-app .card').first
                else:
                    for tab in ('queue','upload','drive-profiles','preset','captions','pexels','fg','voice','output'):
                        page.locator(f'.workspace>.tabs [data-page="{tab}"]').click();expect(page.locator('#'+tab)).to_be_visible();geometry('ACS '+tab)
                        if tab=='captions':
                            for sub in ('scroll','caption','style'):
                                page.locator(f'[data-capmode="{sub}"]').click();geometry('caption '+sub)
                    page.locator('[data-page="upload"]').first.click()
                    row=page.locator('#upload .card').first
                # Density changes are measured, not inferred from source declarations.
                compact=row.evaluate('(e)=>parseFloat(getComputedStyle(e).paddingTop)')
                page.evaluate("""() => {window.__compactImports=[];for(const sheet of document.styleSheets) for(let i=sheet.cssRules.length-1;i>=0;i--) if(sheet.cssRules[i].type===CSSRule.IMPORT_RULE){window.__compactImports.push([sheet,i,sheet.cssRules[i].cssText]);sheet.deleteRule(i);}}""")
                original=row.evaluate('(e)=>parseFloat(getComputedStyle(e).paddingTop)')
                page.evaluate("""() => {for(const [sheet,i,text] of window.__compactImports)sheet.insertRule(text,i);}""")
                assert compact<original,(path,width,compact,original)
                page.wait_for_timeout(150)
                if os.environ.get('STUDIO_COMPACT_SCREENSHOTS'):
                    folder=Path(os.environ['STUDIO_COMPACT_SCREENSHOTS']);folder.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(folder/(path.strip('/')+f'-{width}.png')),full_page=True)
                assert not errors,(path,width,errors)
                assert not failures,failures
                assert not writes,('browsing mutated API',writes)
                print('PASS compact studio',width,path,'tabs, density, no overflow, no writes',flush=True)
            page.close()
        browser.close()

if __name__=='__main__':main()

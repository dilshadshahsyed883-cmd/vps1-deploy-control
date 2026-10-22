"""Exercise real config/snapshot code without importing production workers."""
import ast
from contextlib import contextmanager
import importlib.util
import itertools
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PART = ROOT/'apps/scene-builder/part01'
SOURCE = ast.parse((PART/'app.py').read_text())

def functions(*names, **scope):
    nodes = [node for node in SOURCE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    nodes = [ast.parse(ast.unparse(node)).body[0] for node in nodes]
    for node in nodes: node.decorator_list = []
    code = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), '<actual Scene functions>', 'exec'), scope)
    return scope

class SMLSettings(unittest.TestCase):
    def test_settings_defaults_and_each_toggle(self):
        values={}
        scope=functions('scene_sml_settings',setting=lambda key,default: values.get(key,default))
        get=scope['scene_sml_settings']
        self.assertTrue(get()['sml_enabled'])
        self.assertEqual(get()['media_source_mode'],'sml_first')
        for key in ('sml_enabled','sml_auto_scene_matching','sml_manual_audit_selection','sml_clip_preview'):
            values[key]='0'; self.assertFalse(get()[key]); values[key]='1'; self.assertTrue(get()[key])

    def test_sqlite_snapshot_survives_global_changes_and_duplicate_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'test.sqlite3'
            @contextmanager
            def db():
                with sqlite3.connect(path) as conn:
                    conn.row_factory=sqlite3.Row
                    yield conn
            with db() as conn:
                conn.execute('CREATE TABLE job_ownership(job_id TEXT PRIMARY KEY,owner_username TEXT,project_name TEXT,created_at TEXT)')
                conn.execute('CREATE TABLE job_sml_settings(job_id TEXT PRIMARY KEY,settings_json TEXT NOT NULL)')
            scope=functions('record_job_owner','job_sml_settings',db=db,json=json,now_iso=lambda:'now')
            old={'sml_enabled':False,'sml_clip_preview':False}
            scope['record_job_owner']('job','owner','project',old)
            old['sml_enabled']=True
            scope['record_job_owner']('job','owner','project',old)
            self.assertFalse(scope['job_sml_settings']('job')['sml_enabled'])
            self.assertTrue(scope['job_sml_settings']('legacy')['sml_enabled'])

    def test_real_save_mapping_handles_unchecked_and_cached_forms(self):
        save=next(x for x in SOURCE.body if isinstance(x,ast.FunctionDef) and x.name=='save_settings')
        node=next(x for x in save.body if isinstance(x,ast.If) and ast.unparse(x.test)=="sml_controls_present == '1'")
        code=compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'<save mapping>','exec')
        for present in (None,'1'):
            scope=dict(values={},sml_controls_present=present,sml_enabled=None,media_source_mode='pexels_only',sml_media_order='smart',sml_target_percent=80,sml_auto_scene_matching=None,sml_manual_audit_selection='on',sml_clip_preview=None)
            exec(code,scope)
            self.assertEqual(scope['values'].get('sml_enabled'), '0' if present else None)
            if present:self.assertEqual(scope['values']['sml_manual_audit_selection'],'1')

    def test_package_priority_uses_saved_config(self):
        build=next(x for x in SOURCE.body if isinstance(x,ast.FunctionDef) and x.name=='build_project_package')
        metadata=next(x.value for x in build.body if isinstance(x,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='metadata' for t in x.targets))
        # Execute actual SML entries of the package expression for every combination.
        selected=[(k,v) for k,v in zip(metadata.keys,metadata.values) if k is None or isinstance(k,ast.Constant) and k.value in ('media_source_mode','sml_auto_scene_matching')]
        expr=compile(ast.fix_missing_locations(ast.Expression(ast.Dict(keys=[x[0] for x in selected],values=[x[1] for x in selected]))),'<package SML metadata>','eval')
        for enabled,auto,mode in itertools.product((False,True),(False,True),('sml_first','pexels_only')):
            result=eval(expr,{'sml_config':dict(sml_enabled=enabled,sml_auto_scene_matching=auto,media_source_mode=mode)})
            self.assertEqual(result['media_source_mode'], mode if enabled else 'pexels_only')
            self.assertEqual(result['sml_auto_scene_matching'],enabled and auto)

    def test_live_counts_authenticate_and_use_total_not_page_size(self):
        spec=importlib.util.spec_from_file_location('sml_counts',PART/'sml_scene_bridge.py')
        bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
        with patch.object(bridge,'_request',side_effect=[
            {'items':[{}],'total':321}, {'items':[],'total':0},
            {'counts':{'video':{'total':400,'candidate':79},'image':{'total':50,'candidate':50}}}
        ]) as api:
            status=bridge.library_status()
            self.assertEqual(status['clips'],321)
            self.assertEqual(status['images'],0)
            self.assertEqual(status['inventory']['image']['candidate'],50)
            self.assertEqual(api.call_args_list[0].kwargs['params']['media_type'],'video')
        with patch.object(bridge,'_request',return_value={'items':[]}):
            with self.assertRaises(bridge.SMLUnavailable):bridge.library_status()

    def test_disabled_automatic_and_manual_routes_do_no_transfer(self):
        class HTTPError(Exception):
            def __init__(self,code,message):self.status_code=code
        class Response:
            def json(self):return {'items':[]}
        worker=lambda *args:Response()
        scope=functions('scene_sml_auto_match','scene_sml_manual_select', require_job_access=lambda *a:None,
                        job_sml_settings=lambda job:dict(sml_enabled=False),worker_request=worker,
                        _SML_AUDIT_LOCK=__import__('threading').RLock(),_web_audit_payload=lambda job,p:p,
                        HTTPException=HTTPError)
        self.assertEqual(scope['scene_sml_auto_match'](None,'job')['matched'],0)
        import asyncio
        with self.assertRaises(HTTPError) as error:asyncio.run(scope['scene_sml_manual_select'](None,'job','item'))
        self.assertEqual(error.exception.status_code,409)

    def test_rendered_audit_javascript_parses_and_controls_stay_in_stock_media(self):
        from jinja2 import Environment,DictLoader
        settings=(PART/'templates/settings.html').read_text()
        media=settings.split('data-panel="media">',1)[1].split('data-panel=',1)[0]
        self.assertLess(media.index('Self Media Library (SML)'),media.index('<strong>Pexels</strong>'))
        for key in ('sml_enabled','media_source_mode','sml_auto_scene_matching','sml_manual_audit_selection','sml_clip_preview'):
            self.assertIn('name="'+key+'"',media)
        env=Environment(loader=DictLoader({'base.html':'{% block content %}{% endblock %}{% block scripts %}{% endblock %}'}))
        template=env.from_string((PART/'templates/audit_media.html').read_text())
        rendered=template.render(selected_job={'id':'fixture','project_name':'test'},audit={'items':[],'sml_settings':{}},waiting_jobs=[])
        js=rendered.split('<script>',1)[1].split('</script>',1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'audit.js';path.write_text(js)
            subprocess.run(['node','--check',str(path)],check=True,capture_output=True)

if __name__=='__main__':unittest.main()

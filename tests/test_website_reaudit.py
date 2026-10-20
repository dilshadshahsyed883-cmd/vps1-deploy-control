"""Offline behavior checks for the approved website re-audit, with no production data."""
from __future__ import annotations
import ast
import asyncio
import json
from pathlib import Path
import re
import secrets
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]


def function(path,name,bindings):
    tree=ast.parse((ROOT/path).read_text())
    node=next(n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
    node.decorator_list=[]
    code=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
    ast.fix_missing_locations(code);scope=dict(bindings);exec(compile(code,str(path),'exec'),scope);return scope[name]


class ReauditBehavior(unittest.TestCase):
    def test_drive_share_url_rejected_before_creating_background_job(self):
        from fastapi import HTTPException
        fn=function('apps/self-media-library/part01/media_extensions.py','create_import',{'HTTPException':HTTPException})
        for value in ('https://drive.google.com/file/d/example/view','../clip.mp4','Videos//clip.mp4','gdrive:clip.mp4'):
            with self.assertRaises(HTTPException) as caught:fn(SimpleNamespace(source='drive',value=value))
            self.assertEqual(caught.exception.status_code,400)
            self.assertIn('relative',caught.exception.detail)

    def test_audio_settings_validate_token_before_persisting(self):
        from fastapi import HTTPException
        check=function('apps/scene-builder/part01/app.py','require_settings_csrf',{'HTTPException':HTTPException,'secrets':secrets})
        saved={};request=SimpleNamespace(session={'settings_csrf':'valid'},headers={'x-settings-csrf':'wrong'})
        async def payload():return {'preset':'clean','highpass':85,'arbitrary_path':'/tmp/no','nested':{}}
        request.json=payload
        fn=function('apps/scene-builder/part01/app.py','scene_audio_settings_save',{'require_login':lambda r:None,'require_settings_csrf':check,'HTTPException':HTTPException,'json':json,'set_setting':lambda k,v:saved.update({k:json.loads(v)})})
        with self.assertRaises(HTTPException):asyncio.run(fn(request))
        self.assertFalse(saved)
        request.headers['x-settings-csrf']='valid';result=asyncio.run(fn(request))
        self.assertTrue(result['ok']);self.assertEqual(saved['scene_audio_processing'],{'preset':'clean','highpass':85})

    def test_human_formatted_systemd_monotonic_deadline(self):
        from production.test_storage_auto_clean_view import load_manager
        module=load_manager()
        def command(args,**kwargs):
            if args[1]=='show':return SimpleNamespace(stdout='NextElapseUSecMonotonic=1w 2d 3h 4min 10s\n' if 'Monotonic' in args[3] else 'NextElapseUSecRealtime=n/a\n')
            return SimpleNamespace(stdout='active\n' if args[1]=='is-active' else 'enabled\n')
        deadline=9*86400+3*3600+4*60+10
        with patch.object(module,'run',side_effect=command),patch.object(module.time,'clock_gettime',return_value=deadline-60),patch.object(module,'_public_deployment_gc_status',return_value=None):
            result=module.deployment_cleanup_status()
        self.assertIsNotNone(result['next_run_at']);self.assertEqual(result['status'],'active')

    def test_auto_stream_requires_selection_and_saved_policy(self):
        source=(ROOT/'apps/short-stream/part01/auto_stream_ui.py').read_text()
        start=source.index('function updateActionEligibility()');end=source.index("\ndocument.addEventListener('input'",start)
        code='''const assert=require('node:assert/strict');let jobs=[],channels=[],flags={};const fields={};const qs=id=>fields[id]||(fields[id]={value:'',disabled:false});
'''+source[start:end]+'''
updateActionEligibility();assert.equal(qs('studio-live').disabled,true);assert.equal(qs('manual-start').disabled,true);
jobs=[{id:'job',channel_id:'channel',status:'INGESTING'}];channels=[{id:'channel',authorized:true,active:true}];qs('selected-job').value='job';updateActionEligibility();assert.equal(qs('studio-live').disabled,true);
flags={go_live_enabled:true,transitions_enabled:true,publishing_enabled:true,youtube_mutations_enabled:true};updateActionEligibility();assert.equal(qs('studio-live').disabled,false);
channels[0].authorized=false;updateActionEligibility();assert.equal(qs('studio-live').disabled,true);
'''
        subprocess.run(['node','-e',code],check=True,capture_output=True,text=True)

    def test_acs_missing_media_and_preview_never_enables_submit(self):
        source=(ROOT/'apps/auto-caption-studio/part01/web_backend/acs_studio.html').read_text()
        start=source.index('function updatePreflight()');end=source.index('\nasync function submitReady',start)
        code='''const assert=require('node:assert/strict');const fields={};const document={getElementById:id=>fields[id]||(fields[id]={value:'Upload Audio',textContent:'',className:'',innerHTML:''})};const p={name:'Fixture',preview_approved:false,settings:{pexels_enabled:true,use_image_backgrounds:true,use_video_backgrounds:true}};const appState={uploadItems:[{valid:true}],bootstrap:{pexels:{image_count:0,video_count:0}},fg:[]};const selectedPreset=()=>p,textRenderModeFromSettings=()=> 'caption',fgName=x=>x,voiceName=x=>x,esc=String;const submit={disabled:false};const page=()=>({querySelector:()=>({})});const buttonByText=()=>submit;
'''+source[start:end]+'''
updatePreflight();assert.equal(submit.disabled,true);assert.equal(appState.preflightReady,false);
p.preview_approved=true;updatePreflight();assert.equal(submit.disabled,true);
appState.bootstrap.pexels={image_count:3,video_count:2};updatePreflight();assert.equal(submit.disabled,false);
appState.uploadItems=[];updatePreflight();assert.equal(submit.disabled,true);
'''
        subprocess.run(['node','-e',code],check=True,capture_output=True,text=True)


if __name__=='__main__':unittest.main()

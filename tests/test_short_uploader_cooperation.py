"""Short Uploader's opt-in participant in the Short Maker retirement barrier.

Uses two independently loaded modules from the *actual* uploader and Maker
application directories; no production uploader DB, YouTube or Drive access.
"""
import ast
import asyncio
import importlib.util
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

ROOT=Path(__file__).resolve().parents[1]
MAKER=ROOT/"apps/short-maker/part01"
UPLOADER=ROOT/"apps/local-upload-manager/part01/lum_engine"


def load(path, module_name):
    spec=importlib.util.spec_from_file_location(module_name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contender(role, folder_root, result):
    os.environ["LUM_ENGINE_INSTANCE"]="shortstudio"
    os.environ["SHORT_STUDIO_VPS_FOLDER_ROOT"]=str(folder_root)
    os.environ["LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED"]="1"
    uploader=load(UPLOADER/"uploader_shortstudio_cooperation.py","isolated_uploader_contender")
    try:
        with (uploader._uploader_lease.retire() if role=="exclusive"
              else uploader.uploader_shortstudio_guard()):
            result.put("acquired")
    except HTTPException as exc:
        result.put("blocked-"+str(exc.status_code))


class ShortUploaderCooperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/"common-root"
        self.root.mkdir()
        self.env={
            "LUM_ENGINE_INSTANCE":"shortstudio",
            "SHORT_STUDIO_VPS_FOLDER_ROOT":str(self.root),
            "LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED":"1",
            "SHORT_MAKER_COOPERATIVE_LOCK_ENABLED":"1",
        }
        with patch.dict(os.environ,self.env):
            self.maker=load(MAKER/"short_folder_cooperation.py","maker_barrier_tests")
            self.uploader=load(UPLOADER/"uploader_shortstudio_cooperation.py","uploader_barrier_tests")
        self.maker_guard=self.maker.ShortStudioFolderLease(
            self.root,enabled=True,timeout_seconds=.25)
        self.uploader._uploader_lease.timeout=.25

    def tearDown(self):
        self.tmp.cleanup()

    def child(self,role):
        ctx=multiprocessing.get_context("spawn")
        result=ctx.Queue()
        child=ctx.Process(target=contender,args=(role,self.root,result))
        child.start()
        child.join(timeout=8)
        self.assertFalse(child.is_alive(),"Uploader lock contention hung")
        self.assertEqual(child.exitcode,0)
        return result.get(timeout=2)

    def test_binary_compatible_lock_primitive_and_filename(self):
        def implementation(path):
            tree=ast.parse(path.read_text(encoding="utf-8"))
            value=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=="ShortStudioFolderLease")
            return ast.dump(value,include_attributes=False)
        self.assertEqual(implementation(MAKER/"short_folder_cooperation.py"),
                         implementation(UPLOADER/"uploader_shortstudio_cooperation.py"))
        self.assertEqual(self.maker.LOCK_FILENAME,self.uploader.LOCK_FILENAME)

    def test_maker_shared_lock_blocks_independent_uploader_exclusive(self):
        with self.maker_guard.producer():
            self.assertEqual(self.child("exclusive"),"blocked-503")
            self.assertEqual(self.child("shared"),"acquired")
        self.assertEqual(self.child("exclusive"),"acquired")

    def test_uploader_shared_lock_blocks_maker_retirement(self):
        with self.uploader.uploader_shortstudio_guard():
            with self.assertRaises(HTTPException) as denied:
                with self.maker_guard.retire(): pass
            self.assertEqual(denied.exception.status_code,503)
        with self.maker_guard.retire():
            self.assertEqual(self.child("shared"),"blocked-503")

    def test_uploader_async_guard_uses_same_physical_lock(self):
        async def exercise():
            async with self.uploader.uploader_shortstudio_guard_async():
                self.assertEqual(self.child("exclusive"),"blocked-503")
        asyncio.run(exercise())
        self.assertEqual(self.child("exclusive"),"acquired")

    def test_disabled_default_does_not_create_lock_or_affect_other_modes(self):
        path=UPLOADER/"uploader_shortstudio_cooperation.py"
        for params in (
            {"LUM_ENGINE_INSTANCE":"shortstudio","LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED":"0"},
            {"LUM_ENGINE_INSTANCE":"song","LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED":"1"},
            {"LUM_ENGINE_INSTANCE":"copyright","LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED":"1"},
        ):
            with patch.dict(os.environ,{**self.env,**params}):
                module=load(path,"uploader_cooperation_disabled")
            self.assertFalse(module.UPLOADER_COOPERATION_ENABLED)
            with module.uploader_shortstudio_guard():
                pass
        self.assertFalse((self.root/self.maker.LOCK_FILENAME).exists())

    def test_opt_in_requires_explicit_canonical_shared_root(self):
        for module,env_flag in (
            (UPLOADER/"uploader_shortstudio_cooperation.py","LUM_SHORT_STUDIO_COOPERATIVE_LOCK_ENABLED"),
        ):
            with patch.dict(os.environ,{"LUM_ENGINE_INSTANCE":"shortstudio",
                   env_flag:"1"},clear=True):
                with self.assertRaises(RuntimeError):
                    load(module,"uploader_without_root")

    def test_entrypoints_cover_web_scheduler_worker_and_cleanup(self):
        web=ast.parse((UPLOADER/"standalone_app.py").read_text(encoding="utf-8"))
        scheduler=ast.parse((UPLOADER/"auto_engine_v6_scheduler.py").read_text(encoding="utf-8"))
        worker=ast.parse((UPLOADER/"auto_engine_v6_worker.py").read_text(encoding="utf-8"))
        def function(tree,name):
            return next(x for x in tree.body if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name==name)
        webfn=function(web,"shortstudio_folder_cooperation")
        self.assertTrue(any(isinstance(x,ast.AsyncWith) and
            any(isinstance(y.context_expr,ast.Call) and
                isinstance(y.context_expr.func,ast.Name) and
                y.context_expr.func.id=="uploader_shortstudio_guard_async"
                for y in x.items) for x in ast.walk(webfn)))
        schedfn=function(scheduler,"main")
        guards=[x for x in ast.walk(schedfn) if isinstance(x,ast.With)
            and any(isinstance(item.context_expr,ast.Call) and
                isinstance(item.context_expr.func,ast.Name) and
                item.context_expr.func.id=="uploader_shortstudio_guard"
                for item in x.items)]
        self.assertTrue(guards)
        self.assertTrue(any(any(isinstance(x,ast.Call) and
              isinstance(x.func,ast.Name) and x.func.id=="check_profile"
              for x in ast.walk(guard)) for guard in guards))
        workerfn=function(worker,"main")
        blocks=[x for x in ast.walk(workerfn) if isinstance(x,ast.With)
              and any(isinstance(y.context_expr,ast.Call) and
                  isinstance(y.context_expr.func,ast.Name) and
                  y.context_expr.func.id=="uploader_shortstudio_guard"
                  for y in x.items)]
        self.assertGreaterEqual(len(blocks),3)
        self.assertFalse(any(
            isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
            and isinstance(node.func.value,ast.Name)
            and node.func.value.id=="time" and node.func.attr=="sleep"
            for block in blocks for node in ast.walk(block)
        ),"Uploader must not hold a shared lease while idling")
        chain={"recover_channel_jobs","retry_post_actions","retry_proxy_waits",
               "retry_network_waits","claim_available_jobs","process_one"}
        actual=set()
        for block in blocks:
            for call in (x for x in ast.walk(block) if isinstance(x,ast.Call)):
                if isinstance(call.func,ast.Name):
                    actual.add(call.func.id)
                elif isinstance(call.func,ast.Attribute):
                    actual.add(call.func.attr)
        self.assertFalse(chain-actual,"Unprotected uploader operation: "+str(chain-actual))


if __name__=="__main__":
    unittest.main()

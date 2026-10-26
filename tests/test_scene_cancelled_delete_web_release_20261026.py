"""Guarded Scene cancelled-job Delete web overlay tests."""
import ast
import re
import unittest
from pathlib import Path
R = Path(__file__).resolve().parents[1]
SOURCE = (R/"production/scene_cancelled_delete_web_release.py").read_text()
QUEUE = (R/"apps/scene-builder/part01/templates/queue.html").read_text()
WEB = (R/"apps/scene-builder/part01/app.py").read_text()
POLICY = (R/"apps/scene-builder/part01/precutover_policy.py").read_text()
RUNTIME = (R/"production/runtime.py").read_text()

def patchers():
    tree = ast.parse(SOURCE)
    nodes = [x for x in tree.body if isinstance(x, ast.FunctionDef)
             and x.name in ("patch_policy", "patch_web", "patch_queue")]
    nodes += [x for x in tree.body if isinstance(x, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id in ("DELETE_BUTTON","DELETE_ROUTE")
                      for t in x.targets)]
    scope = {"re": re, "require":lambda ok,msg:None if ok else
             (_ for _ in ()).throw(RuntimeError(msg))}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),"overlay","exec"),scope)
    return scope

class CancelledDeleteWebTests(unittest.TestCase):
    def test_guarded_web_only_route(self):
        ast.parse(SOURCE)
        self.assertIn("[vps1-scene-delete-web-only]",RUNTIME)
        self.assertIn("release_cancelled_delete(revision)",RUNTIME)
        self.assertIn("atomic_link(CURRENT, old)",SOURCE)
        self.assertIn("WORKER=UNTOUCHED",SOURCE)
        self.assertNotIn("scene-production-worker.service",SOURCE)
        route=patchers()["DELETE_ROUTE"]
        ast.parse(route)
        for term in ("require_job_access(request, job_id)",
                     'lower() != "cancelled"', 'current.get("cancel_requested")',
                     'current.get("drive_destination")',
                     'worker_request("DELETE", f"/jobs/{job_id}"',
                     'DELETE FROM job_ownership', 'DELETE FROM job_sml_settings'):
            self.assertIn(term,route)

    def test_adds_button_once_without_changing_start(self):
        modify=patchers()["patch_queue"]
        start=QUEUE.index("function actionsFor(j){")
        end=QUEUE.index("\nfunction projectRow(j)",start)
        old=re.sub(r"\s*if\(s==='cancelled'\)\s*out\+=.*?Delete Permanently.*?;",
                   "",QUEUE[start:end],count=1,flags=re.S)
        self.assertNotIn("Delete Permanently",old)
        result=modify(QUEUE[:start]+old+QUEUE[end:])
        self.assertIn("Delete Permanently",result)
        self.assertIn("if(s==='ready_to_render'",result)
        self.assertNotIn("out+=\\"+chr(96),result)
        self.assertEqual(modify(result),result)

    def test_route_preserves_other_handlers(self):
        change=patchers()["patch_web"]
        b=WEB.index('@app.post("/queue/{job_id}/delete")')
        e=WEB.index("\n\n@app.post(",b+2)
        result=change(WEB[:b]+WEB[e+2:])
        ast.parse(result)
        self.assertIn('@app.post("/queue/{job_id}/delete")',result)
        self.assertIn('@app.post("/queue/{job_id}/start")',result)
        self.assertEqual(change(result),result)

    def test_candidate_delete_requires_exact_post_and_editing(self):
        change=patchers()["patch_policy"]
        old=POLICY.replace("cancel|delete|start|","cancel|start|")
        result=change(old)
        scope={}
        exec(compile(result,"policy","exec"),scope)
        url="/queue/"+"a"*32+"/delete"
        self.assertTrue(scope["mutation_allowed"]("POST",url,True))
        self.assertFalse(scope["mutation_allowed"]("POST",url,False))
        self.assertFalse(scope["mutation_allowed"]("GET",url,True))
        self.assertFalse(scope["mutation_allowed"]("POST","/queue/invalid/delete",True))
        self.assertEqual(change(result),result)

if __name__=="__main__":unittest.main()

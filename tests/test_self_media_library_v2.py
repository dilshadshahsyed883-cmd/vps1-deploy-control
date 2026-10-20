"""Self Media Library V2 additive API and safety regression tests."""
import ast
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

APP=Path(__file__).resolve().parents[1]/"apps/self-media-library/part01"

class Contract(unittest.TestCase):
    def test_files_and_schema(self):
        ext=(APP/"media_extensions.py").read_text()
        py=(APP/"app.py").read_text()
        ast.parse(ext);ast.parse(py)
        self.assertIn("register_media_extensions(app,",py)
        for needle in ("image_assets","media_reservations","SML_EDITOR_API_TOKEN","BEGIN IMMEDIATE","drive_preflight","upload_and_verify"):
            self.assertIn(needle,ext)
        html=(APP/"static/index.html").read_text()
        for needle in ('id="imagesGrid"','id="imageFiles"','id="importHistory"','id="editorApiPill"'):
            self.assertIn(needle,html)
        self.assertEqual(html.count("<section "),html.count("</section>"))

    def test_diverse_ranking(self):
        spec=importlib.util.spec_from_file_location("rank_v2",APP/"media_extensions.py")
        mod=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rows=[{"id":"a","source_id":"A","quality":95,"keywords":["forest"]},
              {"id":"b","source_id":"B","quality":75,"keywords":["forest"]}]
        self.assertEqual(mod.rank_candidates(rows,["forest"],last_source="A")[0]["id"],"b")
        self.assertEqual(mod.normalized_tags(["Forest","forest","  SUN  "]),["forest","sun"])

@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),"FFmpeg required")
class API(unittest.TestCase):
    def test_photo_review_search_reservation_and_trash(self):
        from fastapi.testclient import TestClient
        t=tempfile.TemporaryDirectory(prefix="sml-v2-")
        before={x:os.environ.get(x) for x in ("SML_DATA_ROOT","SML_EDITOR_API_TOKEN","AI_ANALYZER_URL")}
        os.environ["SML_DATA_ROOT"]=t.name
        os.environ["SML_EDITOR_API_TOKEN"]="test-token"
        os.environ.pop("AI_ANALYZER_URL",None)
        sys.path.insert(0,str(APP))
        try:
            sys.modules.pop("app",None)
            import app as module
            png=Path(t.name)/"sample.png"
            subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error","-f","lavfi",
                            "-i","color=c=blue:s=64x64:d=1","-frames:v","1",
                            "-threads","1","-update","1",str(png)],check=True,timeout=30)
            with TestClient(module.app) as c:
                self.assertEqual(c.get("/api/health").json()["version"],"2.0.0")
                self.assertTrue(c.get("/api/v1/media/status").json()["v2"])
                with png.open("rb") as f:
                    up=c.post("/api/v1/images/upload",files={"file":("mountain.png",f,"image/png")})
                self.assertEqual(up.status_code,200,up.text)
                id=up.json()["id"]
                self.assertEqual(c.get("/api/v1/images").json()["total"],1)
                self.assertEqual(c.patch("/api/v1/images/"+id,json={"status":"approved","keywords":["mountain","lake"]}).status_code,200)
                self.assertEqual(c.get("/api/v1/media/search?query=mountain").status_code,401)
                h={"X-SML-Editor-Token":"test-token"}
                self.assertEqual(c.get("/api/v1/media/search?query=mountain&media_type=image",headers=h).json()["total"],1)
                r=c.post("/api/v1/media/reservations",headers=h,json={"project_id":"p1","scene_id":"s1","query":"mountain","media_type":"image"})
                self.assertEqual(r.status_code,200,r.text)
                lease=r.json()["items"][0]["reservation_id"]
                url="/api/v1/media/reservations/"+lease+"/commit"
                self.assertEqual(c.post(url,headers=h).json()["state"],"committed")
                self.assertTrue(c.post(url,headers=h).json()["idempotent"])
                self.assertEqual(c.get("/api/v1/images/"+id+"/preview").status_code,200)
                self.assertEqual(c.delete("/api/v1/images/"+id).status_code,200)
                self.assertEqual(c.get("/api/v1/media/search?query=mountain",headers=h).json()["total"],0)
        finally:
            sys.modules.pop("app",None)
            sys.path.remove(str(APP))
            for k,v in before.items():
                if v is None:os.environ.pop(k,None)
                else:os.environ[k]=v
            t.cleanup()

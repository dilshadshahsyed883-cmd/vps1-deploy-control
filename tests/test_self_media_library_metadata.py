"""Self Media Library V2 phase 2: technical metadata, orientation filters and origins."""
from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"
sys.path.insert(0, str(APP))
from media_catalog import classify_orientation, safe_origin, probe_video


class MetadataUnits(unittest.TestCase):
    def test_orientation(self):
        self.assertEqual(classify_orientation(1920, 1080), "landscape")
        self.assertEqual(classify_orientation(1080, 1920), "portrait")
        self.assertEqual(classify_orientation(1000, 980), "square")
        self.assertEqual(classify_orientation(0, 1080), "unknown")

    def test_safe_origin_drops_unsafe_query_params(self):
        self.assertEqual(safe_origin("youtube","https://www.youtube.com/watch?v=abc123DEF09&token=SECRET"),"youtube:abc123DEF09")
        self.assertEqual(safe_origin("youtube","https://youtu.be/abc123DEF09?t=10"),"youtube:abc123DEF09")
        self.assertEqual(safe_origin("youtube","https://evil.invalid/watch?v=abc123DEF09"),"")
        self.assertEqual(safe_origin("drive","Footage/raw/final.mp4"),"Footage/raw/final.mp4")
        self.assertEqual(safe_origin("drive","../../etc/shadow"),"")
        self.assertEqual(safe_origin("local","C:/private/video.mp4"),"")

    def test_ffprobe_failure_is_non_destructive(self):
        with tempfile.TemporaryDirectory() as root:
            bad = Path(root) / "invalid.mp4"
            bad.write_bytes(b"not really a video")
            info=probe_video(bad)
            self.assertEqual(info["orientation"],"unknown")
            self.assertEqual(info["width"],0)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe") and importlib.util.find_spec("fastapi") is not None,
                     "Real FFmpeg/FastAPI integration needed")
class MetadataIntegration(unittest.TestCase):
    def test_real_video_image_metadata_and_filtered_results(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory(prefix="sml-p2-") as root:
            before=os.environ.get("SML_DATA_ROOT")
            os.environ["SML_DATA_ROOT"]=root
            sys.modules.pop("app",None)
            try:
                import app as module
                video=Path(root)/"test.mp4"
                image=Path(root)/"test.png"
                subprocess.run(["ffmpeg","-y","-loglevel","error","-f","lavfi",
                                "-i","testsrc2=size=160x90:rate=15:duration=3",
                                "-c:v","libx264","-pix_fmt","yuv420p",str(video)],check=True,timeout=40)
                subprocess.run(["ffmpeg","-y","-loglevel","error","-f","lavfi",
                                "-i","color=c=blue:s=64x96:d=1",
                                "-frames:v","1","-threads","1","-update","1",str(image)],
                               check=True,timeout=30)
                with TestClient(module.app) as c:
                    result=c.post("/api/sources/upload",files={"file":("landscape.mp4",io.BytesIO(video.read_bytes()),"video/mp4")})
                    self.assertEqual(result.status_code,200,result.text)
                    source_id=result.json()["id"]
                    source=next(s for s in c.get("/api/sources").json()["items"] if s["id"]==source_id)
                    self.assertEqual((source["width"],source["height"]),(160,90))
                    self.assertEqual(source["orientation"],"landscape")
                    self.assertEqual(source["codec"],"h264")
                    self.assertEqual(source["source_kind"],"local")
                    self.assertAlmostEqual(source["fps"],15.0,places=1)
                    module.process_source(source_id)
                    good=c.get("/api/clips?orientation=landscape").json()
                    self.assertGreaterEqual(good["total"],1)
                    self.assertTrue(all(x["source_orientation"]=="landscape" for x in good["items"]))
                    self.assertEqual(c.get("/api/clips?orientation=portrait").json()["total"],0)
                    self.assertEqual(c.get("/api/clips?orientation=wat").status_code,400)
                    img=c.post("/api/v1/images/upload",files={"file":("portrait.png",io.BytesIO(image.read_bytes()),"image/png")})
                    self.assertEqual(img.status_code,200,img.text)
                    items=c.get("/api/v1/images?orientation=portrait").json()
                    self.assertEqual(items["total"],1)
                    self.assertEqual(items["items"][0]["source_kind"],"local")
                    self.assertEqual(items["items"][0]["orientation"],"portrait")
                    self.assertEqual(c.get("/api/v1/images?orientation=landscape").json()["total"],0)
                    self.assertEqual(c.get("/api/v1/images?orientation=wat").status_code,400)
            finally:
                sys.modules.pop("app",None)
                if before is None:os.environ.pop("SML_DATA_ROOT",None)
                else:os.environ["SML_DATA_ROOT"]=before


class UIContract(unittest.TestCase):
    def test_ui_supports_multiple_videos_and_real_orientation_filters(self):
        html=(APP/"static/index.html").read_text()
        js=(APP/"static/app.js").read_text()
        self.assertIn('id="sourceFile"',html)
        self.assertIn('video/*" multiple hidden',html)
        for control in ["sourceUploadResult","clipOrientation","imageOrientation","imageTechnical"]:
            self.assertIn('id="'+control+'"',html)
            self.assertIn("'#"+control+"'",js)
        self.assertIn("async function uploadFiles(files)",js)
        self.assertIn("source_orientation",js)
        self.assertIn("imageOrientation",js)

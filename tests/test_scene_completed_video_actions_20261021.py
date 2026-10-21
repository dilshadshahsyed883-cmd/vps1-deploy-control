"""Regression checks for completed Scene MP4 viewing, downloads and delivery safety."""
from __future__ import annotations
import ast
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
Q = (SCENE / "templates/queue.html").read_text(encoding="utf-8")
WEB = (SCENE / "app.py").read_text(encoding="utf-8")
WORKER = (ROOT / "apps/scene-builder/part02/api_server.py").read_text(encoding="utf-8")


class CompletedVideoActionsTests(unittest.TestCase):
    def test_real_js_completed_local_and_drive_cases(self):
        if not shutil.which("node"):
            self.skipTest("Node unavailable on this runner")
        self.assertIn("function actionsFor(j){", Q)
        fn = "function actionsFor(j){" + Q.split("function actionsFor(j){", 1)[1].split("\nfunction projectRow(j)", 1)[0]
        js = "\n".join([
            "const queueMode='normal', serialActiveJobId='';",
            "const esc=s=>String(s);",
            fn,
            "const local={id:'a'.repeat(32),status:'completed',output_file:'/worker/output/final.mp4',size_bytes:8200000,drive_status:'',drive_view_url:''};",
            "const result=actionsFor(local);",
            "if(!result.includes('View Video')||!result.includes('Download MP4'))throw Error('missing local actions');",
            "if(!result.includes('/queue/'+local.id+'/view')||!result.includes('/queue/'+local.id+'/download'))throw Error('bad paths');",
            "if(result.includes('View on Drive')||result.includes('Clean Now'))throw Error('fake delivery');",
            "const cleaned={...local,status:'cleaned',output_file:'',size_bytes:0,drive_status:'completed',drive_view_url:'https://drive.google.com/file/d/real/view'};",
            "const delivered=actionsFor(cleaned);",
            "if(!delivered.includes('View on Drive')||delivered.includes('Download MP4'))throw Error('cleaned actions invalid');",
            "const invalid={...local,output_file:'',size_bytes:0};",
            "if(actionsFor(invalid).includes('Download MP4'))throw Error('nonexistent output offered');",
            "const failed={...local,status:'failed'};",
            "if(!actionsFor(failed).includes('Restart Stage')||actionsFor(failed).includes('View Video'))throw Error('failed actions regression');",
        ])
        result = subprocess.run(["node", "--eval", js], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_app_download_and_inline_view_use_same_authz_and_range(self):
        ast.parse(WEB)
        start = WEB.index("def _stream_completed_job_video(")
        end = WEB.index("# SCENE_INTERNAL_AUDIO_STUDIO_WEB_API_V1", start)
        block = WEB[start:end]
        for needle in (
            "require_job_access(request, job_id)", 'request.headers.get("range"',
            '"Range": range_value', "Content-Range", "Accept-Ranges",
            "upstream.close()", "status_code=upstream.status_code",
            "inline=True", "inline=False", 'media_type="video/mp4"',
            "private, no-store", "X-Content-Type-Options",
            '@app.get("/queue/{job_id}/view")',
            '@app.get("/queue/{job_id}/download")',
        ):
            self.assertIn(needle, block, needle)
        self.assertNotIn("token=", block.replace("params=worker_params(),", ""))
        self.assertEqual(block.count("require_job_access(request, job_id)"), 1)
        self.assertIn("if upstream.status_code >= 400:", block)

    def test_worker_local_mp4_and_cleanup_are_preserved(self):
        ast.parse(WORKER)
        self.assertIn('@app.get("/jobs/{job_id}/download")', WORKER)
        self.assertIn('return FileResponse(path, media_type="video/mp4"', WORKER)
        block = WORKER.split("def _cleanup_transient_job_media(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('job_dir / "input"', block)
        self.assertNotIn('"final.mp4"', block)
        self.assertNotIn("shutil.rmtree(job_dir)", block)
        self.assertIn('drive_required = bool(DRIVE_UPLOAD_ENABLED)', WORKER)
        self.assertIn('status="completed",stage="Completed"', WORKER)
        self.assertIn('output_file=str(result),size_bytes=result_size', WORKER)


if __name__ == "__main__":
    unittest.main()

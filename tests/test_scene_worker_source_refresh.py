"""Scene worker source refresh: fail-closed invariants and disposable real FFmpeg canary."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from production import scene_production_worker as worker
from production import scene_worker_source_refresh as refresh

ROOT = Path(__file__).resolve().parents[1]
PART2 = ROOT / "apps/scene-builder/part02"


class WorkerSourceRefreshPolicy(unittest.TestCase):
    def test_queue_guard_denies_unknown_inflight_and_queued_without_changes(self):
        with tempfile.TemporaryDirectory(prefix="scene-worker-policy-ci-") as temp:
            state = Path(temp)
            db = state / "queue.sqlite3"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE jobs(id TEXT, status TEXT)")
                con.execute("INSERT INTO jobs VALUES('old','completed')")
                con.execute("INSERT INTO jobs VALUES('old2','failed')")
                con.execute("INSERT INTO jobs VALUES('clean-1','cleaned')")
                con.execute("INSERT INTO jobs VALUES('clean-2','cleaned')")
                con.execute("INSERT INTO jobs VALUES('cancel','cancelled')")
            with patch.object(worker, "STATE", state):
                refresh._require_idle()
                with sqlite3.connect(db) as con:
                    con.execute("INSERT INTO jobs VALUES('keep','queued')")
                with self.assertRaisesRegex(RuntimeError, "in-flight"):
                    refresh._require_idle()
                with sqlite3.connect(db) as con:
                    con.execute("UPDATE jobs SET status='processing' WHERE id='keep'")
                with self.assertRaisesRegex(RuntimeError, "in-flight"):
                    refresh._require_idle()
                with sqlite3.connect(db) as con:
                    con.execute("UPDATE jobs SET status='unknown' WHERE id='keep'")
                with self.assertRaisesRegex(RuntimeError, "in-flight"):
                    refresh._require_idle()
                with sqlite3.connect(db) as con:
                    self.assertEqual(con.execute("SELECT count(*) FROM jobs").fetchone()[0],6)

    def test_unchanged_renderer_bytes_do_not_require_worker_queue_idle(self):
        with tempfile.TemporaryDirectory(prefix="scene-ui-worker-parity-") as temp:
            root=Path(temp)
            old, newest = root/"active-worker", root/"new-web"
            for release in (old,newest):
                (release/"part02/module").mkdir(parents=True)
                (release/"part02/module/renderer.py").write_bytes(b"same renderer bytes")
                (release/"part01").mkdir()
            (old/"part01/auto_script_kokoro.css").write_text("old UI")
            (newest/"part01/auto_script_kokoro.css").write_text("new UI")
            self.assertTrue(refresh._identical_render_source(old,newest))
            # No queue read needed: a worker with identical bytes isn't restarted.
            with patch.object(refresh,"_require_idle",side_effect=AssertionError("queue touched")):
                self.assertTrue(refresh._identical_render_source(old,newest))
            (newest/"part02/module/renderer.py").write_bytes(b"new renderer implementation")
            self.assertFalse(refresh._identical_render_source(old,newest))
            (newest/"part02/module/renderer.py").write_bytes(b"same renderer bytes")
            (newest/"part02/module/sidecar.py").write_bytes(b"extra worker file")
            self.assertFalse(refresh._identical_render_source(old,newest))
            (newest/"part02/module/sidecar.py").unlink()
            (newest/"part02/module/linked.py").symlink_to("renderer.py")
            self.assertFalse(refresh._identical_render_source(old,newest))
            (newest/"part02/module/linked.py").unlink()
            (old/"part02/module/renderer.py").unlink()
            self.assertFalse(refresh._identical_render_source(old,newest))

    def test_worker_source_parity_gate_precedes_idle_guard(self):
        source=(ROOT/"production/scene_worker_source_refresh.py").read_text()
        gate=source.index("if _identical_render_source(old, latest):")
        idle=source.index("_require_idle()",gate)
        self.assertLess(gate,idle)
        self.assertIn("IDENTICAL_WORKER_SOURCE_UNCHANGED",source)
        self.assertIn("worker.require(current.get(\"ok\") is True",source)

    def test_file_swap_preserves_content_and_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "test.service"
            p.write_bytes(b"original\n")
            refresh._atomic_regular_file(p,b"replacement\n",0o644)
            self.assertEqual(p.read_bytes(),b"replacement\n")
            self.assertEqual(p.stat().st_mode & 0o777,0o644)

    def test_unknown_managed_record_fails_before_touching_service(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            record, unit, env, web = (root/x for x in ("state.json","unit.service","private.env","web.env"))
            for file in (record,unit,env,web): file.write_text("{}")
            with (patch.object(worker, "RECORD", record),
                  patch.object(worker, "UNIT_FILE", unit),
                  patch.object(worker, "ENV", env),
                  patch.object(worker, "WEB_ENV", web),
                  patch.object(worker.os, "geteuid", return_value=0),
                  patch.object(refresh.subprocess, "run") as run):
                with self.assertRaisesRegex(RuntimeError, "unrecognized production worker owner"):
                    refresh.SceneWorkerSourceRefresh(root,root).refresh()
                run.assert_not_called()
            self.assertEqual(unit.read_text(),"{}")

    def test_runuser_sets_writable_home_after_identity_switch(self):
        runner = Path("/srv/fresh-website/releases/scene-production-worker/revision")
        cmd = refresh._smoke_command(runner)
        self.assertEqual(cmd[:5],["runuser","-u","vps1scene","--","/usr/bin/env"])
        after_switch = cmd[5:-3]
        self.assertIn("HOME="+str(worker.STATE/"worker-home"),after_switch)
        self.assertIn("XDG_DATA_HOME="+str(worker.STATE/"worker-home/.local/share"),after_switch)
        self.assertIn("XDG_CONFIG_HOME="+str(worker.STATE/"worker-home/.config"),after_switch)
        self.assertIn("TMPDIR="+str(worker.STATE/"tmp"),after_switch)
        self.assertIn("PYTHONPATH="+str(runner)+":"+str(runner/"vps_engine"),after_switch)
        self.assertEqual(cmd[-2],"-c")

    def test_rollback_waits_for_old_worker_python_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            unit=root/"old.service"
            record=root/"record.json"
            unit.write_text("new")
            manager=refresh.SceneWorkerSourceRefresh(root,root)
            manager.original_unit=b"old"
            manager.original_record={"owner":"scene-production-worker-v1","source":"old"}
            manager.token="private"
            with (patch.object(worker,"UNIT_FILE",unit),
                  patch.object(worker,"RECORD",record),
                  patch.object(worker,"health",side_effect=[{},{"ok":True,"admission_only":False}]) as h,
                  patch.object(refresh.time,"sleep") as pause,
                  patch("production.runtime.run") as run):
                manager._restore()
                self.assertEqual(unit.read_text(),"old")
                self.assertEqual(h.call_count,2)
                pause.assert_called_once()
                self.assertEqual(run.call_count,2)
                self.assertIsNone(manager.original_unit)

    def test_missing_owned_worker_does_not_modify_any_unmanaged_service(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch.object(worker,"RECORD",root/"absent"),patch.object(worker.os,"geteuid",return_value=0):
                self.assertEqual(refresh.SceneWorkerSourceRefresh(root,root).refresh()["state"],
                                 "SKIPPED_NO_MANAGED_WORKER")

    def test_existing_release_wraps_worker_with_safe_rollback(self):
        s=(ROOT/"production/scene_candidate_release.py").read_text()
        ast.parse(s)
        i=s.index('worker_source_refresh.refresh()')
        self.assertGreater(i,s.index('atomic_link(CURRENT, target)'))
        self.assertLess(i,s.index('production_worker.commit()'))
        self.assertIn('worker_source_refresh.rollback()',s)
        self.assertIn('production_worker.rollback()',s)
        self.assertIn('if previous == target:',s)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "Disposable FFmpeg proof requires ffmpeg and ffprobe")
    def test_exact_worker_render_canary_runs_without_queue_or_projects(self):
        with tempfile.TemporaryDirectory(prefix="scene-real-worker-fixture-") as td:
            root=Path(td)
            (root/"vps_engine").symlink_to(PART2,target_is_directory=True)
            env=os.environ.copy()
            env["PYTHONPATH"] = str(root)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result=subprocess.run([sys.executable,"-c",refresh._SMOKE],
                                  cwd=root,env=env,capture_output=True,text=True,timeout=240)
            self.assertEqual(result.returncode,0,result.stderr[-3000:])
            markers=[x for x in result.stdout.splitlines()
                     if x.startswith("SCENE_LIVE_WORKER_RENDER_PROOF=")]
            self.assertEqual(len(markers),4,result.stdout[-3000:])
            self.assertTrue(all('"codec": "h264/aac"' in x for x in markers))
            full=[x for x in result.stdout.splitlines()
                  if x.startswith("SCENE_LIVE_EFFECT_MATRIX=")]
            self.assertEqual(len(full),1,result.stdout[-3000:])
            data=json.loads(full[0].split("=",1)[1])
            self.assertEqual(data["image_motions"],5)
            self.assertEqual(data["video_motions"],4)
            self.assertEqual(data["transitions"],14)
            self.assertEqual(data["luts"],23)
            self.assertGreaterEqual(data["colour_filters"],15)
            self.assertEqual(data["lut_plus_filter"],"PASS")
            self.assertEqual(data["whisper_karaoke"],"ASS_FFMPEG_PIXELS_PASS")
            self.assertFalse((root/"queue.sqlite3").exists())
            self.assertFalse((root/"project.json").exists())


if __name__ == "__main__":
    unittest.main()

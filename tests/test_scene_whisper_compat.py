"""Real faster-whisper/PyAV decoding plus idle-worker/rollback contracts."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from production import scene_whisper_compat as compat

ROOT=Path(__file__).resolve().parents[1]

class WhisperCompatibilityTests(unittest.TestCase):
    def test_unknown_active_and_queued_work_block_maintenance(self):
        for state in ('queued','preparing','running','rendering','ready_to_render','drive_uploading','unknown'):
            with self.subTest(state=state),self.assertRaises(RuntimeError):
                compat.idle_worker({'ok':True,'admission_only':False,'queue_counts':{state:1,'failed':1}})
        with self.assertRaises(RuntimeError): compat.idle_worker({'ok':True,'admission_only':False})
        compat.idle_worker({'ok':True,'admission_only':False,'queue_counts':{'failed':1,'completed':2}})

    def test_only_owned_project_recovery_routes_reach_existing_handlers(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('scene_recovery_policy',ROOT/'apps/scene-builder/part01/precutover_policy.py')
        policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
        job='a'*32
        for action in ('restart-stage','restart','delete'):
            path='/queue/'+job+'/'+action
            self.assertTrue(policy.mutation_allowed('POST',path,True))
            self.assertFalse(policy.mutation_allowed('POST',path,False))
            self.assertFalse(policy.mutation_allowed('DELETE',path,True))
            self.assertFalse(policy.mutation_allowed('POST',path+'/extra',True))
        # The new delete route is owner-checked and worker-guarded to cancelled jobs only.
        self.assertFalse(policy.mutation_allowed('POST','/queue/not-a-job/restart-stage',True))
        from tests.test_scene_user_roles import get_function,HTTPException
        for name in ('restart_stage_job','restart_job'):
            def deny(request, job_id): raise HTTPException(403,'not your project')
            with patch('builtins.print'):
                calls=[]
                fn=get_function(name,{'require_job_access':deny,'worker_request':lambda *a: calls.append(a)})
                with self.assertRaises(HTTPException): fn(None,job)
                self.assertEqual(calls,[])

    def test_initial_and_future_installers_pin_the_same_compatible_pair(self):
        from production.scene_production_worker import DEPENDENCIES
        self.assertIn('av=='+compat.AV_VERSION,DEPENDENCIES)
        self.assertIn('faster-whisper==1.2.0',DEPENDENCIES)
        text=(ROOT/'apps/scene-builder/part02/requirements.txt').read_text()
        self.assertIn('av=='+compat.AV_VERSION,text)
        self.assertIn('faster-whisper==1.2.0',text)

    def test_failed_package_change_restores_cached_previous_wheel_then_service(self):
        with tempfile.TemporaryDirectory() as temp:
            repair=compat.WhisperCompatibility(Path(temp)); repair.previous='19.0.1'
            repair.mutated=True; repair.stopped=True
            with patch.object(repair,'_run') as run,patch.object(repair,'_ready'):
                repair.rollback()
            self.assertIn('av==19.0.1',run.call_args_list[0].args[0])
            self.assertIn('--no-index',run.call_args_list[0].args[0])
            self.assertIn('--no-deps',run.call_args_list[0].args[0])
            self.assertEqual(run.call_args_list[1].args[0],['systemctl','start',compat.UNIT])
            self.assertFalse(repair.stopped)

    def test_active_projects_prevent_a_dependency_rollback_restart(self):
        repair=compat.WhisperCompatibility(Path('/tmp/unused-whisper-journal'))
        repair.mutated=True;repair.previous='19.0.1'
        with patch.object(compat,'health',return_value={'ok':True,'admission_only':False,'queue_counts':{'rendering':1}}),patch.object(repair,'_run') as run:
            with self.assertRaises(RuntimeError): repair.rollback()
            run.assert_not_called()

    def test_real_narration_smoke_does_not_restart_or_modify_jobs(self):
        source=(ROOT/'production/scene_whisper_compat.py').read_text()
        self.assertNotIn('UPDATE jobs',source)
        self.assertNotIn('DELETE FROM',source)
        self.assertNotIn('/restart',source)
        self.assertNotIn('/resume',source)
        self.assertIn('read_bytes()).hexdigest() == before',source)
        self.assertIn('caption_segments',compat.CAPTION_PROBE)
        self.assertIn('word_timestamps=True',compat.CAPTION_PROBE)

    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ('faster_whisper','av','numpy','requests')), 'real decoder dependencies installed in dedicated compatibility CI')
    def test_actual_stereo_wav_decode_matches_whisper_input_contract(self):
        completed=subprocess.run([sys.executable,'-c',compat.DECODE_PROBE],capture_output=True,text=True,timeout=45)
        self.assertEqual(completed.returncode,0,completed.stderr)
        evidence=json.loads(completed.stdout)
        self.assertEqual(evidence['av'],compat.AV_VERSION)
        self.assertEqual(evidence['faster_whisper'],'1.2.0')
        self.assertGreaterEqual(evidence['decoded_samples'],15000)

if __name__ == '__main__': unittest.main()

"""Production worker ownership, binding and rollback invariants, no VPS calls."""
import ast
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from production import scene_production_worker as worker

ROOT = Path(__file__).resolve().parents[1]

class ProductionWorkerContracts(unittest.TestCase):
    def test_binding_changes_only_worker_endpoint_and_private_token(self):
        token = 'a'*48
        old = '# retained\nSESSION_SECRET=keep\nWORKER_URL=http://127.0.0.1:1\nWORKER_TOKEN=old\nSCENE_PERMANENT_ROOT=/same\n'
        new = worker.bind_web(old,token)
        self.assertIn('SESSION_SECRET=keep\n',new)
        self.assertIn('SCENE_PERMANENT_ROOT=/same\n',new)
        self.assertIn('WORKER_URL='+worker.URL+'\n',new)
        self.assertEqual(new,worker.bind_web(new,token))
        with self.assertRaises(RuntimeError): worker.bind_web(old+'WORKER_URL=other\n',token)
        with self.assertRaises(RuntimeError): worker.bind_web(old,'bad')

    def test_separate_nonroot_unit_retains_admission_guard(self):
        unit = worker.unit_text(Path('/srv/fresh-website/releases/scene-production-worker/test'))
        for value in ('User=vps1scene','--host 127.0.0.1 --port 18081','ProtectSystem=strict',
                      'ReadWritePaths=/var/lib/fresh-website/scene-builder','--workers 1'):
            self.assertIn(value,unit)
        self.assertNotIn('scene-worker-admission',unit)
        self.assertIn('entry:app',unit)
        self.assertIn("if fresh:",worker.ENTRYPOINT)
        self.assertIn("worker._pause_pipeline('Production release admission')",worker.ENTRYPOINT)
        original = (ROOT/'apps/scene-builder/part02/api_server.py').read_text()
        self.assertIn('if WORKER_ADMISSION_ONLY:',original)
        self.assertIn('worker admission: mutations disabled',original)

    def test_rollback_stops_only_new_empty_service_and_retains_any_projects(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with sqlite3.connect(root/'queue.sqlite3') as conn:
                conn.execute('CREATE TABLE jobs(id TEXT)')
            manager = worker.ProductionWorker(root,root); manager.started = True
            with patch.object(worker,'STATE',root),patch.object(worker.subprocess,'run') as run:
                manager.rollback()
                self.assertEqual(run.call_args.args[0],['systemctl','disable','--now',worker.UNIT])
                with sqlite3.connect(root/'queue.sqlite3') as conn: conn.execute("INSERT INTO jobs VALUES('own-or-user-job')")
                run.reset_mock(); manager.rollback(); run.assert_not_called()
            with sqlite3.connect(root/'queue.sqlite3') as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM jobs').fetchone()[0],1)

    def test_unknown_live_endpoint_fails_before_any_service_mutation(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'web.env'; path.write_text('WORKER_URL=http://127.0.0.1:9999\n')
            with patch.object(worker,'WEB_ENV',path),patch.object(worker.os,'geteuid',return_value=0),patch.object(worker,'health',return_value={}),patch.object(worker.subprocess,'run') as commands:
                with self.assertRaisesRegex(RuntimeError,'unknown worker binding'):
                    worker.ProductionWorker(Path(temp),Path(temp)).ensure()
                commands.assert_not_called()

    def test_adapter_provisions_inside_existing_rollback_transaction(self):
        source = (ROOT/'production/scene_candidate_release.py').read_text()
        ast.parse(source)
        self.assertLess(source.index('production_worker.ensure()'),source.index('atomic_link(CURRENT, target)'))
        self.assertIn('production_worker.rollback()',source)
        self.assertIn('_atomic_scene_env(original_env)',source)
        self.assertIn('verify_backend()',source)

if __name__ == '__main__': unittest.main()

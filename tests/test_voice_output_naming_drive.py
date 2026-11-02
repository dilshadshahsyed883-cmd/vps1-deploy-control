"""Offline Voice Studio output naming and Drive receipt contracts (no real uploads)."""
import ast
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
K_API = ROOT / 'apps/voice-studio/part01/kokoro_api.py'
E_API = ROOT / 'apps/voice-studio/part01/edge_api.py'
K_WORKER = ROOT / 'apps/voice-studio/part02/kokoro_worker.py'
E_WORKER = ROOT / 'apps/voice-studio/part03/edge_worker.py'
UI = ROOT / 'apps/voice-studio/part01/templates/voice_studio.html'


def import_api(path, name):
    sys.path.insert(0, str(ROOT / 'apps/voice-studio/part01'))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def worker_functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in keep} == set(names)
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


@unittest.skipIf(importlib.util.find_spec('fastapi') is None,
                 'FastAPI test fixture runs in dedicated Voice Studio CI')
class VoiceOutputContract(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        sys.path.insert(0, str(ROOT / "apps/voice-studio/part01"))
        import voice_tenant
        self._identity_tokens = voice_tenant.set_identity("testvoice", False)
        self.addCleanup(lambda: voice_tenant.reset_identity(self._identity_tokens))
        self.mods = {}
        for engine, path in (('ko', K_API), ('ed', E_API)):
            mod = import_api(path, engine + '_output_test')
            mod.ROOT = self.tmp / engine
            mod.DATA = mod.ROOT / 'data'
            mod.DB = mod.DATA / ('kokoro_jobs.sqlite3' if engine == 'ko' else 'edge_jobs.sqlite3')
            mod.init_storage()
            self.mods[engine] = mod

    def test_custom_txt_unicode_and_fallback(self):
        for engine, mod in self.mods.items():
            with self.subTest(engine=engine):
                choose = mod.choose_output_name
                self.assertEqual(choose('My Documentary', 'Chapter_05.txt', 'Hello', 'JOB')[0], 'My_Documentary')
                self.assertEqual(choose('', 'Chapter_05.txt', 'Hello', 'JOB'), ('Chapter_05', 'source_txt'))
                self.assertEqual(choose('', '', 'Hello everyone', 'JOB'), ('He_JOB', 'text_fallback'))
                self.assertEqual(choose('', '', 'مرحبا دنیا', 'JOB')[0], 'مر_JOB')
                self.assertEqual(choose('', '', '你好世界', 'JOB')[0], '你好_JOB')
                self.assertEqual(choose('..\\../../secret', '', 'Hello', 'JOB')[0], 'secret')
                self.assertNotIn('/', choose('../../private', 'a.txt', 'abc', 'JOB')[0])

    def test_unique_jobs_and_persisted_names(self):
        for engine, mod in self.mods.items():
            if engine == 'ko':
                make = lambda name: mod.create_job('Hello everybody', 'text-input', 'en-us', 'af_heart', 1, 'batch', custom_name=name)
            else:
                make = lambda name: mod.create_job('Hello everybody', 'text-input', 'en-US-A', 'en-US', 0, 0, 0, 'batch', custom_name=name)
            id1, id2, id3 = make('My Documentary'), make('My Documentary'), make('')
            self.assertNotEqual(id1, id2)
            with (mod.connection() if engine == 'ko' else mod.db()) as db:
                names = db.execute('select job_id,output_basename,naming_method,input_file from jobs order by created_at,job_id').fetchall()
            self.assertEqual(len(names), 3)
            self.assertTrue(all(Path(r['input_file']).exists() for r in names))
            self.assertEqual(len({str(r['input_file']) for r in names}), 3)
            self.assertEqual(sum(r['naming_method'] == 'custom' for r in names), 2)
            self.assertEqual(sum(r['naming_method'] == 'text_fallback' for r in names), 1)

    def test_existing_database_idempotent_migration(self):
        for engine, mod in self.mods.items():
            original = mod.DB.read_bytes()
            mod.init_storage()
            self.assertTrue(mod.DB.is_file())
            with (mod.connection() if engine == 'ko' else mod.db()) as c:
                cols = {r[1] for r in c.execute('pragma table_info(jobs)')}
                self.assertTrue({'naming_method', 'drive_file_id', 'drive_url', 'drive_verified_at', 'drive_files_json'} <= cols)
            self.assertGreater(len(original), 0)

    def test_retry_only_delivery_no_regeneration(self):
        for engine, mod in self.mods.items():
            if engine == 'ko':
                jid = mod.create_job('Hello', 'script.txt', 'en-us', 'af_heart', 1, 'batch')
                audio_col = 'wav_file'
                audio_path = mod.DATA / 'jobs' / jid / 'script.wav'
                fn = mod.connection
            else:
                jid = mod.create_job('Hello', 'script.txt', 'en-US-A', 'en-US', 0, 0, 0, 'batch')
                audio_col = 'mp3_file'
                audio_path = mod.DATA / 'jobs' / jid / 'script.mp3'
                fn = mod.db
            audio_path.write_bytes(b'fake audio bytes for a delivery-only test')
            with fn() as con:
                con.execute('update jobs set status=?,delivery_status=?, ' + audio_col + '=? where job_id=?',
                            ('completed', 'retry_pending', str(audio_path), jid))
            result = mod.retry_delivery(jid)
            self.assertEqual(result['delivery_status'], 'retry_pending')
            with fn() as con:
                row = con.execute('select status,delivery_status,input_file from jobs where job_id=?', (jid,)).fetchone()
            self.assertEqual(row['status'], 'completed')
            self.assertTrue(Path(row['input_file']).exists())

    def test_download_routes_return_named_existing_audio_without_resynthesis(self):
        for engine, mod in self.mods.items():
            if engine == 'ko':
                jid=mod.create_job('Hello!', 'Chapter_05.txt', 'en-us', 'af_heart', 1, 'batch')
                key='wav_file'; suffix='.wav'; endpoint=lambda: mod.audio(jid); connection=mod.connection
            else:
                jid=mod.create_job('Hello!', 'Chapter_05.txt', 'en-US-A', 'en-US', 0, 0, 0, 'batch')
                key='mp3_file'; suffix='.mp3'; endpoint=lambda: mod.file(jid, 'mp3'); connection=mod.db
            output=mod.DATA / 'jobs' / jid / ('Chapter_05'+suffix)
            output.write_bytes(b'pretend generated media for response test')
            with connection() as db:
                db.execute('UPDATE jobs SET status=?, '+key+'=? WHERE job_id=?',
                           ('completed', str(output), jid))
            response=endpoint()
            self.assertEqual(response.filename, 'Chapter_05'+suffix)
            self.assertEqual(Path(response.path).read_bytes(), b'pretend generated media for response test')
            with connection() as db:
                stored=db.execute('SELECT output_basename FROM jobs WHERE job_id=?',(jid,)).fetchone()[0]
            self.assertEqual(stored, 'Chapter_05')

    def test_kokoro_legacy_queue_label_is_not_replaced(self):
        source=K_API.read_text(encoding='utf8')
        self.assertIn("out['filename']=d['output_basename']+'.txt'", source)
        self.assertIn("out['output_filename']=d['output_basename']+'.wav'", source)

    def test_verified_remote_receipts_and_negative_evidence(self):
        import re
        for engine, path in (('ko', K_WORKER), ('ed', E_WORKER)):
            local = self.tmp / (engine + '.wav')
            local.write_bytes(b'isolated test audio bytes')
            expected_id = 'abcdef1234567890'
            remote = 'gdrive:Omni Voice/Fake/' + local.name
            remote_data = {
                'ID': expected_id, 'Name': local.name, 'Size': local.stat().st_size,
                'Hashes': {'MD5': hashlib.md5(local.read_bytes()).hexdigest()}
            }
            calls = []
            def fake(*args):
                calls.append(args)
                if 'lsjson' in args:
                    return json.dumps(remote_data)
                return ''
            ns = worker_functions(path, {'verified_copy'}, {
                'json': json, 're': re, 'hashlib': hashlib, 'Path': Path,
                'run_rclone': fake, 'rclone_cmd': fake,
            })
            with self.subTest(engine=engine):
                receipt = ns['verified_copy'](local, remote)
                self.assertEqual(receipt['id'], expected_id)
                self.assertEqual(receipt['url'], 'https://drive.google.com/file/d/' + expected_id + '/view')
                self.assertTrue(any('--ignore-existing' in call for call in calls))
                self.assertTrue(any('--hash' in call for call in calls))
                remote_data['Size'] += 1
                with self.assertRaises(RuntimeError):
                    ns['verified_copy'](local, remote)
                remote_data['Size'] -= 1
                remote_data['ID'] = ''
                with self.assertRaises(RuntimeError):
                    ns['verified_copy'](local, remote)

    def test_restart_requeues_interrupted_upload(self):
        for engine, path in (('ko', K_WORKER), ('ed', E_WORKER)):
            mod = self.mods[engine]
            if engine == 'ko':
                jid = mod.create_job('Hello', 's.txt', 'en-us', 'af_heart', 1, 'batch')
                connection = mod.connection
            else:
                jid = mod.create_job('Hello', 's.txt', 'en-US-A', 'en-US', 0, 0, 0, 'batch')
                connection = mod.db
            with connection() as c:
                c.execute("update jobs set status='completed',delivery_status='transferring' where job_id=?", (jid,))
            ns = worker_functions(path, {'ensure_output_metadata'}, {'db': connection, 'RuntimeError': RuntimeError})
            ns['ensure_output_metadata']()
            with connection() as c:
                row = c.execute('select delivery_status,next_delivery_retry from jobs where job_id=?', (jid,)).fetchone()
            self.assertEqual(row['delivery_status'], 'retry_pending')
            self.assertEqual(row['next_delivery_retry'], 0)

    def test_missing_drive_credentials_leave_completed_audio_and_schedule_retry(self):
        import time
        for engine, path in (('ko', K_WORKER), ('ed', E_WORKER)):
            mod=self.mods[engine]
            if engine=='ko':
                jid=mod.create_job('Hello', 'Chapter.txt', 'en-us', 'af_heart', 1, 'batch')
                key='wav_file'; connection=mod.connection; name='deliver_once'
            else:
                jid=mod.create_job('Hello', 'Chapter.txt', 'en-US-A', 'en-US', 0, 0, 0, 'batch')
                key='mp3_file'; connection=mod.db; name='deliver'
            audio=mod.DATA/'jobs'/jid/('Chapter.wav' if engine=='ko' else 'Chapter.mp3')
            audio.write_bytes(b'completed local audio')
            with connection() as c:
                c.execute("update jobs set status='completed',delivery_status='pending',"+key+"=? where job_id=?",(str(audio),jid))
            ns=worker_functions(path, {name}, {
                'RCLONE':'','RCFILE':str(self.tmp/'missing.conf'),'RCCONF':self.tmp/'missing.conf',
                'Path':Path,'time':time,'db':connection
            })
            ns[name]()
            with connection() as c:
                row=c.execute("select status,delivery_status,delivery_error,next_delivery_retry from jobs where job_id=?",(jid,)).fetchone()
            self.assertEqual(row['status'],'completed')
            self.assertEqual(row['delivery_status'],'retry_pending')
            self.assertEqual(row['delivery_error'],'drive_configuration_missing')
            self.assertGreater(row['next_delivery_retry'],time.time())
            self.assertTrue(audio.is_file())

    def test_worker_threads_and_ui_binding(self):
        html = UI.read_text(encoding='utf8')
        self.assertIn('id="vskOutputName"', html)
        self.assertIn('id="vseOutputName"', html)
        self.assertIn('Google Drive: Open uploaded file', html)
        self.assertIn('Google Drive ', html)
        self.assertIn("'/delivery/retry'", html.replace("+'/delivery/retry'", "'/delivery/retry'"))
        for path in (K_WORKER, E_WORKER):
            source = path.read_text(encoding='utf8')
            self.assertIn('threading.Thread(target=delivery_loop,daemon=True).start()', source)
            self.assertIn('drive_file_id=', source)
            self.assertIn('drive_verified_at=', source)


if __name__ == '__main__':
    unittest.main()

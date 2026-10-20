"""Isolated 403 rate-limit retry contracts for Kokoro/Edge Drive workers.
No real Google Drive files, credentials or synthesis.
"""
import ast
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
WORKERS=(
    ("kokoro",ROOT/"apps/voice-studio/part02/kokoro_worker.py","run_rclone"),
    ("edge",ROOT/"apps/voice-studio/part03/edge_worker.py","rclone_cmd"),
)

def isolated_runner(source, function_name, subprocess_stub, timer):
    tree=ast.parse(source.read_text(encoding="utf-8"))
    function=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and
                  x.name==function_name)
    values={
        "RCLONE":"/usr/bin/rclone",
        "RCFILE":"/tmp/qa-rclone.conf",
        "RCCONF":SimpleNamespace(is_file=lambda:True,__str__=lambda s:"/tmp/qa-rclone.conf"),
        "subprocess":subprocess_stub,
        "time":timer,
    }
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),"exec"),values)
    return values[function_name]

class DriveRateLimitWorkerTests(unittest.TestCase):
    def test_rate_limit_is_retried_without_exposing_error(self):
        for engine,source,func in WORKERS:
            with self.subTest(engine=engine):
                attempts=[]
                def execute(args,**kwargs):
                    attempts.append(args)
                    if len(attempts)==1:
                        return SimpleNamespace(returncode=1,stdout="",stderr="Google API Error 403 rateLimitExceeded token=NEVER_PRINT")
                    return SimpleNamespace(returncode=0,stdout='{"ID":"abcdefgh1234"}',stderr="")
                timer=SimpleNamespace(sleep=mock.Mock())
                runner=isolated_runner(source,func,
                    SimpleNamespace(run=execute,PIPE=subprocess.PIPE),timer)
                self.assertEqual(json.loads(runner("lsjson","--stat","gdrive:qa"))["ID"],
                                 "abcdefgh1234")
                self.assertEqual(len(attempts),2)
                timer.sleep.assert_called_once_with(10)

    def test_permanent_permission_error_is_not_retried(self):
        for engine,source,func in WORKERS:
            with self.subTest(engine=engine):
                run=mock.Mock(return_value=SimpleNamespace(
                    returncode=1,stdout="",stderr="403 insufficientFilePermissions token=SECRET"))
                timer=SimpleNamespace(sleep=mock.Mock())
                runner=isolated_runner(source,func,
                    SimpleNamespace(run=run,PIPE=subprocess.PIPE),timer)
                with self.assertRaises(RuntimeError) as exc:
                    runner("lsjson","--stat","gdrive:qa")
                self.assertNotIn("SECRET",str(exc.exception))
                self.assertEqual(run.call_count,1)
                timer.sleep.assert_not_called()

    def test_storage_quota_exceeded_is_not_transient_api_throttle(self):
        for engine,source,func in WORKERS:
            with self.subTest(engine=engine):
                run=mock.Mock(return_value=SimpleNamespace(
                    returncode=1,stdout="",stderr="403 storageQuotaExceeded"))
                timer=SimpleNamespace(sleep=mock.Mock())
                runner=isolated_runner(source,func,
                    SimpleNamespace(run=run,PIPE=subprocess.PIPE),timer)
                with self.assertRaises(RuntimeError):
                    runner("copyto","input.txt","gdrive:qa")
                self.assertEqual(run.call_count,1)
                timer.sleep.assert_not_called()

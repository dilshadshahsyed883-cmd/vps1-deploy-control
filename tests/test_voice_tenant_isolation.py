"""Offline two-user isolation tests. Never connect to a live TTS or Drive service."""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1] / "apps/voice-studio/part01"
sys.path.insert(0, str(ROOT))

try:
    from fastapi import HTTPException
    import voice_tenant
except ModuleNotFoundError:
    HTTPException = None
    voice_tenant = None


@contextmanager
def as_user(username: str):
    tokens = voice_tenant.set_identity(username, False)
    try:
        yield
    finally:
        voice_tenant.reset_identity(tokens)


def load(name: str):
    spec = importlib.util.spec_from_file_location("voice_tenant_test_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(voice_tenant is None, "Voice FastAPI requirements not installed")
class TenantBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.kokoro = load("kokoro_api")
        self.edge = load("edge_api")
        self.clone = load("voice_clone_api")
        for name, module in (("kokoro", self.kokoro), ("edge", self.edge)):
            module.ROOT = root / name
            module.DATA = module.ROOT / "data"
            module.DB = module.DATA / ("kokoro_jobs.sqlite3" if name == "kokoro" else "edge_jobs.sqlite3")
            module.init_storage()
        self.clone.ROOT = root / "clone"
        self.clone.DB_PATH = self.clone.ROOT / "clone.db"
        self.clone.SAMPLES_DIR = self.clone.ROOT / "samples"
        self.clone.AUDIO_DIR = self.clone.ROOT / "audio"
        self.clone.TEXT_DIR = self.clone.ROOT / "text"
        self.clone.TMP_DIR = self.clone.ROOT / "tmp"
        self.clone.init_storage()

    def assert_404(self, fn):
        with self.assertRaises(HTTPException) as ctx:
            fn()
        self.assertEqual(ctx.exception.status_code, 404)

    def test_kokoro_and_edge_job_lists_actions_and_downloads_are_separate(self):
        with as_user("alpha"):
            a_k = self.kokoro.create_job("Alpha narration", "script.txt", "en-us", "af_heart", 1, "batch")
            a_e = self.edge.create_job("Alpha narration", "script.txt", "en-US-JennyNeural", "en-US", 0, 0, 0, "batch")
        with as_user("beta"):
            b_k = self.kokoro.create_job("Beta narration", "script.txt", "en-us", "af_heart", 1, "batch")
            b_e = self.edge.create_job("Beta narration", "script.txt", "en-US-JennyNeural", "en-US", 0, 0, 0, "batch")
            self.assertEqual([x["job_id"] for x in self.kokoro.jobs()["jobs"]], [b_k])
            self.assertEqual([x["job_id"] for x in self.edge.jobs()["jobs"]], [b_e])
            self.assert_404(lambda: self.kokoro.control(a_k, "cancel"))
            self.assert_404(lambda: self.edge.control(a_e, "cancel"))
            self.assert_404(lambda: self.kokoro.audio(a_k))
            self.assert_404(lambda: self.edge.file(a_e, "txt"))
            self.assert_404(lambda: self.kokoro.retry_delivery(a_k))
            self.assert_404(lambda: self.edge.retry_delivery(a_e))
        with as_user("alpha"):
            self.assertEqual([x["job_id"] for x in self.kokoro.jobs()["jobs"]], [a_k])
            self.assertEqual([x["job_id"] for x in self.edge.jobs()["jobs"]], [a_e])
        with self.edge.db() as con:
            con.execute("UPDATE jobs SET owner_username='' WHERE job_id=?", (a_e,))
        with as_user("alpha"):
            self.assertEqual(self.edge.jobs()["jobs"], [], "legacy unassigned jobs are preserved but hidden")

    def test_omnivoice_profiles_categories_jobs_and_previews_are_owned(self):
        clone = self.clone
        for username in ("alpha", "beta"):
            with as_user(username):
                self.assertEqual(clone.list_categories()["categories"][0]["name"], "General")
                self.assertEqual(clone.create_category(clone.CategoryCreate(name="Series"))["category"], "Series")
                profile_id = "CLV-" + username.upper()
                job_id = "CLJ-" + username.upper()
                with clone.db() as con:
                    con.execute(
                        "INSERT INTO profiles(profile_id,name,source_filename,sample_file,sample_sha256,duration_seconds,created_at,owner_username) VALUES(?,?,?,?,?,?,?,?)",
                        (profile_id,"voice","sample.wav","sample.wav","abc",3.0,"2026-11-02",username),
                    )
                    con.execute(
                        "INSERT INTO jobs(job_id,profile_id,text,created_at,owner_username,batch_id) VALUES(?,?,?,?,?,?)",
                        (job_id,profile_id,"sample","2026-11-02",username,"batch-" + username),
                    )
                self.assertEqual([x["profile_id"] for x in clone.list_profiles()["profiles"]], [profile_id])
                self.assertEqual([x["job_id"] for x in clone.list_clone_jobs(limit=0)["jobs"]], [job_id])
        with as_user("beta"):
            self.assert_404(lambda: clone.delete_clone_job("CLJ-ALPHA"))
            with clone.db() as con:
                self.assert_404(lambda: clone._batch_rows(con, "batch-alpha"))
            self.assertEqual(clone._validate_drive_folder("default"), "vs_beta__default")
            with self.assertRaises(HTTPException) as ctx:
                clone._validate_drive_folder("vs_alpha__default")
            self.assertEqual(ctx.exception.status_code, 403)
        preview_id = "f" * 32
        with clone.db() as con:
            con.execute(
                "INSERT INTO voice_design_preview_owners(preview_id,owner_username) VALUES(?,?)",
                (preview_id, "alpha"),
            )
        with as_user("beta"):
            self.assert_404(lambda: clone._require_design_preview_owner(preview_id))
        with as_user("alpha"):
            self.assertEqual(clone._require_design_preview_owner(preview_id), preview_id)

    def test_no_identity_is_denied(self):
        with self.assertRaises(HTTPException) as ctx:
            voice_tenant.owner()
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()

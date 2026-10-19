"""Offline Auto Stream OAuth key bootstrap; no production or Google access."""
import base64
from pathlib import Path
import sqlite3
import tempfile
import unittest
from production.auto_stream_oauth_env import ensure_key, valid_key

class AutoStreamVaultKeyTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.env=self.root/"production-short-stream.env"
        self.env.write_text("SHORT_STREAM_DATA=/tmp/short-stream\n")
        self.env.chmod(0o600)
        self.state=self.root/"state"

    def test_single_stable_vault_key(self):
        self.assertEqual(ensure_key(self.env,self.state),"NEW")
        after=self.env.read_text()
        self.assertEqual(ensure_key(self.env,self.state),"PRESERVED")
        self.assertEqual(after,self.env.read_text())
        key=next(x.partition("=")[2] for x in after.splitlines()
                 if x.startswith("AUTO_STREAM_FERNET_KEY="))
        self.assertTrue(valid_key(key))
        self.assertEqual(len(base64.urlsafe_b64decode(key)),32)

    def test_client_values_preserved(self):
        self.env.write_text("AUTO_STREAM_GOOGLE_CLIENT_ID=unrelated-client\n"
                            "AUTO_STREAM_GOOGLE_CLIENT_SECRET=sample-not-real\n")
        ensure_key(self.env,self.state)
        self.assertIn("AUTO_STREAM_GOOGLE_CLIENT_SECRET=sample-not-real",
                      self.env.read_text())

    def test_key_never_rotated_even_if_invalid(self):
        self.env.write_text("AUTO_STREAM_FERNET_KEY=invalid\n")
        with self.assertRaisesRegex(RuntimeError,"refusing rotation"):
            ensure_key(self.env,self.state)
        self.assertEqual(self.env.read_text(),"AUTO_STREAM_FERNET_KEY=invalid\n")

    def test_existing_encrypted_oauth_record_blocks_generation(self):
        db=self.state/"auto-stream-engine"/"integrations.sqlite3"
        db.parent.mkdir(parents=True)
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE channels (token BLOB)")
            con.execute("INSERT INTO channels VALUES (?)",(b"encrypted",))
        with self.assertRaisesRegex(RuntimeError,"Encrypted Auto Stream records"):
            ensure_key(self.env,self.state)
        self.assertNotIn("AUTO_STREAM_FERNET_KEY",self.env.read_text())

    def test_existing_broadcast_secret_blocks_generation(self):
        db=self.state/"auto-stream-engine"/"integrations.sqlite3"
        db.parent.mkdir(parents=True)
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE live_control (secret BLOB)")
            con.execute("INSERT INTO live_control VALUES (?)",(b"encrypted",))
        with self.assertRaises(RuntimeError):
            ensure_key(self.env,self.state)

    def test_unsafe_env_refused(self):
        link=self.root/"alias.env"
        link.symlink_to(self.env)
        with self.assertRaises(RuntimeError):
            ensure_key(link,self.state)
        self.env.chmod(0o644)
        with self.assertRaises(RuntimeError):
            ensure_key(self.env,self.state)

if __name__=="__main__":
    unittest.main()

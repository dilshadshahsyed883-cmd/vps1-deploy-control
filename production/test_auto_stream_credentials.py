"""Auto Stream OAuth web setup regression; never use real Google keys."""
import os
from pathlib import Path
import sys
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"apps/short-stream/part01"))
import auto_stream_client_config as m
import auto_stream_integrations as integrations

ID="1234-testing-web-app.apps.googleusercontent.com"
SECRET="GOCSPX-unit-test-secret-not-real"
HOST="app.scenebuilder.online"
ADMIN={"host":HOST,"x-auth-user":"admin","x-is-admin":"1"}
WRITE={**ADMIN,"origin":"https://app.scenebuilder.online",
       "x-auto-stream-config-action":"1"}


class OAuthClientWebSetupTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.secret=Fernet.generate_key().decode()
        context=patch.dict(os.environ,{"AUTO_STREAM_FERNET_KEY":self.secret,
          "AUTO_STREAM_GOOGLE_CLIENT_ID":"",
          "AUTO_STREAM_GOOGLE_CLIENT_SECRET":"",
          "AUTO_STREAM_GOOGLE_REDIRECT_URI":""})
        context.start()
        self.addCleanup(context.stop)
        self.store=m.OAuthClientStore(self.root)
        p=patch.object(m,"store",return_value=self.store)
        p.start()
        self.addCleanup(p.stop)
        app=FastAPI()
        app.include_router(m.router)
        self.client=TestClient(app)
        self.addCleanup(self.client.close)

    def test_non_admin_cannot_read_or_write_secret(self):
        for headers in ({}, {"x-auth-user":"reader","x-is-admin":"0"},
                        {"x-is-admin":"1"}):
            self.assertEqual(self.client.get("/engine/oauth/client",headers=headers).status_code,403)
            self.assertEqual(self.client.post("/engine/oauth/client",
              json={"client_id":ID,"client_secret":SECRET},headers=headers).status_code,403)
        self.assertFalse(self.store.path.exists())

    def test_admin_origin_header_required(self):
        data={"client_id":ID,"client_secret":SECRET}
        for headers in (ADMIN,{**WRITE,"origin":"https://evil.example"},
                        {**WRITE,"x-auto-stream-config-action":"0"},
                        {**WRITE,"host":"untrusted.example"}):
            self.assertEqual(self.client.post("/engine/oauth/client",
                             json=data,headers=headers).status_code,403)
        self.assertFalse(self.store.path.exists())

    def test_admin_save_encrypts_never_echoes_credentials(self):
        res=self.client.post("/engine/oauth/client",
            json={"client_id":ID,"client_secret":SECRET},headers=WRITE)
        self.assertEqual(res.status_code,200,res.text)
        self.assertTrue(res.json()["configured"])
        self.assertNotIn(SECRET,res.text)
        self.assertTrue(self.store.path.is_file())
        self.assertEqual(self.store.path.stat().st_mode & 0o777,0o600)
        self.assertNotIn(SECRET,self.store.path.read_bytes().decode("latin1"))
        read=self.client.get("/engine/oauth/client",headers=ADMIN)
        self.assertEqual(read.status_code,200)
        self.assertNotIn(SECRET,read.text)
        self.assertEqual(read.headers["cache-control"],"no-store")
        self.assertEqual(m.OAuthClientStore(self.root).read_client(),
                         (ID,SECRET,m.REDIRECT))
        with patch.object(m,"store",return_value=self.store), \
             patch("auto_stream_engine.ENGINE_ROOT",self.root):
            self.assertEqual(integrations._configuration(),
                             (ID,SECRET,m.REDIRECT))

    def test_clear_saved_client_and_never_rotate_key(self):
        self.store.save(ID,SECRET)
        res=self.client.request("DELETE","/engine/oauth/client",headers=WRITE)
        self.assertEqual(res.status_code,200)
        self.assertEqual(res.json()["configured"],False)
        self.assertFalse(self.store.path.exists())
        self.assertEqual(os.environ["AUTO_STREAM_FERNET_KEY"],self.secret)

    def test_disallow_changes_while_authorized_channel_exists(self):
        db=self.root/"integrations.sqlite3"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE channels(authorized INTEGER, token BLOB)")
            con.execute("INSERT INTO channels VALUES(1,?)",(b"encrypted-grant",))
        r=self.client.post("/engine/oauth/client",
           json={"client_id":ID,"client_secret":SECRET},headers=WRITE)
        self.assertEqual(r.status_code,409)
        self.assertFalse(self.store.path.exists())

    def test_reject_malformed_secret_without_leaking_it(self):
        for bad in ("", "secret\nprivate","a"*600):
            res=self.client.post("/engine/oauth/client",
                json={"client_id":ID,"client_secret":bad},headers=WRITE)
            self.assertEqual(res.status_code,409)
            self.assertNotIn(bad if bad else "GOCSPX",res.text)

    def test_legacy_env_compatibility(self):
        with patch.dict(os.environ,{
          "AUTO_STREAM_GOOGLE_CLIENT_ID":ID,
          "AUTO_STREAM_GOOGLE_CLIENT_SECRET":SECRET,
          "AUTO_STREAM_GOOGLE_REDIRECT_URI":m.REDIRECT}):
            self.assertEqual(integrations._configuration(),(ID,SECRET,m.REDIRECT))


if __name__=="__main__":
    unittest.main()

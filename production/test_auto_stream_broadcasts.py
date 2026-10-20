"""V3 YouTube lifecycle tests: all Google calls mocked; never publish."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"apps/short-stream/part01"))
import auto_stream_integrations as g
import auto_stream_broadcasts as live
from auto_stream_engine import StreamEngine

CID="UC"+"x"*22


class LiveBroadcastV3Tests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        p=Path(t.name)
        media=p/"media"
        media.mkdir()
        (media/"video.mp4").write_bytes(b"fake media")
        self.engine=StreamEngine(p/"independent",media)
        self.acct=g.AutoStreamAccounts(p/"independent")
        self.acct.import_metadata(CID,"Private Test Channel")
        self.env=patch.dict(os.environ,{
            "AUTO_STREAM_GOOGLE_CLIENT_ID":"unit-test.apps.googleusercontent.com",
            "AUTO_STREAM_GOOGLE_CLIENT_SECRET":"not-real",
            "AUTO_STREAM_GOOGLE_REDIRECT_URI":
                "https://app.scenebuilder.online/short-stream/engine/oauth/callback",
            "AUTO_STREAM_FERNET_KEY":Fernet.generate_key().decode(),
        },clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        with self.acct.connect() as c:
            c.execute("UPDATE channels SET authorized=1,token=?,scopes=? WHERE id=?",
                      (g._cipher().encrypt(b"dummy_refresh"),g.YOUTUBE_SCOPE,CID))
        scheduled=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        self.job=self.engine.create(dict(channel_id=CID,media_path="video.mp4",
                     title="Private Stage",scheduled_at=scheduled,
                     idempotency_key="v3-mock-test-0001"))
        self.engine.prepare(self.job["id"])
        self.manager=live.BroadcastController(engine=self.engine,acct=self.acct)

    @staticmethod
    def fake_google(url,**kwargs):
        from urllib.parse import urlparse,parse_qs
        ep=urlparse(url).path
        params=parse_qs(urlparse(url).query)
        if ep.endswith("/channels"):
            return {"items":[{"id":CID}]}
        if ep.endswith("/liveBroadcasts") and kwargs.get("method")=="POST":
            data=kwargs["payload"]
            assert data["status"]["privacyStatus"]=="private"
            assert data["contentDetails"]["enableAutoStart"] is False
            return {"id":"broadcast123"}
        if ep.endswith("/liveStreams") and kwargs.get("method")=="POST":
            return {"id":"stream456","cdn":{"ingestionInfo":{
                "streamName":"key_abcdefghij",
                "rtmpsIngestionAddress":"rtmps://a.rtmps.youtube.com/live2"}}}
        if ep.endswith("/liveBroadcasts/bind"):
            return {"contentDetails":{"boundStreamId":"stream456"}}
        if ep.endswith("/liveBroadcasts"):
            return {"items":[{"id":"broadcast123","status":{
                "lifeCycleStatus":"ready","privacyStatus":"private"},
                "contentDetails":{"boundStreamId":"stream456"}}]}
        if ep.endswith("/liveStreams"):
            return {"items":[{"id":"stream456","status":{"streamStatus":"inactive"}}]}
        if ep.endswith("/liveBroadcasts/transition"):
            return {"status":{"lifeCycleStatus":params["broadcastStatus"][0]}}
        raise AssertionError(ep)

    def test_default_no_remote_writes_or_streams(self):
        with patch.object(g,"_http_json",side_effect=AssertionError("network not allowed")):
            with self.assertRaises(PermissionError):
                self.manager.provision(self.job["id"])
            with self.assertRaises(PermissionError):
                self.manager.start_ingest(self.job["id"])
            with self.assertRaises(PermissionError):
                self.manager.transition(self.job["id"],"live")
        self.assertIsNone(self.manager.get(self.job["id"]))
        self.assertFalse(self.manager.integration_health()["go_live_enabled"])

    def test_private_broadcast_bind_encrypted_credentials_and_idempotence(self):
        with patch.dict(os.environ,{"AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED":"1"}),\
             patch.object(self.acct,"_access",return_value="mock_access"),\
             patch.object(g,"_http_json",side_effect=self.fake_google) as remote:
            b=self.manager.provision(self.job["id"])
        self.assertEqual(b["state"],"BOUND")
        self.assertTrue(b["secret_available"])
        self.assertEqual(remote.call_count,4)
        with self.acct.connect() as con:
            raw=con.execute("SELECT secret FROM live_control").fetchone()[0]
        self.assertNotIn(b"key_abcdefghij",raw)
        self.assertNotIn("key_abcdefghij",str(self.manager.get(self.job["id"])))
        with patch.dict(os.environ,{"AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED":"1"}):
            with self.assertRaisesRegex(ValueError,"already"):
                self.manager.provision(self.job["id"])

    def test_reject_inactive_ingest_even_if_transition_enabled(self):
        with self.acct.connect() as con:
            con.execute("""INSERT INTO live_control(job_id,channel_id,broadcast_id,
               stream_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
               (self.job["id"],CID,"broadcast123","stream456","BOUND",
                g.now(),g.now()))
        with patch.dict(os.environ,{"AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED":"1",
                                     "AUTO_STREAM_LIVE_TRANSITIONS_ENABLED":"1",
                                     "AUTO_STREAM_GO_LIVE_ENABLED":"1"}),\
             patch.object(self.acct,"_access",return_value="mock_access"),\
             patch.object(g,"_http_json",side_effect=self.fake_google) as remote:
            with self.assertRaisesRegex(ValueError,"not active"):
                self.manager.transition(self.job["id"],"live")
            self.assertFalse(any("/transition?" in a.args[0] for a in remote.call_args_list))

    def test_no_live_without_extra_owner_control(self):
        with patch.dict(os.environ,{"AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED":"1",
                                     "AUTO_STREAM_LIVE_TRANSITIONS_ENABLED":"1"}):
            with self.assertRaisesRegex(PermissionError,"GO_LIVE"):
                self.manager.transition(self.job["id"],"live")

    def test_explicit_live_applies_selected_privacy_and_records_remote_truth(self):
        with self.acct.connect() as con:
            con.execute("INSERT INTO live_control(job_id,channel_id,broadcast_id,stream_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(self.job['id'],CID,'broadcast123','stream456','ENCODER_STARTED',g.now(),g.now()))
        with self.engine._connect() as con:con.execute("UPDATE jobs SET status='INGESTING',privacy='unlisted' WHERE id=?",(self.job['id'],))
        def provider(url,**kwargs):
            if '/liveStreams?' in url:return {'items':[{'id':'stream456','status':{'streamStatus':'active','healthStatus':{'status':'good'}}}]}
            if kwargs.get('method')=='PUT':
                self.assertEqual(kwargs['payload']['status']['privacyStatus'],'unlisted')
                return {'status':{'privacyStatus':'unlisted'}}
            return self.fake_google(url,**kwargs)
        with patch.dict(os.environ,{'AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED':'1','AUTO_STREAM_LIVE_TRANSITIONS_ENABLED':'1','AUTO_STREAM_GO_LIVE_ENABLED':'1'}),patch.object(self.acct,'_access',return_value='fixture'),patch.object(g,'_http_json',side_effect=provider):
            result=self.manager.transition(self.job['id'],'live')
        self.assertTrue(result['youtube_verified_live'])
        self.assertEqual(self.engine.get(self.job['id'])['display_state'],'LIVE')

    def test_reject_foreign_ingest_hosts(self):
        for url in ["rtmp://a.rtmp.youtube.com/live2",
                    "rtmps://example.org/live2",
                    "rtmps://a.rtmps.youtube.com/live2?x=1",
                    "rtmps://a.rtmps.youtube.com.attacker.net/live2"]:
            with self.assertRaises(ValueError):
                live._rtmps(url)

    def test_partial_failure_requires_reconciliation(self):
        def fail_after_broadcast(url,**kw):
            if "/liveStreams?" in url:
                raise ValueError("Google API refused operation")
            return self.fake_google(url,**kw)
        with patch.dict(os.environ,{"AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED":"1"}),\
             patch.object(self.acct,"_access",return_value="mock_access"),\
             patch.object(g,"_http_json",side_effect=fail_after_broadcast):
            with self.assertRaises(ValueError):
                self.manager.provision(self.job["id"])
        self.assertEqual(self.manager.get(self.job["id"])["state"],"NEEDS_RECONCILIATION")
        with patch.dict(os.environ,{"AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED":"1"}):
            with self.assertRaises(ValueError):
                self.manager.provision(self.job["id"])

    def test_ingest_returns_not_verified_and_uses_internal_credentials(self):
        with self.acct.connect() as con:
            secret=g._cipher().encrypt(json.dumps({"url":"rtmps://a.rtmps.youtube.com/live2",
                                            "key":"key_abcdefghij"}).encode())
            con.execute("""INSERT INTO live_control(job_id,channel_id,broadcast_id,
               stream_id,secret,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)""",
               (self.job["id"],CID,"broadcast123","stream456",secret,"BOUND",g.now(),g.now()))
        with patch.dict(os.environ,{"AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED":"1",
                                     "AUTO_STREAM_PUBLISH_ENABLED":"1"}),\
             patch.object(self.acct,"_access",return_value="mock_access"),\
             patch.object(g,"_http_json",side_effect=self.fake_google),\
             patch.object(self.engine,"start",return_value={"status":"INGESTING"}) as start:
            result=self.manager.start_ingest(self.job["id"])
        self.assertFalse(result["youtube_verified_live"])
        self.assertEqual(start.call_args.kwargs["internal_ingest"][1],"key_abcdefghij")
        self.assertNotIn("key_abcdefghij",str(result))

if __name__=="__main__":
    unittest.main()

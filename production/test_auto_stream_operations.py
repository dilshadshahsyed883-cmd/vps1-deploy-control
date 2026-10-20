"""Supervisor, inventory, Sheets and app-owned cleanup integration acceptance."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'apps/short-stream/part01'))
import auto_stream_engine as e
import auto_stream_integrations as g
import auto_stream_operations as ops
import auto_stream_compositor as compose
import auto_stream_broadcasts as broadcasts
CID='UC'+'C'*22
HEADERS={'host':'app.scenebuilder.online','origin':'https://app.scenebuilder.online','x-auth-user':'test-admin','x-is-admin':'1','x-auto-stream-config-action':'1'}

class OperationalTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.media=self.root/'media';self.media.mkdir();(self.media/'video.mp4').write_bytes(b'media-fixture')
        self.engine=e.StreamEngine(self.root/'engine',self.media);self.acct=g.AutoStreamAccounts(self.root/'engine');self.acct.import_metadata(CID,'Channel')
        for target,name,value in ((e,'service',self.engine),(g,'accounts',self.acct)):
            ctx=patch.object(target,name,return_value=value);ctx.start();self.addCleanup(ctx.stop)
        self.controller=Mock();self.controller.get.return_value=None
        ctx=patch.object(broadcasts,'controller',return_value=self.controller);ctx.start();self.addCleanup(ctx.stop)
        app=FastAPI();app.include_router(e.router);app.include_router(g.router)
        self.client=TestClient(app);self.addCleanup(self.client.close)
        self.payload={'channel_id':CID,'media_path':'video.mp4','title':'Test','idempotency_key':'operational00001'}
    def test_edit_schedule_history_cancel_delete(self):
        job=self.engine.create(self.payload);jid=job['id']
        r=self.client.patch('/engine/jobs/'+jid,json={'title':'Edited','scheduled_at':'2026-11-03T10:00:00+05:00','profile':'1080p'})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['status'],'DRAFT')
        self.engine.prepare(jid);self.assertEqual(self.engine.get(jid)['status'],'SCHEDULED')
        logs=self.client.get('/engine/jobs/'+jid+'/history').json()['history']
        self.assertIn('SCHEDULED',[x['state'] for x in logs])
        self.assertEqual(self.client.delete('/engine/jobs/'+jid).status_code,409)
        self.client.post('/engine/jobs/'+jid+'/cancel')
        self.assertEqual(self.client.delete('/engine/jobs/'+jid).status_code,200)
        self.assertTrue((self.media/'video.mp4').exists())
    def test_channel_bulk_metadata_and_active_job_guard(self):
        response=self.client.post('/engine/channels/manage',json={'ids':[CID],'action':'edit','values':{'account_label':'Brand owner','brand_account':'yes','group_name':'News'}})
        self.assertEqual(response.status_code,200,response.text)
        row=self.acct.channel_catalog()[0];self.assertEqual(row['group_name'],'News');self.assertEqual(row['brand_account'],'yes')
        job=self.engine.create(self.payload);self.engine.prepare(job['id'])
        r=self.client.post('/engine/channels/manage',json={'ids':[CID],'action':'deactivate'})
        self.assertEqual(r.status_code,422)
        self.engine.stop(job['id']);self.assertEqual(self.client.post('/engine/channels/manage',json={'ids':[CID],'action':'deactivate'}).status_code,200)
    def test_connection_identity_mismatch_updates_health(self):
        with patch.object(self.acct,'_access',return_value='runtime-test'),patch.object(g,'_http_json',return_value={'items':[{'id':'UC'+'D'*22}]}):
            with self.assertRaises(ValueError):self.acct.test_channel(CID)
        self.assertEqual(self.acct.channel_catalog()[0]['connection_state'],'NEEDS_RECONNECT')
    def test_inventory_sync_never_imports_credentials_or_starts_jobs(self):
        with self.acct.connect() as con:con.execute('UPDATE channels SET authorized=1 WHERE id=?',(CID,))
        source=self.acct.add_sheet(CID,'x'*24,'Sheet1!A1:Z200')
        rows={'values':[['Channel ID','Channel Name','Gmail','Group','Password','Token'],['UC'+'D'*22,'Brand News','owner label','News','must-not-copy','must-not-copy']]}
        with patch.object(self.acct,'_access',return_value='test'),patch.object(g,'_http_json',return_value=rows):
            preview=self.acct.sync_sheet(source['id'],True,self.engine);self.assertEqual(preview['mode'],'channel_inventory')
            self.assertEqual(len(self.acct.channel_catalog()),1)
            result=self.acct.sync_sheet(source['id'],False,self.engine)
        self.assertEqual(result['inventory_updated'],1);self.assertEqual(len(self.engine.list_jobs()),0)
        row=next(c for c in self.acct.channel_catalog() if c['id']!=CID)
        self.assertFalse(row['authorized']);self.assertNotIn('must-not-copy',str(row));self.assertNotIn('must-not-copy',str(preview))
    def test_schedule_aliases_mapping_and_duplicate_skip(self):
        with self.acct.connect() as con:con.execute('UPDATE channels SET authorized=1 WHERE id=?',(CID,))
        source=self.acct.add_sheet(CID,'x'*24,'Sheet1!A1:Z200')
        rows={'values':[['YouTube Channel ID','Video Path','Stream Title','Scheduled Start','Loops'],[CID,'video.mp4','Repeat safe','2026-12-01T10:00:00+05:00','2']]}
        with patch.object(self.acct,'_access',return_value='test'),patch.object(g,'_http_json',return_value=rows):
            first=self.acct.sync_sheet(source['id'],False,self.engine);second=self.acct.sync_sheet(source['id'],False,self.engine)
        self.assertEqual(first['created'],1);self.assertEqual(second['created'],0);self.assertEqual(second['skipped_duplicates'],1)
        self.assertEqual(self.engine.list_jobs()[0]['status'],'DRAFT')
    def test_admin_settings_start_no_stream_and_reject_nonadmin(self):
        self.assertEqual(self.client.put('/engine/settings',json={}).status_code,403)
        with patch.dict(os.environ,{},clear=True):
            flags={key:False for key in ops.FLAGS};flags['AUTO_STREAM_PUBLISH_ENABLED']=True
            r=self.client.put('/engine/settings',headers=HEADERS,json=flags)
            self.assertEqual(r.status_code,200,r.text);self.assertTrue(r.json()['flags']['AUTO_STREAM_PUBLISH_ENABLED'])
            self.assertEqual(self.engine.processes,{})
            flags['AUTO_STREAM_PUBLISH_ENABLED']=False;self.client.put('/engine/settings',headers=HEADERS,json=flags)
    def prepared_owned_job(self):
        folder=self.media/'auto-stream'/'renders'/'render_test';folder.mkdir(parents=True);source=folder/'composition.mp4';source.write_bytes(b'verified-render')
        (folder/'intermediate.txt').write_text('temporary')
        job=self.engine.create({**self.payload,'media_path':str(source.relative_to(self.media))})
        with self.engine._connect() as con:con.execute("UPDATE jobs SET status='COMPLETED' WHERE id=?",(job['id'],))
        os.utime(source,(time.time()-13*3600,)*2)
        return job,source
    def test_cleanup_requires_receipt_and_preserves_shared_dependencies(self):
        job,source=self.prepared_owned_job()
        self.assertEqual(ops.cleanup(self.engine,False)['eligible'],[]);self.assertTrue(source.exists())
        with self.engine._connect() as con:con.execute('INSERT INTO cleanup_receipts VALUES(?,?,?,?,?,?)',(job['id'],job['media_path'],'gdrive:test/item',hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_size,e.utc_now()))
        other=self.engine.create({**self.payload,'media_path':job['media_path'],'idempotency_key':'dependency00002'})
        self.assertTrue(ops.cleanup(self.engine,False)['blocked']);self.assertTrue(source.exists())
        with self.engine._connect() as con:con.execute('DELETE FROM jobs WHERE id=?',(other['id'],))
        self.assertEqual(ops.cleanup(self.engine,True)['eligible'],[job['media_path']]);self.assertTrue(source.exists())
        self.assertEqual(ops.cleanup(self.engine,False)['eligible'],[job['media_path']]);self.assertFalse(source.parent.exists());self.assertTrue(self.engine.db.exists())
    def test_drive_readback_mismatch_never_records_cleanup_receipt(self):
        job,source=self.prepared_owned_job()
        def fake(args):
            if args[1].startswith('gdrive:'):Path(args[2]).write_bytes(b'wrong readback')
            return b''
        with patch.object(ops,'rclone',side_effect=fake):
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):ops.preserve(self.engine,job['id'],'gdrive:test')
        with self.engine._connect() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM cleanup_receipts').fetchone()[0],0)
        self.assertTrue(source.exists())
    def test_restart_marks_pending_render_failed_and_running_stream_interrupted(self):
        job=self.engine.create(self.payload)
        with self.engine._connect() as con:
            con.execute("UPDATE jobs SET status='INGESTING' WHERE id=?",(job['id'],))
            con.execute("INSERT INTO scene_renders(id,document,state,created_at,updated_at) VALUES('render_pending','{}','PREPARING','now','now')")
        recovered=e.StreamEngine(self.engine.root,self.media)
        self.assertEqual(recovered.get(job['id'])['status'],'INTERRUPTED')
        self.assertEqual(compose.read_render(recovered,'render_pending')['state'],'FAILED')
    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_actual_supervisor_lifecycle_and_telemetry_without_network(self):
        source=self.media/'real.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:s=320x180:r=30','-t','0.4','-c:v','libx264',str(source)],check=True,capture_output=True)
        job=self.engine.create({**self.payload,'media_path':'real.mp4'});self.engine.prepare(job['id'])
        original=self.engine._command
        def local(row,key,ingest_url):
            command=original(row,key,ingest_url);command[-1]=str(self.root/'encoder.flv');return command
        with patch.dict(os.environ,{'AUTO_STREAM_PUBLISH_ENABLED':'1'}),patch.object(self.engine,'_command',side_effect=local):
            self.engine.start(job['id'],internal_ingest=('rtmps://a.rtmps.youtube.com/live2','synthetic-key'))
            deadline=time.monotonic()+8
            while self.engine.get(job['id'])['status'] in e.LIVE_STATES and time.monotonic()<deadline:time.sleep(0.05)
        self.assertEqual(self.engine.get(job['id'])['status'],'COMPLETED')
        self.assertEqual(self.engine.processes,{})
        self.assertTrue((self.root/'encoder.flv').exists())
        self.assertIn('heartbeat',self.engine.metrics[job['id']]);self.assertNotIn('synthetic-key',str(self.engine.health()))

if __name__=='__main__':unittest.main()

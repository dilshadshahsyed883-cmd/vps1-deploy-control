"""Frozen 02 Nov Video Merger Library: VPS-first, optional Drive, owner-safe actions."""
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/"apps/video-merger/part01/app.py"
TEMPLATE=SOURCE.parent/"templates/index.html"


class FrozenLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        with patch.dict(os.environ, VIDEO_MERGER_ROOT=str(self.root),
                        VIDEO_MERGER_PROXY_CONFIG_DIR=str(self.root/"config")):
            spec=importlib.util.spec_from_file_location("vm_frozen_library", SOURCE)
            self.app=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.app.init_db()
        self.url="https://example.org/video.mp4"
        self.trim={"url":self.url,"trim_enabled":False}
        self.id="aliceclip"
        self.data=b"mp4-library"
        now=self.app.utcnow()
        with self.app.db() as c:
            c.execute("""INSERT INTO library_items
                (id,name,filename,status,owner_username,source_url,trim_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (self.id,"Intro","intro.mp4","importing","alice",self.url,
                 '{"url":"https://example.org/video.mp4","trim_enabled":false}',now,now))

    def imported(self, drive=False, fail=False):
        def download(url,dest,*args,**kw): dest.write_bytes(b"source")
        def transcode(src,dest,*args): dest.write_bytes(self.data)
        def rclone(args,**kw):
            if fail: raise RuntimeError("test Drive disconnected")
            if args[0]=="lsjson":
                return '{"ID":"mock-drive-id","Size":11}'
            return ""
        with patch.object(self.app,"source_to_file",side_effect=download), \
             patch.object(self.app,"transcode_library",side_effect=transcode), \
             patch.object(self.app,"verify_library_media",return_value=3.0), \
             patch.object(self.app,"probe",return_value={"format":{"duration":3.0},"streams":[{"codec_type":"video"}]}), \
             patch.object(self.app,"rclone",side_effect=rclone) as remote:
            self.app.process_library_import(self.id,self.url,self.trim,False,drive)
        return remote.call_count

    def test_checkbox_off_is_vps_only_and_merge_uses_saved_bytes(self):
        self.assertEqual(self.imported(drive=False),0)
        item=self.app.get_library_item(self.id)
        self.assertEqual(item["status"],"ready")
        self.assertEqual(item["drive_requested"],0)
        self.assertEqual(item["drive_status"],"not_requested")
        self.assertFalse(item["drive_url"])
        self.assertEqual(item["size_bytes"],len(self.data))
        self.assertEqual(self.app.library_cache_path(self.id).read_bytes(),self.data)
        output=self.root/"merger-input.mp4"
        with patch.object(self.app,"validate_downloaded_file"):
            self.app.library_to_file(self.id,output,1)
        self.assertEqual(output.read_bytes(),self.data)

    def test_drive_opt_in_uses_fixed_folder_only_after_vps_save(self):
        self.assertGreater(self.imported(drive=True),0)
        item=self.app.get_library_item(self.id)
        self.assertEqual(item["status"],"ready")
        self.assertEqual(item["drive_requested"],1)
        self.assertEqual(item["drive_status"],"ready")
        self.assertEqual(item["remote_path"],"Video Merger Library/assets/"+self.id+".mp4")
        self.assertEqual(item["drive_id"],"mock-drive-id")
        self.assertEqual(self.app.library_cache_path(self.id).read_bytes(),self.data)

    def test_drive_failure_never_hides_or_deletes_verified_vps_media(self):
        self.assertGreater(self.imported(drive=True,fail=True),0)
        item=self.app.get_library_item(self.id)
        self.assertEqual(item["status"],"ready")
        self.assertEqual(item["drive_status"],"failed")
        self.assertIn("test Drive disconnected",item["drive_error"])
        self.assertEqual(self.app.library_cache_path(self.id).read_bytes(),self.data)
        with patch.object(self.app.threading,"Thread") as launch:
            self.assertEqual(self.app.retry_library_drive(self.id),self.id)
            self.assertTrue(launch.called)
        self.assertEqual(self.app.get_library_item(self.id)["drive_status"],"uploading")

    def test_active_render_protects_all_three_vps_mutations(self):
        self.imported()
        job,_=self.app.add_job({
            "videos":[{"kind":"library","library_id":self.id},
                      {"kind":"url","url":"https://example.org/other.mp4"}],
            "filename_mode":"manual","filename":"merged.mp4",
            "upload_to_drive":False,
        },owner="alice")
        self.assertTrue(self.app.library_in_use(self.id))
        with self.assertRaisesRegex(ValueError,"in use"):
            self.app.rename_library_item(self.id,"Different")
        with self.assertRaisesRegex(ValueError,"used"):
            self.app.delete_library_item(self.id)
        with self.assertRaisesRegex(ValueError,"used"):
            self.app.create_library_item({"url":self.url},replace_id=self.id,owner="alice")
        self.app.cancel_job(job,owner="alice")
        self.assertFalse(self.app.library_in_use(self.id))
        self.assertEqual(self.app.rename_library_item(self.id,"Different"),"Different")
        self.app.delete_library_item(self.id)
        self.assertIsNone(self.app.get_library_item(self.id))
        self.assertFalse(self.app.library_cache_path(self.id).exists())

    def test_owner_scoped_view_and_byte_range(self):
        self.imported()
        def request(owner,byterange=""):
            handler=object.__new__(self.app.Handler)
            handler.path=self.app.PREFIX+"/api/library/"+self.id+"/view"
            handler.headers={"X-Auth-User":owner,"Range":byterange}
            handler.wfile=io.BytesIO()
            result={}
            handler.send_response=lambda code:result.update(code=code)
            handler.send_header=lambda name,value:result.setdefault("headers",{}).update({name:value})
            handler.end_headers=lambda:None
            handler.send_json=lambda code,body:result.update(code=code,json=body)
            handler.do_GET()
            return result,handler.wfile.getvalue()
        result,payload=request("alice","bytes=2-5")
        self.assertEqual(result["code"],206)
        self.assertEqual(payload,self.data[2:6])
        self.assertEqual(result["headers"]["Content-Range"],"bytes 2-5/11")
        denied,payload=request("bob","bytes=0-2")
        self.assertNotEqual(denied["code"],206)
        self.assertFalse(payload)

    def test_restart_keeps_preexisting_verified_vps_video_after_replace_interruption(self):
        self.imported(drive=False)
        self.assertEqual(self.app.get_library_item(self.id)['status'],'ready')
        with self.app.db() as c:
            c.execute("UPDATE library_items SET status='importing',progress=12,message='Replacing video' WHERE id=?",
                      (self.id,))
        # Startup recovery must remain local and never contact Drive.
        with patch.object(self.app,'rclone',side_effect=AssertionError('Network in recovery')):
            self.app.init_db()
        item=self.app.get_library_item(self.id)
        self.assertEqual(item['status'],'ready')
        self.assertIn('Previous verified',item['message'])
        self.assertEqual(self.app.library_cache_path(self.id).read_bytes(),self.data)

    def test_restart_does_not_mark_incomplete_new_import_ready(self):
        # A newly added item has no previously committed duration/size or media.
        self.assertFalse(self.app.library_cache_path(self.id).exists())
        with patch.object(self.app,'rclone',side_effect=AssertionError('Network in recovery')):
            self.app.init_db()
        self.assertEqual(self.app.get_library_item(self.id)['status'],'failed')

    def test_restart_recovers_existing_drive_backed_library_without_local_cache(self):
        # Original Library videos were Drive-backed and can restore VPS cache
        # on demand; restarting a replacement must not erase that reference.
        with self.app.db() as c:
            c.execute("""UPDATE library_items SET size_bytes=123,drive_id='prior-drive-id',
                         remote_path='Video Merger Library/assets/aliceclip.mp4',
                         status='importing' WHERE id=?""",(self.id,))
        with patch.object(self.app,'rclone',side_effect=AssertionError('Network in recovery')):
            self.app.init_db()
        item=self.app.get_library_item(self.id)
        self.assertEqual(item['status'],'ready')
        self.assertEqual(item['drive_id'],'prior-drive-id')

    def test_different_owners_can_use_same_name_without_seeing_other_items(self):
        with patch.object(self.app.threading,'Thread'):
            alice=self.app.create_library_item({'url':self.url,'name':'Same Intro'},owner='alice')
            bob=self.app.create_library_item({'url':self.url,'name':'Same Intro'},owner='bob')
            self.assertNotEqual(alice,bob)
            with self.assertRaisesRegex(ValueError,'already exists'):
                self.app.create_library_item({'url':self.url,'name':'Same Intro'},owner='alice')
        self.assertIn(alice,[r['id'] for r in self.app.list_library_items('alice')])
        self.assertNotIn(bob,[r['id'] for r in self.app.list_library_items('alice')])
        self.assertIn(bob,[r['id'] for r in self.app.list_library_items('bob')])
        self.assertNotIn(alice,[r['id'] for r in self.app.list_library_items('bob')])

    def test_old_global_unique_name_db_upgrades_without_deleting_legacy_media(self):
        with self.app.db() as c:
            schema=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='library_items'").fetchone()['sql']
            self.assertIn('name TEXT NOT NULL,',schema)
            c.execute('DROP TABLE library_items')
            c.execute(schema.replace('name TEXT NOT NULL,','name TEXT NOT NULL UNIQUE,',1))
            c.execute("""INSERT INTO library_items(id,name,filename,owner_username,
                         status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
                      ('legacy','Same Intro','legacy.mp4','alice','ready',self.app.utcnow(),self.app.utcnow()))
        self.app.init_db()
        old=self.app.get_library_item('legacy')
        self.assertEqual(old['name'],'Same Intro')
        self.assertEqual(old['owner_username'],'alice')
        with patch.object(self.app.threading,'Thread'):
            other=self.app.create_library_item({'url':self.url,'name':'Same Intro'},owner='bob')
        self.assertEqual(self.app.get_library_item(other)['owner_username'],'bob')
        # Idempotent recovery should not rebuild the schema or mutate owners.
        self.app.init_db()
        self.assertEqual(self.app.get_library_item('legacy')['name'],'Same Intro')
        self.assertEqual(self.app.get_library_item(other)['name'],'Same Intro')

    def test_ui_has_unchecked_drive_choice_and_real_vps_preview(self):
        html=TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('id="libraryUploadDrive" type="checkbox"',html)
        self.assertIn("libraryUploadDrive.checked=false",html)
        self.assertIn("libraryUploadDrive.checked=!!x.drive_requested",html)
        self.assertIn("upload_to_drive:libraryUploadDrive.checked",html)
        self.assertIn("libraryPreviewPlayer",html)
        self.assertIn("library-drive-working",html)
        self.assertIn("Uploading optional Google Drive copy",html)
        self.assertIn("/api/library/",html)
        self.assertIn("x.in_use",html)


if __name__=="__main__":
    unittest.main()

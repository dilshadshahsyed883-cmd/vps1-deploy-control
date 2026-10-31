"""Two-account, offline, disposable-DB acceptance for Video Merger handover."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "apps/video-merger/part01/app.py"
HTML = SOURCE.parent / "templates/index.html"


class OwnerHandoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        with patch.dict(os.environ, {
            "VIDEO_MERGER_ROOT": str(root),
            "VIDEO_MERGER_PROXY_CONFIG_DIR": str(root / "proxy"),
        }):
            spec = importlib.util.spec_from_file_location("vm_owner_handover", SOURCE)
            self.app = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.app)
        self.app.init_db()
        now = self.app.utcnow()
        with self.app.db() as c:
            for owner in ("alice", "bob"):
                c.execute(
                    "INSERT INTO library_items(id,name,filename,owner_username,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (owner+"lib",owner+" Video",owner+".mp4",owner,now,now))

    def queue(self, owner):
        return self.app.add_job({
            "videos": [
                {"kind":"library","library_id":owner+"lib"},
                {"kind":"url","url":"https://example.org/source.mp4"},
            ],
            "aspect":"9:16", "quality":"1080p", "fit_mode":"crop",
            "filename_mode":"manual", "filename":owner+".mp4",
            "upload_to_drive":False, "add_to_copyright_folder":False,
            "folder":"",
        },owner=owner)[0]

    def request(self, path, owner, admin=False, payload=None):
        handler = object.__new__(self.app.Handler)
        handler.path = self.app.PREFIX + path
        handler.headers = {"X-Auth-User":owner, "X-Is-Admin": "1" if admin else "0"}
        handler.read_json = lambda: payload or {}
        result = []
        handler.send_json = lambda status, body: result.append((status,body))
        if path == "/api/state":
            handler.do_GET()
        else:
            handler.do_POST()
        self.assertEqual(len(result),1)
        return result[0]

    def test_per_owner_pause_worker_skips_only_paused_account_and_ignores_legacy_global_pause(self):
        alice=self.queue("alice")
        bob=self.queue("bob")
        self.assertEqual(self.request("/api/queue/pause","alice")[0],200)
        self.assertTrue(self.app.owner_queue_paused("alice"))
        self.assertFalse(self.app.owner_queue_paused("bob"))
        self.app.set_setting("paused","1")  # legacy global state must not pause another owner
        self.assertEqual(self.app.next_waiting()["id"],bob)
        self.assertEqual(self.app.get_job(alice)["status"],"waiting")
        self.assertEqual(self.request("/api/queue/resume","alice")[0],200)
        self.assertEqual(self.app.next_waiting()["id"],alice)

    def test_unassigned_legacy_job_is_preserved_but_never_auto_claimed(self):
        jid=self.queue("alice")
        with self.app.db() as c:
            c.execute("UPDATE jobs SET owner_username='' WHERE id=?",(jid,))
        self.assertIsNone(self.app.next_waiting())
        self.assertEqual(self.app.get_job(jid)["status"],"waiting")
        self.assertEqual(self.app.list_jobs("alice"),[])
        self.assertEqual(self.app.list_jobs("bob"),[])

    def test_state_no_network_and_no_admin_cross_owner_data(self):
        alice=self.queue("alice")
        bob=self.queue("bob")
        with patch.object(self.app,"rclone",side_effect=AssertionError("Network in /api/state")):
            for is_admin in (False, True):
                code,body=self.request("/api/state","alice",admin=is_admin)
                self.assertEqual(code,200)
                self.assertEqual([x["id"] for x in body["jobs"]],[alice])
                self.assertNotIn(bob,[x["id"] for x in body["jobs"]])
                self.assertEqual([x["id"] for x in body["library"]],["alicelib"])
                self.assertEqual(body["folders"],[self.app.owner_drive_root("alice")])
                self.assertFalse(body["paused"])
                self.assertTrue(body["ok"])

    def test_owned_drive_folder_create_select_delete_and_foreign_protection(self):
        calls=[]
        def fake_rclone(args, **kwargs):
            calls.append(args)
            return ""
        with patch.object(self.app,"rclone",side_effect=fake_rclone):
            code, result=self.request("/api/folders","alice",payload={"name":"Hindi/Channel1"})
            self.assertEqual(code,201)
            name=result["folder"]
            self.assertTrue(name.startswith(self.app.owner_drive_root("alice")+"/"))
            self.assertIn(name,self.app.list_owner_folders("alice"))
            self.assertNotIn(name,self.app.list_owner_folders("bob"))
            self.assertEqual(self.app.resolve_owner_folder("alice",name),name)
            with self.assertRaisesRegex(ValueError,"owned by your account"):
                self.app.resolve_owner_folder("bob",name)
            code,bad=self.request("/api/folders/delete","bob",payload={"name":name})
            self.assertEqual(code,400)
            self.assertTrue(bad["error"])
            code,_=self.request("/api/folders/delete","alice",payload={"name":name})
            self.assertEqual(code,200)
            self.assertNotIn(name,self.app.list_owner_folders("alice"))
            self.assertIn("rmdir",calls[-1])
        with self.assertRaisesRegex(ValueError,"protected"):
            self.app.delete_owner_folder("alice",self.app.owner_drive_root("alice"))

    def test_delete_refuses_pending_job_or_nonempty_remote(self):
        with patch.object(self.app,"rclone",return_value=""):
            path=self.app.create_owner_folder("alice","ToDelete")
        jid=self.queue("alice")
        with self.app.db() as c:
            c.execute("UPDATE jobs SET folder=? WHERE id=?",(path,jid))
        with patch.object(self.app,"rclone",side_effect=AssertionError("Cannot rmdir active folder")):
            code,_=self.request("/api/folders/delete","alice",payload={"name":path})
            self.assertEqual(code,400)
        self.app.cancel_job(jid,owner="alice")
        with patch.object(self.app,"rclone",side_effect=RuntimeError("directory not empty")):
            code,_=self.request("/api/folders/delete","alice",payload={"name":path})
            self.assertEqual(code,500)
        self.assertIn(path,self.app.list_owner_folders("alice"))

    def test_foreign_job_and_library_actions_denied_even_when_admin(self):
        alice=self.queue("alice")
        bob=self.queue("bob")
        for path,payload in [
            ("/api/jobs/"+bob+"/cancel",{}),
            ("/api/jobs/"+bob+"/delete",{}),
            ("/api/jobs/"+bob+"/move",{"direction":"up"}),
            ("/api/library/boblib/rename",{"name":"Wrong owner"}),
            ("/api/library/boblib/delete",{}),
        ]:
            with self.subTest(path=path):
                code,_=self.request(path,"alice",admin=True,payload=payload)
                self.assertEqual(code,400)
        self.assertEqual(self.app.get_job(alice)["status"],"waiting")
        self.assertEqual(self.app.get_job(bob)["status"],"waiting")
        self.assertEqual(self.app.get_library_item("boblib")["name"],"bob Video")

    def test_private_folder_is_required_for_new_jobs(self):
        for folder in ("users/others/foo", "Shared/Legacy", "../other"):
            with self.subTest(folder=folder):
                with self.assertRaises(ValueError):
                    self.app.resolve_owner_folder("alice",folder)

    def test_proxy_is_admin_only_but_owner_operations_equal(self):
        self.assertEqual(self.request("/api/proxy/toggle","bob",payload={"enabled":True})[0],400)
        self.assertEqual(self.request("/api/queue/pause","bob",admin=False)[0],200)
        self.assertEqual(self.request("/api/queue/resume","bob",admin=True)[0],200)
        source=SOURCE.read_text()
        self.assertIn("#vm-tab-proxy,#tab-proxy{display:none!important}",source)
        self.assertIn("deliver_copyright_folder",source)
        html=HTML.read_text()
        self.assertIn("created.folder",html)
        self.assertIn("My Drive Root",html)

    def test_source_name_lookup_requires_session_and_does_no_network_when_logged_out(self):
        handler=object.__new__(self.app.Handler)
        handler.path=self.app.PREFIX+"/api/source-name"
        handler.headers={}
        handler.read_json=lambda: {"url":"https://www.youtube.com/watch?v=123"}
        observed=[]
        handler.send_json=lambda status,body: observed.append((status,body))
        with patch.object(self.app,"source_display_filename",
                          side_effect=AssertionError("unauthenticated source lookup")):
            handler.do_POST()
        self.assertEqual(observed[0][0],400)

    def test_guarded_overlay_only_changes_owner_folder_callbacks(self):
        from production.video_merger_import_release import overlay_owner_handover_ui
        source=HTML.read_text()
        changed=source.replace("function renderFolders()","function renderFolders()",1)
        self.assertEqual(overlay_owner_handover_ui(changed,source),source)
        drift=source.replace("async function addFolder()","async function otherName()")
        with self.assertRaisesRegex(RuntimeError,"anchor drift"):
            overlay_owner_handover_ui(drift,source)


if __name__=="__main__":
    unittest.main()

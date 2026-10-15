import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from rebuild import fresh_vps as f

class FreshRootfsTests(unittest.TestCase):
    def test_11_groups_with_separate_auto_script_owner(self):
        p=f.registry()
        self.assertEqual(len(p["groups"]),11)
        self.assertTrue(p["groups"]["auto-script"]["parallel_owner"])
        self.assertEqual(p["groups"]["auto-script"]["deployment_mode"], "offline")
        self.assertEqual(len(f.managed_groups()),10)
        self.assertNotIn("algrow-voice",p["groups"])
        self.assertEqual(p["groups"]["video-merger"]["services"][0]["kind"],"script")
        self.assertEqual(p["groups"]["vps-storage-manager"]["services"][0]["args"],["serve"])

    def test_guarded_standalone_service_candidates(self):
        merger=f.service_unit("video-merger",{"kind":"script","part":"part01","entry":"app.py","port":8148})
        storage=f.service_unit("vps-storage-manager",{"kind":"script","part":"part01","entry":"storage_manager.py","args":["serve"],"port":8149})
        self.assertIn("bin/python app.py",merger)
        self.assertIn("bin/python storage_manager.py serve",storage)
        for candidate in (merger,storage):
            self.assertIn("NoNewPrivileges=true",candidate)
            self.assertIn("ConditionPathExists=/etc/fresh-website/enabled/",candidate)
            self.assertIn("ProtectSystem=strict",candidate)

    def test_reject_host_root_and_relative_paths(self):
        for value in ("/","/etc","/opt/new","/var/data","/srv/new","/home/user","relative","/tmp"):
            with self.assertRaises(ValueError):
                f.root_dir(value)

    def test_source_filter_drops_old_backups_and_dangerous_scripts(self):
        with tempfile.TemporaryDirectory() as d:
            repo=Path(d)
            src=repo/"apps/short-maker"
            (src/".acs_backups").mkdir(parents=True)
            (src/"part01").mkdir()
            (src/"part01/app.py").write_text("from fastapi import FastAPI\napp=FastAPI()\n")
            (src/".acs_backups/old.py").write_text("old=1\n")
            (src/"part01/production_reset.sh").write_text("rm -rf /")
            with patch.object(f,"ROOT",repo):
                files=f.sources("short-maker")
            self.assertEqual([item[0] for item in files],["part01/app.py"])

    def test_real_lum_database_repository_source_is_not_runtime_data(self):
        with tempfile.TemporaryDirectory() as d:
            repo=Path(d)
            package=repo/"apps/local-upload-manager/part01/desktop_core/modules/db"
            package.mkdir(parents=True)
            (package/"__init__.py").write_text("# source module\n")
            (package/"repository.py").write_text("class QueueRepository: pass\n")
            (package/"old-data.sqlite3").write_bytes(b"SQLite format 3\\x00")
            with patch.object(f,"ROOT",repo):
                items=f.sources("local-upload-manager")
            self.assertEqual([path for path,_ in items],
                ["part01/desktop_core/modules/db/__init__.py",
                 "part01/desktop_core/modules/db/repository.py"])

    def test_credential_content_blocks_release(self):
        with tempfile.TemporaryDirectory() as d:
            repo=Path(d);src=repo/"apps/short-maker"
            src.mkdir(parents=True)
            (src/"app.py").write_text("token='ghp_"+"a"*32+"'\n")
            with patch.object(f,"ROOT",repo):
                with self.assertRaises(ValueError):f.sources("short-maker")

    def test_atomic_staging_empty_runtime_and_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            repo=Path(d)/"source"
            src=repo/"apps/short-maker/part01"
            src.mkdir(parents=True)
            app=src/"app.py";app.write_text("x=1\n")
            root=Path(d)/"isolated"
            with patch.object(f,"ROOT",repo):
                first=f.deploy_app(root,"short-maker","a"*40)
                self.assertEqual(first["files"],1)
                self.assertEqual(list((root/"var/lib/fresh-website/short-maker/queue").iterdir()),[])
                self.assertEqual(list((root/"var/lib/fresh-website/short-maker/db").iterdir()),[])
                app.write_text("x=2\n")
                f.deploy_app(root,"short-maker","b"*40)
                self.assertEqual((root/"srv/fresh-website/current/short-maker/part01/app.py").read_text(),"x=2\n")
                self.assertEqual(f.rollback(root,"short-maker")["restored_revision"],"a"*40)
                self.assertEqual((root/"srv/fresh-website/current/short-maker/part01/app.py").read_text(),"x=1\n")

    def test_no_rollback_without_previous_release(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):f.rollback(d,"scene-builder")

    def test_verify_detects_modified_source_and_enabled_units(self):
        with tempfile.TemporaryDirectory() as d:
            repo=Path(d)/"repo"
            src=repo/"apps/short-stream/part01"
            src.mkdir(parents=True)
            (src/"app.py").write_text("x=1\n")
            root=Path(d)/"rootfs"
            with patch.object(f,"ROOT",repo),patch.object(f,"managed_groups",return_value=["short-stream"]):
                f.deploy_app(root,"short-stream","a"*40)
                enabled=root/"etc/fresh-website/enabled"
                enabled.mkdir(parents=True)
                self.assertEqual(f.verify(root)["result"],"PASS")
                (enabled/"short-stream").touch()
                with self.assertRaises(ValueError):f.verify(root)
                (enabled/"short-stream").unlink()
                (root/"srv/fresh-website/current/short-stream/part01/app.py").write_text("tampered\n")
                with self.assertRaises(ValueError):f.verify(root)

if __name__=="__main__":
    unittest.main()

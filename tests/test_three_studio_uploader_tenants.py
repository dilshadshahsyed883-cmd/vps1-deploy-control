"""Offline, no-production tests for Song/Short/Copyright tenant-bound resources."""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PACKAGES=[
 ROOT/"apps/local-upload-manager/part01/lum_engine",
 ROOT/"apps/copyright-uploader/part01/lum_engine",
]


class TenantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def tenant(self, package, name):
        modpath=package/"studio_tenants.py"
        spec=importlib.util.spec_from_file_location("isolated_tenants_"+name,modpath)
        mod=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        class SQLite:
            def __init__(self, path):
                self.path=path
            def connect(self):
                return sqlite3.connect(self.path)
        return mod.StudioTenants(SQLite(str(Path(self.temp.name)/(name+".sqlite"))))

    def test_strict_two_user_and_legacy_ownership_both_packages(self):
        alice={"username":"Alice","is_admin":0}
        bob={"username":"Bob","is_admin":0}
        admin={"username":"Administrator","is_admin":1}
        for idx,package in enumerate(PACKAGES):
            with self.subTest(instance=idx):
                tenant=self.tenant(package,str(idx))
                tenant.bind("channel",11,"ALICE")
                tenant.bind("profile",21,"alice")
                tenant.bind("channel",12,"BOB")
                tenant.bind("profile",22,"bob")
                self.assertEqual(tenant.allowed_ids("channel",alice),{11})
                self.assertEqual(tenant.allowed_ids("profile",bob),{22})
                self.assertIsNone(tenant.allowed_ids("profile",admin))
                tenant.require("channel",11,alice)
                tenant.require("profile",22,bob)
                for kind,id in (("channel",12),("profile",22),("sheet",44)):
                    with self.assertRaises(PermissionError):
                        tenant.require(kind,id,alice)
                tenant.require("sheet",44,admin)  # Unassigned legacy is admin-only.
                self.assertEqual(tenant.owner("group",100),"")
                with self.assertRaises(PermissionError):
                    tenant.bind("channel",11,"bob")
                with self.assertRaises(PermissionError):
                    tenant.assign_by_admin("channel",11,"bob",admin)
                with self.assertRaises(PermissionError):
                    tenant.assign_by_admin("channel",30,"bob",alice)
                self.assertEqual(tenant.assign_by_admin("channel",30,"bob",admin),"bob")
                self.assertEqual(tenant.allowed_ids("channel",bob),{12,30})
                self.assertEqual(tenant.owner("channel",11),"alice")
                with self.assertRaises(PermissionError):
                    tenant.allowed_ids("channel",{"username":""})

    def test_studios_do_not_share_tenant_database_rows(self):
        a=self.tenant(PACKAGES[0],"song")
        b=self.tenant(PACKAGES[1],"copyright")
        a.bind("channel",42,"alice")
        b.bind("channel",42,"bob")
        self.assertEqual(a.owner("channel",42),"alice")
        self.assertEqual(b.owner("channel",42),"bob")

    def test_new_tenant_module_is_in_immutable_source_export(self):
        from rebuild.fresh_vps import sources
        for app in ("local-upload-manager","copyright-uploader"):
            with self.subTest(app=app):
                names={path for path,_contents in sources(app)}
                self.assertIn("part01/lum_engine/studio_tenants.py",names)
                self.assertIn("part01/lum_engine/standalone_app.py",names)

    def test_specialized_song_short_releases_package_owners_and_counts(self):
        root=Path(__file__).resolve().parents[1]
        for file_name in ("short_uploader_group_release.py",
                          "song_uploader_group_summary_release.py"):
            with self.subTest(release=file_name):
                script=(root/"production"/file_name).read_text(encoding="utf-8")
                self.assertIn('"lum_engine/studio_tenants.py"',script)
                self.assertIn('"lum_engine/project_store.py"',script)
                self.assertIn('NEW_FILES=frozenset({"lum_engine/studio_tenants.py"})',script)
                self.assertIn('relative in NEW_FILES and not dst.exists()',script)
                compile(script,file_name,"exec")

    def test_middleware_direct_routes_and_profile_history_have_guards(self):
        for package in PACKAGES:
            standalone=(package/"standalone_app.py").read_text(encoding="utf-8")
            engine=(package/"auto_engine_v6.py").read_text(encoding="utf-8")
            template=(package.parent/"templates/app.html").read_text(encoding="utf-8")
            for marker in (
                "TENANT_SAFE_WRITES", "studio_tenants.require(\"channel\"",
                "studio_tenants.require(\"profile\"", "studio_tenants.bind(\"profile\"",
                "studio_tenants.assign_by_admin(", "STUDIO_TENANT_ASSIGNMENT_V1",
                "studio_tenants.allowed_ids(\"channel\"", "studio_tenants.allowed_ids(\"sheet\"",
            ):
                self.assertIn(marker,standalone, (package,marker))
            for marker in ("STUDIO_USER_ISOLATION_V1", 'allowed_profiles=acl.allowed_ids("profile",viewer)',
                           'allowed_channels=acl.allowed_ids("channel",viewer)',
                           'int(row.get("auto_engine_profile_id") or row.get("profile_id") or 0)'):
                self.assertIn(marker,engine)
            self.assertIn("{% if user.is_admin and engine_instance in ",template)
            self.assertIn("/actions/studio-tenant/grant",template)
            # Regression: direct OAuth/queue actions must not fall back to the
            # service-global channel inventory when called by a member.
            for marker in (
                'studio_tenants.require("group",group_id,user)',
                'studio_tenants.require("channel",cid,user)',
                'studio_tenants.require("channel",channel_id,user)',
                'video_ids=[core_id]',
                'payload["connections"]=[',
                'pc_sync_history = (list_sync_history() if selected == "channels"',
                'counts=store.counts(owner=owner)',
                'route("/actions/google-oauth/connect")',
            ):
                self.assertIn(marker,standalone,(package,marker))
            compile(standalone,str(package/"standalone_app.py"),"exec")
            compile(engine,str(package/"auto_engine_v6.py"),"exec")


if __name__=="__main__":
    unittest.main()

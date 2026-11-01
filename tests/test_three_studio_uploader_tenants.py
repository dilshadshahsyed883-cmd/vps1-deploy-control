"""Offline, no-production tests for Song/Short/Copyright tenant-bound resources."""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
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

    def test_owner_self_service_channel_group_is_atomic_and_collision_safe(self):
        for idx,package in enumerate(PACKAGES):
            with self.subTest(instance=idx):
                tenant=self.tenant(package,"self-serve-"+str(idx))
                with tenant.db.connect() as con:
                    con.executescript("""
                        CREATE TABLE channels(
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            channel_id TEXT UNIQUE NOT NULL,
                            channel_name TEXT NOT NULL,
                            google_account_email TEXT,
                            token_file_path TEXT NOT NULL,
                            channel_url TEXT,status TEXT NOT NULL DEFAULT 'ACTIVE');
                        CREATE TABLE channel_groups(
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            group_name TEXT UNIQUE NOT NULL,
                            notes TEXT);
                        CREATE TABLE channel_group_members(
                            group_id INTEGER NOT NULL, channel_db_id INTEGER NOT NULL,
                            PRIMARY KEY(group_id,channel_db_id));
                    """)
                a1=tenant.create_channel("ALICE","UC"+"a"*22,"Alice Channel","alice@example.com")
                a2=tenant.create_channel("alice","UC"+"b"*22,"Second Channel")
                b1=tenant.create_channel("BOB","UC"+"c"*22,"Bob Channel")
                self.assertEqual(tenant.allowed_ids("channel",{"username":"alice","is_admin":0}),{a1,a2})
                with self.assertRaises(PermissionError):
                    tenant.create_channel("bob","UC"+"a"*22,"Hijack")
                with self.assertRaises(ValueError):
                    tenant.create_channel("alice","invalid","Wrong")
                with self.assertRaises(PermissionError):
                    tenant.create_group("alice","Mixed Group",[a1,b1])
                self.assertEqual(tenant.owner("group",1),"")
                group=tenant.create_group("alice","Alice Group",[a1,a2])
                self.assertEqual(tenant.owner("group",group),"alice")
                with self.assertRaises(PermissionError):
                    tenant.create_group("bob","Alice Group",[b1])
                bob_group=tenant.create_group("bob","Bob Group",[b1])
                self.assertEqual(tenant.owner("group",bob_group),"bob")
                with tenant.db.connect() as con:
                    members=con.execute("SELECT channel_db_id FROM channel_group_members WHERE group_id=?",
                                        (group,)).fetchall()
                    token=con.execute("SELECT token_file_path FROM channels WHERE id=?",(a1,)).fetchone()
                    self.assertEqual({row[0] for row in members},{a1,a2})
                    self.assertEqual(token[0],"")
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM channels").fetchone()[0],3)

    def test_owned_sheet_sync_rollback_and_delete_preserve_credentials(self):
        for idx,package in enumerate(PACKAGES):
            with self.subTest(instance=idx):
                tenant=self.tenant(package,"sheets-"+str(idx))
                with tenant.db.connect() as con:
                    con.executescript("""
                        CREATE TABLE sheet_sources (
                          id INTEGER PRIMARY KEY,sheet_name TEXT NOT NULL,
                          last_sync_at TEXT,last_sync_status TEXT,updated_at TEXT);
                        CREATE TABLE channels (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          channel_id TEXT UNIQUE NOT NULL,channel_name TEXT NOT NULL,
                          google_account_email TEXT,token_file_path TEXT NOT NULL,
                          status TEXT NOT NULL DEFAULT 'ACTIVE',
                          sheet_source_id INTEGER,sheet_row_key TEXT,sheet_present INTEGER,
                          sheet_notes TEXT,sheet_last_seen_at TEXT,last_used_at TEXT
                        );
                        INSERT INTO sheet_sources(id,sheet_name) VALUES (1,'Alice Sheet');
                        INSERT INTO channels(channel_id,channel_name,token_file_path)
                          VALUES ('UCbbbbbbbbbbbbbbbbbbbbbb','Bob token','/keep/bob-token.json');
                    """)
                tenant.bind("sheet",1,"alice")
                tenant.bind("channel",1,"bob")
                alice=SimpleNamespace(key="alice-row",channel_id="UC"+"a"*22,
                                      channel_name="Alice",gmail="alice@example.com",notes="mine")
                foreign=SimpleNamespace(key="foreign",channel_id="UC"+"b"*22,
                                        channel_name="Bob",gmail="bob@example.com",notes="")
                with self.assertRaises(PermissionError):
                    tenant.sync_owned_sheet_rows("Alice",1,[alice,foreign])
                with tenant.db.connect() as con:
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM channels").fetchone()[0],1)
                    self.assertEqual(con.execute("SELECT token_file_path FROM channels WHERE id=1").fetchone()[0],
                                     "/keep/bob-token.json")
                result=tenant.sync_owned_sheet_rows("ALICE",1,[alice])
                self.assertEqual(result["rows"],1)
                self.assertEqual(tenant.owner("channel",2),"alice")
                self.assertEqual(tenant.allowed_ids("channel",{"username":"bob","is_admin":0}),{1})
                with self.assertRaises(PermissionError):
                    tenant.delete_owned_sheet("bob",1)
                self.assertEqual(tenant.delete_owned_sheet("alice",1),1)
                self.assertEqual(tenant.owner("sheet",1),"")
                with tenant.db.connect() as con:
                    values=con.execute("SELECT channel_id,token_file_path,sheet_source_id FROM channels ORDER BY id").fetchall()
                    self.assertEqual(len(values),2)
                    self.assertEqual(values[0][1],"/keep/bob-token.json")
                    self.assertIsNone(values[1][2])

    def test_owned_manual_channel_can_join_owned_sheet_without_token_rotation(self):
        for idx,package in enumerate(PACKAGES):
            with self.subTest(instance=idx):
                tenant=self.tenant(package,"existing-own-sheet-"+str(idx))
                with tenant.db.connect() as con:
                    con.executescript("""
                      CREATE TABLE sheet_sources(
                        id INTEGER PRIMARY KEY,last_sync_at TEXT,
                        last_sync_status TEXT,updated_at TEXT);
                      CREATE TABLE channels(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id TEXT UNIQUE NOT NULL,channel_name TEXT NOT NULL,
                        google_account_email TEXT,token_file_path TEXT NOT NULL,
                        status TEXT NOT NULL,sheet_source_id INTEGER,
                        sheet_row_key TEXT,sheet_present INTEGER,
                        sheet_notes TEXT,sheet_last_seen_at TEXT,last_used_at TEXT);
                      INSERT INTO sheet_sources(id) VALUES (1);
                      INSERT INTO channels(channel_id,channel_name,token_file_path,status)
                        VALUES ('UCaaaaaaaaaaaaaaaaaaaaaa','Original','/keep/alice-oauth.json','ACTIVE');
                    """)
                tenant.bind("sheet",1,"alice")
                tenant.bind("channel",1,"alice")
                parsed=SimpleNamespace(key="alpha",channel_id="UC"+"a"*22,
                    channel_name="Updated channel name",gmail="alice@example.com",notes="")
                result=tenant.sync_owned_sheet_rows("alice",1,[parsed])
                self.assertTrue(result["ok"])
                with tenant.db.connect() as con:
                    row=con.execute("""SELECT id,token_file_path,sheet_source_id,
                        sheet_row_key FROM channels""").fetchone()
                    self.assertEqual(row,(1,"/keep/alice-oauth.json",1,"alpha"))
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM channels").fetchone()[0],1)

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
                self.assertIn('"lum_engine/channels_web.py"',script)
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
                scoped_alias = marker.replace("studio_tenants.require", "_studio_require").replace(
                    "studio_tenants.allowed_ids", "_studio_allowed_ids")
                self.assertTrue(marker in standalone or scoped_alias in standalone,
                                (package, marker, scoped_alias))
            for marker in ("STUDIO_USER_ISOLATION_V1", 'allowed_profiles=acl.allowed_ids("profile",viewer)',
                           'allowed_channels=acl.allowed_ids("channel",viewer)',
                           'int(row.get("auto_engine_profile_id") or row.get("profile_id") or 0)'):
                self.assertIn(marker,engine)
            # Retired for admins and members across Song, Short and Copyright uploaders.
            # Owner-scoped onboarding and backend isolation remain untouched.
            self.assertNotIn("Assign Studio resources to a user",template)
            self.assertNotIn('aria-label="User workspace ownership"',template)
            self.assertNotIn("/actions/studio-tenant/grant",template)
            self.assertNotIn("Exact Scene Builder username",template)
            # Copyright removed the manual forms, but retains tenant-safe
            # API endpoints for backward compatibility and isolation.
            if package.parent.parent.name == "copyright-uploader":
                self.assertNotIn("Your YouTube channels &amp; groups", template)
                self.assertNotIn("/actions/studio-tenant/channel-create", template)
                self.assertNotIn("/actions/studio-tenant/group-create", template)
            else:
                self.assertIn("Your YouTube channels &amp; groups", template)
                self.assertIn("/actions/studio-tenant/channel-create", template)
                self.assertIn("/actions/studio-tenant/group-create", template)
            self.assertIn("STUDIO_TENANT_SAFE_ONBOARDING_V1",standalone)
            self.assertIn('studio_tenants.create_channel(',standalone)
            self.assertIn('studio_tenants.create_group(',standalone)
            compile((package/"channels_web.py").read_text(),
                    str(package/"channels_web.py"),"exec")
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
                strict_alias = marker.replace("studio_tenants.require", "_studio_require")
                self.assertTrue(marker in standalone or strict_alias in standalone,
                                (package, marker, strict_alias))
            compile(standalone,str(package/"standalone_app.py"),"exec")
            compile(engine,str(package/"auto_engine_v6.py"),"exec")


    def test_song_assignment_ui_release_is_template_only(self):
        root=Path(__file__).resolve().parents[1]
        release=(root/"production/song_uploader_group_summary_release.py").read_text(encoding="utf-8")
        runtime=(root/"production/runtime.py").read_text(encoding="utf-8")
        self.assertIn("def install(revision, *, ui_only=False):", release)
        self.assertIn('for relative in (("templates/app.html",) if ui_only else FILES):', release)
        self.assertIn("if not ui_only:", release)
        self.assertIn("SONG_ASSIGNMENT_UI_RELEASE", release)
        self.assertIn("'User workspace ownership' not in html", release)
        self.assertIn("'[vps1-song-uploader-assignment-ui-only]' in intent", runtime)
        self.assertIn("install_song_ui(revision, ui_only=True)", runtime)
        compile(release, "production/song_uploader_group_summary_release.py", "exec")
        compile(runtime, "production/runtime.py", "exec")


if __name__=="__main__":
    unittest.main()

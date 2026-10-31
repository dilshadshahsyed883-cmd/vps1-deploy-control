"""Short Studio profile control regression contracts, offline and non-mutating."""
from __future__ import annotations

import ast
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
PART = ROOT / "apps/local-upload-manager/part01"
TEMPLATE = PART / "templates/app.html"
ENGINE = PART / "lum_engine/auto_engine_v6.py"
ROUTES = PART / "lum_engine/standalone_app.py"
WORKER = PART / "lum_engine/auto_engine_v6_worker.py"
REPO = PART / "desktop_core/modules/db/queue_repository.py"
JAVASCRIPT = PART / "static/auto_engine_v6.js"
INSTALLER = ROOT / "production/short_uploader_group_release.py"
WORKFLOW = ROOT / ".github/workflows/vps1-short-studio-release.yml"


def load_function(path, name, namespace):
    tree = ast.parse(path.read_text())
    funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert funcs, name
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), funcs[0]], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


class ShortProfileControlTests(unittest.TestCase):
    def test_shortstudio_sheets_compact_toolbar_retains_sync_actions(self):
        source=TEMPLATE.read_text()
        start=source.index("{% elif tab == 'sheets' %}")
        end=source.index("{% elif tab == 'auto_engine' %}",start)
        section=source[start:end].split("\n",1)[1]
        ctx=dict(base_path='/short-uploader',csrf='offline',user={'is_admin': True},selected_sheet_source=None,
                 sheet_sources=[dict(id=1,sheet_name='Source',sheet_url_or_id='sheet-url',
                                     enabled=True,last_sync_at=None,last_sync_status=None)],
                 stats=dict(settings=dict(sheets_sync_enabled='1')))
        short=Environment().from_string(section).render(engine_instance='shortstudio',**ctx)
        self.assertNotIn('PC-app Sheets engine connected',short)
        self.assertNotIn('<h2>Manual Update</h2>',short)
        self.assertNotIn('Same PC synchronization behavior',short)
        self.assertIn('<summary>Sheet format</summary>',short)
        self.assertEqual(short.count('>Update All Enabled Sheets</button>'),1)
        self.assertIn('/actions/sheets/sync-all',short)
        self.assertIn('/actions/sheets/sync',short)
        self.assertIn('Save Sheet Source',short)
        song=Environment().from_string(section).render(engine_instance='song',**ctx)
        self.assertNotIn('<h2>Manual Update</h2>',song)
        self.assertIn('lum-short-sheet-header-actions',song)
        self.assertEqual(song.count('>Update All Enabled Sheets</button>'),1)

    def test_bulk_import_not_available_to_short_uploader(self):
        template=TEMPLATE.read_text()
        routes=ROUTES.read_text()
        self.assertIn("{% if engine_instance not in ['song', 'shortstudio'] %}",template)
        self.assertIn("tab=='bulk_import'",template)
        self.assertIn("{% elif tab == 'bulk_import' and engine_instance not in ['song', 'shortstudio'] %}",template)
        self.assertIn('allowed = {"dashboard", "channels", "sheets", "auto_engine", "logs"}',routes)
        # Existing Auto Uploader/other instances retain their Bulk Import UI.
        self.assertIn('<h2>Bulk Import</h2>',template)
        Environment().parse(template)

    def test_thumbnail_library_is_hidden_in_short_but_available_to_song(self):
        source = TEMPLATE.read_text()
        self.assertIn("engine_instance != 'shortstudio' and not (p and p.channel_db_id)", source)
        self.assertIn("<h2>Thumbnail Library", source)
        Environment().parse(source)

    def test_four_slots_are_individually_selectable_and_saved_in_existing_csv_field(self):
        template = TEMPLATE.read_text()
        javascript = JAVASCRIPT.read_text()
        self.assertIn('data-short-schedule-slots data-short-profile-schedule', template)
        self.assertIn('data-short-count="{{ pr.daily_upload_count or 4 }}"', template)
        self.assertIn('name="schedule_times" data-short-schedule-csv', template)
        self.assertIn("input.type='time'", javascript)
        self.assertIn("countFor(host)", javascript)
        self.assertIn("field.setCustomValidity(", javascript)
        self.assertIn("One PKT time per video per channel", template)
        ns={"re":re, "clean":lambda x:str(x or "").strip(), "Any":Any, "List":List}
        parse = load_function(ENGINE, "parse_schedule_times", ns)
        self.assertEqual(parse("09:00,12:15,16:30,21:45"), ["09:00","12:15","16:30","21:45"])
        self.assertEqual(len(parse("09:00,09:00,12:15")), 2)
        with self.assertRaises(ValueError): parse("25:00")
        self.assertIn('count != data["daily_upload_count"]', ENGINE.read_text())
        self.assertIn("if len(parsed_times)!=expected:", ROUTES.read_text())

    def test_start_pause_are_ownership_checked_and_isolated(self):
        routes=ROUTES.read_text()
        worker=WORKER.read_text()
        repo=REPO.read_text()
        engine=ENGINE.read_text()
        for endpoint in ('/actions/auto-v6/start-profile','/actions/auto-v6/pause-profile'):
            self.assertIn(endpoint,routes)
        self.assertIn('profile=_v6_profile(pid,request)',routes)
        self.assertIn('require_csrf(request,csrf_token); require_heavy(user)',routes)
        self.assertIn('v6_store.set_manual_profile_ids(sorted(active))',routes)
        self.assertIn('db.set_auto_engine_profile_enabled(pid,False)',routes)
        self.assertIn('allowed_auto_engine_profile_ids',worker)
        self.assertIn('allowed_auto_engine_profile_ids',repo)
        self.assertIn('manual_profile_ids TEXT DEFAULT NULL',engine)
        self.assertIn('v6_store.set_manual_profile_ids(None)',routes)
        self.assertIn('os.environ.get("LUM_ENGINE_INSTANCE") in {"shortstudio","song"}',worker)

    def test_allowed_profile_filter_claims_only_selected_queue(self):
        tree=ast.parse(REPO.read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="QueueRepository")
        target=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="claim_available_jobs")
        class_node=ast.ClassDef(name="ClaimOnly",bases=[],keywords=[],body=[target],decorator_list=[])
        mod=ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),class_node],type_ignores=[])
        ns={"datetime":datetime,"timedelta":timedelta,"timezone":timezone,"List":List,"Dict":dict,"Any":Any,"Optional":__import__("typing").Optional,"Iterable":__import__("typing").Iterable}
        exec(compile(ast.fix_missing_locations(mod),str(REPO),"exec"),ns)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"jobs.db"
            with sqlite3.connect(path) as con:
                con.execute("""CREATE TABLE videos(id INTEGER PRIMARY KEY, channel_db_id INTEGER,
                   auto_engine_profile_id INTEGER, status TEXT, claim_worker_id TEXT,
                   claimed_at TEXT, lease_expires_at TEXT, updated_at TEXT,
                   app_upload_at TEXT, created_at TEXT, normalized_filename TEXT,
                   recovery_state TEXT)""")
                con.execute("INSERT INTO videos(id,channel_db_id,auto_engine_profile_id,status,created_at,normalized_filename) VALUES (1,1,11,'QUEUED','2026-01-01','one.mp4')")
                con.execute("INSERT INTO videos(id,channel_db_id,auto_engine_profile_id,status,created_at,normalized_filename) VALUES (2,2,22,'QUEUED','2026-01-01','two.mp4')")
            obj=ns["ClaimOnly"]()
            def connect():
                con=sqlite3.connect(path)
                con.row_factory=sqlite3.Row
                return con
            obj.connect=connect
            obj.ensure_pending_upload_identities=lambda **kwargs:None
            obj._set_ledger_state=lambda *args:None
            obj.get_video=lambda vid:dict(connect().execute("SELECT * FROM videos WHERE id=?",(vid,)).fetchone())
            jobs=obj.claim_available_jobs("test-worker",1,auto_engine_only=True,allowed_auto_engine_profile_ids=[22])
            self.assertEqual([j["id"] for j in jobs],[2])
            with connect() as con:
                self.assertEqual(con.execute("SELECT status FROM videos WHERE id=1").fetchone()[0],"QUEUED")

    def test_automation_actions_not_hidden_by_legacy_css(self):
        css=(PART/"static/local_upload_manager.css").read_text()
        html=TEMPLATE.read_text()
        self.assertIn(".lum-ae-row-actions form{display:none!important}",css)
        self.assertIn('form[data-short-profile-action]',css)
        self.assertIn("display:inline-flex!important",css.split('form[data-short-profile-action]',1)[1][:140])
        self.assertIn('form[hidden]{display:none!important}',css)
        self.assertIn('data-short-profile-action="start"',html)
        self.assertIn('>Pause Profile</button>',html)
        self.assertIn('action="{{ base_path }}/actions/auto-v6/start-profile"',html)

    def test_automation_only_shows_saved_times_and_profile_owns_schedule_controls(self):
        html=TEMPLATE.read_text()
        js=JAVASCRIPT.read_text()
        automation=html.split('class="lum-table lum-ae-automation-profiles"',1)[1].split('</table>',1)[0]
        self.assertIn('class="lum-ae-saved-times"',automation)
        self.assertNotIn('data-short-inline-schedule',automation)
        self.assertNotIn('>Save Times</button>',automation)
        self.assertNotIn('data-short-profile-action="pause"',automation)
        self.assertIn('data-short-profile-action="start"',automation)
        self.assertIn('data-short-schedule-slots data-short-profile-schedule',html)
        self.assertIn('data-ae-open-schedules>Manage Schedules</button>',html)
        self.assertIn('data-ae-open-schedules>Profile Schedules</button>',html)
        self.assertIn("'short-profile-privacy-status' if short_group else 'song-profile-privacy-status'",html)
        self.assertIn('>Pause Profile</button>',html)
        self.assertIn("data-short-schedule-slots",js)
        self.assertIn('"/actions/auto-v6/profile-schedule/save"',ROUTES.read_text())

    def test_inline_schedule_route_requires_exact_slot_count_and_ownership(self):
        source=ROUTES.read_text()
        start=source.index('def auto_v6_profile_schedule_save(')
        end=source.index('@app.post(route("/actions/auto-v6/profile-schedules/save"))',start)
        body=source[start:end]
        self.assertIn('_v6_profile(pid,request)',body)
        self.assertIn('require_csrf(request,csrf_token); require_heavy(user)',body)
        self.assertIn("len(parsed)!=expected",body)
        self.assertIn('data["work_mode"]="auto"',body)
        self.assertIn('db.save_auto_engine_profile(data,pid)',body)
        self.assertNotIn('set_engine(',body)
        self.assertNotIn('set_scheduler_armed(',body)
        self.assertIn('if os.environ.get("LUM_ENGINE_INSTANCE") not in {"shortstudio","song"}',source)

    def test_short_static_asset_cache_isolated_and_live_checked(self):
        base=(PART/"templates/base.html").read_text()
        installer=INSTALLER.read_text()
        self.assertIn("&amp;short=20261030-compact-automation-v3",base)
        self.assertIn("css_status,_,css_body=https_get('/short-uploader/static/local_upload_manager.css',cookie)",installer)
        self.assertIn("class=\"lum-ae-saved-times\"",installer)
        self.assertIn('data-short-profile-action="start"',installer)

    def test_guarded_release_copies_all_changed_short_uploader_files(self):
        installer=INSTALLER.read_text()
        for part in ("auto_engine_v6.py","standalone_app.py","auto_engine_v6_worker.py",
                     "queue_repository.py","templates/app.html","auto_engine_v6.js","local_upload_manager.css"):
            self.assertIn(part,installer)
        self.assertIn("idle()",installer)
        self.assertIn("backup_databases",installer)
        self.assertIn("Short Uploader should not show thumbnail rotation library",installer)
        self.assertIn("[vps1-shortstudio-profile-controls]",WORKFLOW.read_text())

    def test_short_profiles_compact_action_row_preserves_scheduler_and_dropdown(self):
        """Only Short Uploader removes verbose manager; no scheduler state mutation."""
        source = TEMPLATE.read_text()
        css = (PART/"static/local_upload_manager.css").read_text()
        start = source.index('<section class="lum-card lum-ae-profile-manager" data-ae-panel="profiles" hidden>')
        end = source.index('<section class="lum-card lum-ae-automation-controls"', start)
        section = source[start:end]
        context = dict(engine_instance='shortstudio', base_path='/short-uploader',
                       user={'is_admin': True},
                       csrf='offline-token', p=dict(id=2, category_name='Hania Asif', enabled=True),
                       ae=dict(engine=dict(scheduler_armed=True),
                               profiles=[dict(id=2,category_name='Hania Asif',enabled=True)]))
        rendered = Environment().from_string(section).render(**context)
        self.assertEqual(rendered.count('id="lum-ae-profile-select"'), 1)
        self.assertEqual(rendered.count('data-ae-profile-dirty'), 1)
        self.assertIn('class="lum-short-profile-toolbar"', rendered)
        self.assertIn('form="lum-ae-profile-form"', rendered)
        self.assertIn('form="lum-ae-delete-profile"', rendered)
        self.assertIn('name="action" value="disarm_scheduler"', rendered)
        self.assertIn('Disarm Scheduled Uploads', rendered)
        self.assertNotIn('Manage Schedules', rendered)
        self.assertNotIn('Pause Profile', rendered)
        self.assertNotIn('Turn Profile Off', rendered)
        self.assertNotIn('All saved profiles', rendered)
        self.assertNotIn('Indexed assets', rendered)
        self.assertNotIn('Editing ·', rendered)
        self.assertNotIn('Next run:', rendered)
        context['ae']['engine']['scheduler_armed'] = False
        unarmed = Environment().from_string(section).render(**context)
        self.assertIn('name="action" value="arm_scheduler"', unarmed)
        self.assertIn('Arm Scheduled Uploads', unarmed)
        self.assertIn('lum-short-profile-toolbar', css)
        self.assertIn('flex-wrap:nowrap', css)
        self.assertIn('profile-row=20261030-v1', (PART/"templates/base.html").read_text())



if __name__=="__main__":
    unittest.main()

import copy
import json
from pathlib import Path
import unittest
from production.shell_v2_contract import validate
from production.build_shell_v2 import build, STATIC

class ShellV2Contract(unittest.TestCase):
    def setUp(self):
        self.registry=json.loads((STATIC/'shell_v2_registry.json').read_text())
    def test_assets_match_sources(self):
        self.assertEqual(build(check=True),6)
    def test_reject_incompatible_routes_geometry_and_adapters(self):
        for key,value in [('schema',1),('geometry',{}),('shell_version',3)]:
            with self.subTest(key=key):
                bad=copy.deepcopy(self.registry);bad[key]=value
                with self.assertRaises(ValueError): validate(bad)
        for change in ({'path':'//evil.example/'},{'contract':3},{'smoke':''},{'mode':'iframe'}):
            bad=copy.deepcopy(self.registry);next(a for a in bad['apps'] if a['id']=='storage-manager').update(change)
            with self.assertRaises(ValueError): validate(bad)
    def test_native_boundaries_require_removal_condition(self):
        bad=copy.deepcopy(self.registry);next(a for a in bad['apps'] if a['mode']=='native-document')['removal_condition']=''
        with self.assertRaises(ValueError):validate(bad)
    def test_duplicate_ids_and_routes_are_blocked(self):
        bad=copy.deepcopy(self.registry);bad['apps'].append(bad['apps'][0])
        with self.assertRaises(ValueError):validate(bad)
    def test_native_user_management_verification_remains_fail_closed(self):
        from production.shell_v2_browser import fixture_html
        html = fixture_html(admin=True, user_management=True)
        self.assertIn('<h1>User Management</h1>', html)
        self.assertIn('<h2>Create user</h2>', html)
        self.assertIn('<h2>Accounts</h2>', html)
        browser = (Path(__file__).resolve().parents[1] / 'production/shell_v2_browser.py').read_text()
        self.assertIn("page.wait_for_url(expected, timeout=15000)", browser)
        self.assertIn("for heading in ('User Management', 'Create user', 'Accounts')", browser)
        self.assertIn("if observed != '/user-management':", browser)
        self.assertIn("page.reload(wait_until='domcontentloaded'", browser)
        self.assertIn("assert not writes,'unexpected account writes'", browser)

    def test_seed_contains_only_app_content(self):
        from production.shell_v2_browser import fixture_html
        html=fixture_html();seed=html.split('<template id="vps1-storage-seed">')[1].split('</template>')[0]
        self.assertNotIn('<script',seed)
        self.assertNotIn('<iframe',seed)
        self.assertNotIn('app-sidebar',seed)
        self.assertIn('id="storage-app"',seed)

class CanonicalBackendBoundaries(unittest.TestCase):
    def test_canonical_mount_checks_login_and_storage_admin_before_render(self):
        import ast
        from types import SimpleNamespace
        source=STATIC.parent/'app.py'
        tree=ast.parse(source.read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='persistent_workspace')
        function.decorator_list=[]
        module=ast.Module(body=[function],type_ignores=[])
        rendered=[]
        def login(request):
            if not request.session.get('authenticated'):raise PermissionError('login')
        def admin(request):
            if not request.session.get('is_admin'):raise PermissionError('admin')
        namespace={'Request':object,'Path':Path,'__file__':str(source),'require_login':login,'require_admin':admin,
                   'templates':SimpleNamespace(TemplateResponse=lambda *args,**kw:rendered.append(args))}
        exec(compile(module,str(source),'exec'),namespace)
        def request(path,authenticated=True,is_admin=False):
            return SimpleNamespace(url=SimpleNamespace(path=path),session={'authenticated':authenticated,'is_admin':is_admin,'username':'fixture'})
        mount=namespace['persistent_workspace']
        with self.assertRaises(PermissionError):mount(request('/',False))
        for path in ('/admin/storage','/workspace/storage/'):
            with self.assertRaises(PermissionError):mount(request(path))
        self.assertEqual(rendered,[])
        mount(request('/'));mount(request('/admin/storage',is_admin=True))
        self.assertEqual(len(rendered),2)

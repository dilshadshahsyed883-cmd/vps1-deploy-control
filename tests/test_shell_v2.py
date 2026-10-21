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
        self.assertEqual(build(check=True),4)
    def test_reject_incompatible_routes_geometry_and_adapters(self):
        for key,value in [('schema',1),('geometry',{}),('shell_version',3)]:
            with self.subTest(key=key):
                bad=copy.deepcopy(self.registry);bad[key]=value
                with self.assertRaises(ValueError): validate(bad)
        for change in ({'path':'//evil.example/'},{'contract':3},{'smoke':''},{'mode':'iframe'}):
            bad=copy.deepcopy(self.registry);bad['apps'][-1].update(change)
            with self.assertRaises(ValueError): validate(bad)
    def test_native_boundaries_require_removal_condition(self):
        bad=copy.deepcopy(self.registry);bad['apps'][0]['removal_condition']=''
        with self.assertRaises(ValueError):validate(bad)
    def test_duplicate_ids_and_routes_are_blocked(self):
        bad=copy.deepcopy(self.registry);bad['apps'].append(bad['apps'][0])
        with self.assertRaises(ValueError):validate(bad)
    def test_seed_contains_only_app_content(self):
        from production.shell_v2_browser import fixture_html
        html=fixture_html();seed=html.split('<template id="vps1-storage-seed">')[1].split('</template>')[0]
        self.assertNotIn('<script',seed)
        self.assertNotIn('<iframe',seed)
        self.assertNotIn('app-sidebar',seed)
        self.assertIn('id="storage-app"',seed)

import unittest
from rebuild.nginx_route_compat import load,report,plan,owner

class N7G3CompatibilityTests(unittest.TestCase):
    def test_every_file_scoped_route_and_duplicate_preserved(self):
        result=report(load())
        self.assertEqual(result["entries"],43)
        self.assertLess(result["distinct_paths"],43)
        self.assertFalse(result["inventory_complete"])
        self.assertFalse(result["production_switch_allowed"])
        self.assertTrue(all(not x["switch_allowed"] for x in result["routes"]))
    def test_sensitive_routes_flagged(self):
        for route in report(load())["routes"]:
            if route["websocket"] or "oauth" in route["path"] or route["path"]=="/":
                self.assertIn("PROTECT_TLS_SSO_OAUTH_CSRF_COOKIE_WEBSOCKET_BEHAVIOR",
                              route["warnings"])
    def test_storage_manager_no_live_route(self):
        p=plan("vps-storage-manager",load())
        self.assertEqual(p["affected_entries"],0)
        self.assertEqual(p["action"],"SHADOW_ONLY_NO_ROUTE_CHANGE")
    def test_ownership_and_offline_guard(self):
        self.assertEqual(owner("/auto-caption/api/"),"auto-caption-studio")
        self.assertEqual(owner("/LUM/health"),"local-upload-manager")
        self.assertEqual(owner("/script-work/viewer/websockify"),"script-work")
        with self.assertRaises(ValueError):plan("auto-script",load())

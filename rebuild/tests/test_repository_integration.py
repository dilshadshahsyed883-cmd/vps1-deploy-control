"""Repository-only gates: never connect to or mutate the VPS.

Check the active source registry, historical snapshot provenance, offline source,
website shell boundaries, and regenerated SHA256 release manifests.  Historical
Agent source snapshots are evidence of capture, not a current release lockfile.
"""
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from rebuild import builder, website_shell

ROOT = Path(__file__).resolve().parents[2]
HEX256 = re.compile(r"[0-9a-f]{64}\Z")


def document(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def checked_relative(path):
    assert isinstance(path, str) and path
    p = PurePosixPath(path)
    assert not p.is_absolute() and ".." not in p.parts and "." not in p.parts
    assert str(p) == path


class RepositorySourceOfTruthContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.groups = document("rebuild/groups.json")
        cls.profiles = document("rebuild/install-profiles.json")["groups"]
        cls.active = [x["id"] for x in cls.groups["groups"]
                      if x.get("deployment_mode", "active") == "active"]

    def test_one_registry_ten_independent_active_apps(self):
        registered = [x["id"] for x in self.groups["groups"]]
        self.assertEqual(len(registered), 11)
        self.assertEqual(len(set(registered)), 11)
        self.assertEqual(len(self.active), 10)
        self.assertEqual(set(registered), set(self.profiles))
        self.assertEqual(set(registered) - set(self.active), {"auto-script"})
        self.assertNotIn("algrow-voice", registered)
        self.assertEqual(self.groups["active_application_target"], 10)
        for item in self.groups["groups"]:
            self.assertTrue(item["lock"])
            self.assertEqual(len(item["needs"]), len(set(item["needs"])))
            self.assertTrue(set(item["needs"]).issubset(set(registered) - {item["id"]}))
            self.assertTrue((ROOT / "apps" / item["id"]).is_dir())

    def test_auto_script_remains_source_only_and_offline(self):
        group = next(x for x in self.groups["groups"] if x["id"] == "auto-script")
        profile = self.profiles["auto-script"]
        self.assertTrue(group["parallel_owner"])
        self.assertEqual(group["deployment_mode"], "offline")
        self.assertEqual(profile["deployment_mode"], "offline")
        self.assertEqual(profile["services"], [])
        self.assertEqual(self.groups["offline_applications"], ["auto-script"])
        self.assertNotIn("auto-script", builder.ordered_groups())
        for flag in ("production_deploy_enabled", "fresh_install_enabled",
                     "automatic_database_migrations", "legacy_database_import",
                     "legacy_queue_import", "legacy_media_import"):
            self.assertIs(self.groups[flag], False, flag)

    def test_website_shell_owns_navigation_not_application_ui(self):
        manifest = website_shell.catalog(self.groups, {"groups": self.profiles})
        self.assertEqual({x["id"] for x in manifest["apps"]}, set(self.active))
        self.assertNotIn("auto-script", {x["id"] for x in manifest["apps"]})
        # Installation profiles list desired services, never confirmed public routes.
        # Only independently verified Nginx GitOps entries enable navigation.
        verified_routes = {entry["id"] for entry in
                           document("infrastructure/registry/apps.json")["apps"]}
        for item in manifest["apps"]:
            self.assertEqual(item["path"], "/" + item["id"] + "/")
            self.assertIs(item["runtime_verified"], False)
            self.assertEqual(item["route_configured"], item["id"] in verified_routes)
        markup = (ROOT / "rebuild/shell/index.html").read_text()
        client = (ROOT / "rebuild/shell/assets/shell.js").read_text()
        # Native app documents own their own sidebar: no competing iframe host.
        self.assertNotIn("<iframe", markup)
        self.assertNotIn("appFrame", client)
        self.assertIn("link.href = item.path", client)
        self.assertIn("shell-assets/apps.json", client)

    def test_phase1_github_remote_lanes_are_unconditionally_disabled(self):
        # Merely setting GitHub vars cannot open any automatic VPS connection.
        for workflow, job in (
            ("fresh-vps-source-of-truth.yml", "auto-stage"),
            ("vps1-staging-drift.yml", "audit"),
        ):
            with self.subTest(workflow=workflow):
                body = (ROOT / ".github/workflows" / workflow).read_text()
                section = body.split("\n  " + job + ":\n", 1)[1]
                condition = section.split("\n    runs-on:", 1)[0]
                self.assertRegex(condition, r"(?m)^    if: >-\n      false &&\n")
                self.assertIn("github.ref == 'refs/heads/main'", condition)

    def test_nginx_requires_separate_host_route_adoption(self):
        registry = document("infrastructure/registry/apps.json")
        self.assertEqual(registry["domain"], "app.scenebuilder.online")
        # Empty by design until actual VPS/Agent discovery; never fabricate ports.
        self.assertEqual(registry["apps"], [])

    def test_historical_agent_manifest_inventory_and_sha_format(self):
        capture = document("rebuild/snapshots/live-source-manifest.json")
        self.assertEqual(capture["schema"], "vps1-source-only-v1")
        self.assertEqual(set(capture["captured_groups"]), set(self.active))
        self.assertEqual(set(capture["groups"]), set(self.active))
        self.assertEqual(capture["excluded_group"], "algrow-voice")
        for field in ("production_modified", "database_migrated",
                      "queues_migrated", "uploads_migrated"):
            self.assertIs(capture[field], False)
        count = 0
        for app in self.active:
            group = capture["groups"][app]
            self.assertTrue(group["files"], app)
            seen = set()
            for entry in group["files"]:
                checked_relative(entry["path"])
                self.assertNotIn(entry["path"], seen)
                seen.add(entry["path"])
                self.assertRegex(entry["sha256"], HEX256)
                self.assertIsInstance(entry["size"], int)
                self.assertGreaterEqual(entry["size"], 0)
                count += 1
        self.assertEqual(count, capture["source_files"])
        # Do not pretend this historical snapshot attests later edits.

    def test_targeted_lum_source_hashes(self):
        capture = document("rebuild/snapshots/targeted-source-manifest.json")
        self.assertEqual(capture["runtime_imported"], False)
        self.assertEqual(capture["source"], "AGENT1_TARGETED_LUM_DB_PYTHON")
        self.assertEqual(len(capture["modules"]), 24)
        paths = set()
        for entry in capture["modules"]:
            relative = entry["path"]
            checked_relative(relative)
            self.assertTrue(relative.startswith("apps/local-upload-manager/"))
            self.assertNotIn(relative, paths)
            paths.add(relative)
            self.assertRegex(entry["sha256"], HEX256)
            source = ROOT / relative
            self.assertTrue(source.is_file(), relative)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                             entry["sha256"], relative)

    def test_auto_script_source_manifest_hashes(self):
        capture = document("apps/auto-script/SOURCE_SHA256.json")
        self.assertIs(capture["runtime_data_imported"], False)
        self.assertEqual(len(capture["files"]), 4)
        paths = set()
        for entry in capture["files"]:
            checked_relative(entry["path"])
            self.assertNotIn(entry["path"], paths)
            paths.add(entry["path"])
            source = ROOT / "apps/auto-script" / entry["path"]
            self.assertTrue(source.is_file())
            raw = source.read_bytes()
            self.assertEqual(len(raw), entry["bytes"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])

    def test_all_ten_rebuildable_manifest_hashes_match_source(self):
        # Generate current per-app SHA256 manifests directly from the checkout.
        # Do not compare a fresh Git checkout with the historical Agent snapshot.
        self.assertTrue(builder.state()["ready"])
        revision = "b" * 40
        with tempfile.TemporaryDirectory(prefix="repo-contract-") as tmp:
            for app in self.active:
                with self.subTest(app=app):
                    manifest = builder.artifact(app, revision, Path(tmp) / app)
                    metadata = json.loads(manifest.read_text())
                    self.assertEqual(metadata["commit_sha"], revision)
                    self.assertEqual(metadata["app"], app)
                    self.assertGreater(len(metadata["files"]), 0)
                    self.assertRegex(metadata["sha256"], HEX256)
                    self.assertEqual(builder.verify(manifest), app)
                    # Every emitted checksum must attest the current checkout.
                    for entry in metadata["files"]:
                        checked_relative(entry["path"])
                        self.assertEqual(
                            hashlib.sha256((ROOT / "apps" / app /
                                            entry["path"]).read_bytes()).hexdigest(),
                            entry["sha256"], (app, entry["path"]))


if __name__ == "__main__":
    unittest.main()

"""Focused offline regressions for the October ACS/Storage/Dashboard repair."""
from pathlib import Path
import ast
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCENE = ROOT / "apps/scene-builder/part01"
ACS = ROOT / "apps/auto-caption-studio/part01"


class IntegrationRoutingTests(unittest.TestCase):
    def test_acs_live_uses_production_prefix(self):
        html = (ACS / "web_frontend/templates/live.html").read_text()
        self.assertIn("location.pathname.startsWith('/auto-caption-studio/')", html)
        self.assertIn("/api/live", html)
        self.assertIn("/api/live-audit", html)

    def test_queue_native_live_is_visible_and_old_iframe_css_is_gone(self):
        # Regression: CSS for the retired iframe hid *all* Queue children.
        html = (ACS / "web_backend/acs_studio.html").read_text()
        live = (ACS / "web_frontend/templates/live.html").read_text()
        self.assertIn('<section class="page active acs-live-reference-v1" id="queue">', html)
        self.assertIn('id="acs-native-live"', html)
        self.assertIn('id="workers"', html)
        self.assertIn('id="batchRows"', html)
        self.assertIn(
            '#queue.acs-live-reference-v1 > :not(#acs-native-live){display:none!important}',
            html)
        self.assertIn(
            '#queue.acs-live-reference-v1 > #acs-native-live{display:block!important}',
            html)
        self.assertNotIn(
            '#queue.acs-live-reference-v1 > :not(.acs-live-embed-v1)', html)
        self.assertNotIn('<iframe class="acs-live-embed-v1"', html)
        self.assertIn("['render','Renderer',", live)
        self.assertNotIn("['voice','Algrow',", live)
        self.assertIn('id="acs-native-live-script"', html)

    @unittest.skipUnless(shutil.which("node"), "Node.js required for Queue DOM contract")
    def test_queue_native_worker_cards_render_without_algrow(self):
        # Simulate the actual same-document worker-card renderer with a DOM stub.
        html = (ACS / "web_backend/acs_studio.html").read_text()
        marker = 'function renderWorkers(d){'
        source = marker + html.split(marker, 1)[1].split('async function setPipeline', 1)[0]
        script = """
        const vm=require('node:vm');
        const source=process.argv[1];
        const workers={innerHTML:''};
        const context={
          document:{querySelector:selector=>{
            if(selector!=='#workers')throw new Error('Unexpected selector '+selector);
            return workers;
          },querySelectorAll:()=>[]},
          currentLine:()=>'<div>Idle</div>',
          esc:x=>String(x),controlBusy:''
        };
        const d={can_control:false,
          pipeline_counts:{renderer_queue:2,compression_queue:1,upload_queue:3,algrow_queue:7},
          workers:{render:{state:'idle'},compress:{state:'idle'},upload:{state:'idle'}}};
        vm.runInNewContext(source+';renderWorkers(d)',{...context,d},{timeout:1000});
        for(const name of ['Renderer','Compressor','Drive uploader']){
          if(!workers.innerHTML.includes(name))throw new Error(name+' card missing');
        }
        if(workers.innerHTML.includes('Algrow'))throw new Error('Algrow card returned');
        if((workers.innerHTML.match(/<article /g)||[]).length!==3)
          throw new Error('Expected exactly three visible worker cards');
        console.log('ACS_NATIVE_QUEUE_WORKER_CARDS=PASS');
        """
        result = subprocess.run(["node", "-e", script, source],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACS_NATIVE_QUEUE_WORKER_CARDS=PASS", result.stdout)

    def test_audio_engine_never_loads_broken_acs_root(self):
        html = (ACS / "web_backend/acs_studio.html").read_text()
        self.assertNotIn('src="/acs-audio-engine/"', html)
        self.assertNotIn("fetch('/acs-audio-engine/health'", html)
        self.assertIn("audio-engine-fallback", html)
        self.assertIn("fetch('/audio-engine/app'", html)
        self.assertIn("frame.src = '/audio-engine/app'", html)
        self.assertIn('id="internalAudioEngineCard"', html)


    @unittest.skipUnless(shutil.which("node"), "Node.js required for browser DOM contract")
    def test_voice_profiles_cannot_replace_the_audio_engine_card(self):
        html = (ACS / "web_backend/acs_studio.html").read_text()
        self.assertEqual(html.count('id="acsVoiceProfilesList"'), 1)
        self.assertEqual(html.count('id="internalAudioEngineCard"'), 1)
        self.assertLess(html.index('id="acsVoiceProfilesList"'),
                        html.index('id="internalAudioEngineCard"'))
        fn = "function renderVoiceProfiles(){" + html.split(
            "function renderVoiceProfiles(){", 1)[1].split(
                "function updateVoiceSelects(){", 1)[0]
        self.assertIn("document.getElementById('acsVoiceProfilesList')", fn)
        self.assertNotIn("querySelector('.grid.g2 + .card')", fn)
        script = """
        const vm = require('node:vm');
        const source = process.argv[1];
        const cards = {
          acsVoiceProfilesList: {innerHTML:'BEFORE',querySelectorAll:()=>[]},
          internalAudioEngineCard: {innerHTML:'ENGINE_CONTROLS_INTACT'}
        };
        const context = {
          document:{getElementById:id=>{
            if (!(id in cards)) throw new Error('unexpected target '+id);
            return cards[id];
          }},
          appState:{voices:[{id:'voice',name:'Test Voice',language:'en',
              model:'example',stability:50,similarity:50,enabled:true}]},
          esc:x=>String(x),updateVoiceSelects:()=>{}
        };
        vm.runInNewContext(source+'; renderVoiceProfiles();',context,
          {timeout:1000});
        if (!cards.acsVoiceProfilesList.innerHTML.includes('Test Voice'))
          throw new Error('Profile list did not render');
        if (cards.internalAudioEngineCard.innerHTML!=='ENGINE_CONTROLS_INTACT')
          throw new Error('Audio Engine controls were erased');
        console.log('ACS_PROFILE_RENDER_PRESERVES_AUDIO_ENGINE=PASS');
        """
        result = subprocess.run(
            ["node", "-e", script, fn], capture_output=True, text=True, timeout=8
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACS_PROFILE_RENDER_PRESERVES_AUDIO_ENGINE=PASS", result.stdout)

    def test_acs_audio_card_release_is_acs_only(self):
        workflow = (ROOT / ".github/workflows/vps1-acs-audio-card-release.yml").read_text()
        self.assertIn("[vps1-acs-audio-card-release]", workflow)
        self.assertIn("install $SOURCE_SHA auto-caption-studio", workflow)
        self.assertNotIn("install $SOURCE_SHA local-upload-manager", workflow)
        self.assertNotIn("install $SOURCE_SHA scene-builder", workflow)
        self.assertIn("ACS_AUDIO_ENGINE_CARD_SOURCE=PASS", workflow)
        self.assertIn("AUDIO_SYNTHESIS_UAT=NOT_PROVEN", workflow)

    def test_dashboard_actual_service_names_and_no_fake_failures(self):
        path = SCENE / "app.py"
        code = path.read_text()
        ast.parse(code, filename=str(path))
        self.assertIn("vps1-production-auto-caption-studio.service", code)
        self.assertIn("vps1-production-lum-worker.service", code)
        self.assertIn("for port in (8144, 8100):", code)
        self.assertIn("A disconnected API is not a failed media project.", code)
        self.assertIn("Backend unconfigured does not mean a cleanup operation failed.", code)
        js = (SCENE / "static/dashboard_control_center.js").read_text()
        self.assertIn("status=!online?'Offline'", js)

    def test_dashboard_omits_algrow_voice_card_without_touching_voice_studio(self):
        app = (SCENE / "app.py").read_text()
        js = (SCENE / "static/dashboard_control_center.js").read_text()
        self.assertNotIn('{"name": "Algrow Voice", "available"', app)
        self.assertNotIn('names.add("Algrow Voice")', app)
        self.assertIn("a.name!=='Algrow Voice'", js)
        self.assertIn('"Voice Studio","Voice API"', app)

    def test_storage_manager_is_read_only_and_legacy_controls_are_absent(self):
        manager = (ROOT / "apps/vps-storage-manager/part01/storage_manager.py").read_text()
        scene = (SCENE / "app.py").read_text()
        template = (SCENE / "templates/storage_manager.html").read_text()
        js = (SCENE / "static/storage_manager.js").read_text()
        self.assertIn('if path == "/v1/capabilities"', manager)
        self.assertIn('os.getenv("VPS1_RUNTIME_CANDIDATE") == "1"', manager)
        self.assertIn('can_modify": not read_only', manager)
        self.assertIn('@app.get("/api/admin/storage/capabilities")', scene)
        for required in ('id="refresh-cleanup-status"', 'id="gc-next-run"',
                         'id="deployment-gc-state"', 'id="gc-last-freed"'):
            self.assertIn(required, template)
        for retired in ('id="full-production-clean"', 'id="run-scan"',
                        'id="policy-form"', 'id="reclaim-button"'):
            self.assertNotIn(retired, template)
        self.assertIn("data.read_only !== true || data.partial !== true", js)
        self.assertNotIn("method:'POST'", js)

    def test_storage_fallback_is_metrics_only(self):
        js = (SCENE / "static/storage_manager.js").read_text()
        self.assertIn("fetch('/api/dashboard/v1'", js)
        self.assertIn("method:'GET'", js)
        for forbidden in ("/api/admin/storage/cleanup", "/api/admin/storage/full-clean",
                          "/api/admin/storage/policy", "Full Production Clean"):
            self.assertNotIn(forbidden, js)

    def test_guarded_release_contains_both_updated_pages(self):
        from production.scene_uploader_ui_release import FILES
        for rel in ("app.py", "templates/dashboard.html", "templates/storage_manager.html",
                    "static/dashboard_control_center.js", "static/storage_manager.js"):
            self.assertIn(rel, FILES)


    def _isolated_storage_capabilities(self, backend):
        """Execute the actual Scene endpoint code with inert offline dependencies."""
        source = ast.parse((SCENE / "app.py").read_text())
        node = next(x for x in source.body
                    if isinstance(x, ast.FunctionDef) and x.name == "storage_capabilities")
        node.decorator_list = []
        isolated = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
        class ProbeError(Exception):
            def __init__(self, status_code, detail="probe error"):
                super().__init__(detail)
                self.status_code = status_code
        calls = []
        def require_admin(_request):
            calls.append("admin")
        def request(method, path, timeout=5):
            calls.append((method, path))
            return backend(method, path, ProbeError)
        scope = {"Request": object, "HTTPException": ProbeError,
                 "require_admin": require_admin, "storage_manager_request": request}
        exec(compile(isolated, "isolated-storage-capability", "exec"), scope)
        return scope["storage_capabilities"], calls, ProbeError

    def test_storage_legacy_capability_404_falls_back_only_to_authenticated_metrics(self):
        def backend(_method, path, error):
            if path == "/v1/capabilities":
                raise error(404)
            self.assertEqual(path, "/v1/metrics")
            return {"disk": {"free": 4096}}
        endpoint, calls, _ = self._isolated_storage_capabilities(backend)
        self.assertEqual(endpoint(object()),
                         {"ok": True, "read_only": True, "can_modify": False,
                          "legacy_candidate": True})
        self.assertEqual(calls, ["admin", ("GET", "/v1/capabilities"),
                                 ("GET", "/v1/metrics")])

    def test_storage_legacy_fallback_rejects_other_errors_and_invalid_metrics(self):
        for status in (401, 403, 500, 503):
            def backend(_method, _path, error):
                raise error(status)
            endpoint, calls, err = self._isolated_storage_capabilities(backend)
            with self.assertRaises(err) as caught:
                endpoint(object())
            self.assertEqual(caught.exception.status_code, status)
            self.assertEqual(calls, ["admin", ("GET", "/v1/capabilities")])
        def invalid(_method, path, error):
            return {"ok": True} if path == "/v1/metrics" else (_ for _ in ()).throw(error(404))
        endpoint, calls, err = self._isolated_storage_capabilities(invalid)
        with self.assertRaises(err) as caught:
            endpoint(object())
        self.assertEqual(caught.exception.status_code, 502)

    def test_storage_modern_capabilities_unchanged(self):
        def backend(_method, _path, _error):
            return {"ok": True, "read_only": True, "can_modify": False}
        endpoint, calls, _ = self._isolated_storage_capabilities(backend)
        self.assertEqual(endpoint(object()), {"ok": True, "read_only": True,
                                              "can_modify": False})
        self.assertEqual(calls, ["admin", ("GET", "/v1/capabilities")])



if __name__ == "__main__":
    unittest.main()

"""Director V25 frontend/header contract: V25 TXT chooses preset & project safely."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "apps/scene-builder/part01/templates/queue.html"


class DirectorV25AutosyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE.read_text(encoding="utf-8")

    def test_txt_preset_and_name_auto_sync_without_classic_override(self):
        code = self.source
        self.assertIn("SCENE_PLAN_VERSION", code)
        self.assertIn("parseDirectorV25Header(raw)", code)
        self.assertIn("source.match(", code)
        self.assertIn("presetSelect.value=header.preset;", code)
        self.assertIn("nameInput.value=header.project;", code)
        self.assertIn("directorProcessor.value='director_preview';", code)
        self.assertIn("void syncDirectorV25FromFile(f)", code)
        self.assertIn("const option=[...presetSelect.options].find(o=>o.value===header.preset);", code)
        self.assertIn("if(presetSelect.value!==directorHeader.preset)", code)
        self.assertIn("if(name!==directorHeader.project)", code)
        self.assertIn("director_processor',processor==='director_preview'?'director':'classic'", code)
        self.assertIn("option value=\"classic\" selected", code)

    @unittest.skipUnless(shutil.which("node"), "node is required for executing Queue JavaScript parser")
    def test_real_js_extracts_v25_header_crlf_bom_and_ignores_classic(self):
        match = re.search(
            r"function parseDirectorV25Header\(raw\)\{[\s\S]*?\n\}",
            self.source,
        )
        self.assertIsNotNone(match, "V25 header parser must be exported as a standalone function")
        script = (
            match.group(0)
            + "\nconst fs=require('fs');"
            + "const samples=JSON.parse(fs.readFileSync(0,'utf8'));"
            + "process.stdout.write(JSON.stringify(samples.map(x=>{"
            + "try { return {result:parseDirectorV25Header(x)};}"
            + "catch(e){return {error:e.message}}"
            + "})));"
        )
        valid = (
            "SCENE_PLAN_VERSION=2\n\n[PROJECT]\n"
            "mode=director\nname=Eala_Tokyo_V25\n"
            "base_preset=cinematic_documentary\nvalidation=strict\n\n"
            "[OUTPUT]\nresolution=1080p\n"
        )
        spaced = valid.replace("base_preset=cinematic_documentary", "base_preset = cinematic_documentary")
        samples = [
            valid,
            "\ufeff" + valid.replace("\n", "\r\n"),
            spaced,
            "SCENE_001|Narration|primary_keyword=tennis",
            "SCENE_PLAN_VERSION=2\n[OUTPUT]\nresolution=1080p\n",
            valid.replace("base_preset=cinematic_documentary\n", ""),
        ]
        result = subprocess.run(
            ["node", "-e", script], input=json.dumps(samples),
            capture_output=True, text=True, check=True, timeout=20,
        )
        rows = json.loads(result.stdout)
        for item in rows[:3]:
            self.assertEqual(item["result"]["preset"], "cinematic_documentary")
            self.assertEqual(item["result"]["project"], "Eala_Tokyo_V25")
        self.assertIsNone(rows[3]["result"])
        self.assertIn("[PROJECT]", rows[4]["error"])
        self.assertIn("base_preset", rows[5]["error"])


if __name__ == "__main__":
    unittest.main()

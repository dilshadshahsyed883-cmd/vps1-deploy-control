"""UI-only regression contract for the three Voice Studio fixes of 2026-10-21.

No service, database, queue job, Drive upload or worker is needed to run these.
"""
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps/voice-studio/part01/templates/voice_studio.html"
HTML = SOURCE.read_text(encoding="utf-8")


class QueueTableParser(HTMLParser):
    def __init__(self, queue_id):
        super().__init__()
        self.target = queue_id
        self.inside = False
        self.depth = 0
        self.headers = []
        self.bodies = []
        self.controls = 0
        self.refreshes = 0
        self.wrappers = 0
        self._th = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if not self.inside and a.get("id") == self.target:
            self.inside = True
            self.depth = 0
        elif self.inside and tag == "div":
            self.depth += 1
        if not self.inside:
            return
        if tag == "th":
            self._th = True
        if tag == "tbody":
            self.bodies.append(a.get("id"))
        if "vsq-tablewrap" in a.get("class", "").split():
            self.wrappers += 1
        if tag == "button" and a.get("id") in ("vskRefreshQueue", "vseRefresh"):
            self.refreshes += 1

    def handle_endtag(self, tag):
        if not self.inside:
            return
        if tag == "th":
            self._th = False
        if tag == "div":
            if self.depth == 0:
                self.inside = False
            else:
                self.depth -= 1

    def handle_data(self, data):
        if self.inside and self._th:
            self.headers.append(data.strip())


class VoiceUI20261021Regression(unittest.TestCase):
    def test_kokoro_right_side_controls_column(self):
        table = QueueTableParser("vskQueue")
        table.feed(HTML)
        self.assertEqual(table.headers, ["File / job", "Status", "Audio", "Controls"])
        self.assertEqual(table.bodies, ["vskQueueRows"])
        self.assertEqual(table.wrappers, 1)
        self.assertEqual(table.refreshes, 1)
        self.assertIn("d.className='vsq-controls'", HTML)
        self.assertIn("tr.append(a,b,c,d);rows.append(tr)", HTML)
        self.assertIn("d.append(button)", HTML)
        self.assertIn("td.colSpan=4;td.textContent='No jobs'", HTML)
        self.assertNotIn("c.append(' ',button)", HTML)

    def test_edge_kokoro_parity_and_edge_specific_controls(self):
        table = QueueTableParser("vseQueue")
        table.feed(HTML)
        self.assertEqual(table.headers, ["File", "Job status", "Audio", "Controls"])
        self.assertEqual(table.bodies, ["vseQueueRows"])
        self.assertEqual(table.wrappers, 1)
        self.assertEqual(table.refreshes, 1)
        self.assertIn("ctrl.className='vsq-controls'", HTML)
        self.assertIn("for(const act of j.allowed_actions||[])ctrl.append(b(", HTML)
        edge_js = HTML.split('<script id="voiceStudioEdgeEngineV1Script">', 1)[1].split('</script>', 1)[0]
        self.assertIn("const audioFormats=['mp3','wav'];", edge_js)
        self.assertIn("Google Drive: Open uploaded file", edge_js)
        self.assertNotIn("['mp3','wav','txt','srt']", edge_js)
        self.assertIn("tts/api/edge", HTML)

    def test_omni_voice_clone_and_queue_isolated_css(self):
        css = re.search(r'<style id="voiceStudioUiParity20261021">(.*?)</style>', HTML, re.S)
        self.assertIsNotNone(css)
        sheet = css.group(1)
        self.assertNotIn("{#", sheet, "CSS must not look like a Jinja comment")
        for rule in ("#page-clone .field", "#page-clone .cards",
                     "#page-clone .card.pad", "#page-queue .tablewrap",
                     "#vskQueue .vsq-controls", "#vseQueue .vsq-controls",
                     "@media(max-width:760px)", ":focus-visible"):
            with self.subTest(rule=rule):
                self.assertIn(rule, sheet)
        for forbidden in (".vps3-main-sidebar{", ".vps1-main-sidebar{",
                          "body{", ".vps-shell{", ".enginebar{"):
            self.assertNotIn(forbidden, sheet)

    def test_actions_confirmation_and_drive_receipts_preserved(self):
        k = HTML.split('<script id="voiceStudioDualEngineV1Script">', 1)[1].split('</script>', 1)[0]
        e = HTML.split('<script id="voiceStudioEdgeEngineV1Script">', 1)[1].split('</script>', 1)[0]
        for label, block in (("Kokoro", k), ("Edge", e)):
            with self.subTest(engine=label):
                self.assertIn("j.allowed_actions||[]", block)
                self.assertIn("action" if label == "Kokoro" else "act", block)
                self.assertIn("confirm('Hide this finished job from history?", block)
                self.assertIn("delivery/retry", block)
                self.assertIn("j.drive_verified_at", block)
                self.assertIn("j.drive_file_id", block)
                self.assertIn("drive.google.com/file/d/", block)
        self.assertIn("root+'/'+'jobs/'+jid+'/'+act", e)
        self.assertIn("apiRoot+'/'+'jobs/'+encodeURIComponent(jid)+'/'+action", k)
        self.assertIn("c.append(line)", k)  # audio/Drive links stay in Audio
        self.assertIn("d.append(button)", k)  # controls move, handlers unchanged

    @unittest.skipUnless(shutil.which("node"), "Node.js not present")
    def test_unchanged_kokoro_edge_javascript_still_parses(self):
        for marker in ("voiceStudioDualEngineV1Script", "voiceStudioEdgeEngineV1Script"):
            with self.subTest(marker=marker):
                script = HTML.split('<script id="' + marker + '">', 1)[1].split('</script>', 1)[0]
                result = subprocess.run(
                    ["node", "--check"], input=script, text=True,
                    capture_output=True, timeout=15, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

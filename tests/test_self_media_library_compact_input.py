"""Owner-approved compact SML Input UI contract (SML-INPUT-UI-001)."""
import unittest
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

SML=Path(__file__).resolve().parents[1]/"apps/self-media-library/part01/static"
class Page(HTMLParser):
    def __init__(self): super().__init__(); self.ids={}; self.tags=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs); self.tags.append((tag,a))
        if a.get("id"): self.ids[a["id"]]=self.ids.get(a["id"],0)+1
class CompactInput(unittest.TestCase):
    def test_all_existing_input_action_ids_preserved_once(self):
        html=(SML/"index.html").read_text(); page=Page();page.feed(html)
        for item in ("page-sources","sourceFile","dropPanel","chooseSource","sourceUploadResult","sourcesList","refreshSources","imageFiles","uploadImages","imageUploadResult","aiFolderInput","aiInputStatus","aiBatchUrls","aiBatchRights","aiBatchImport","importSource","importValue","importRights","importProxy","submitImport","importHistory"):
            self.assertEqual(page.ids.get(item),1,item)
        self.assertEqual(html.count('class="sml-input-grid"'),1)
        self.assertEqual(html.count('data-sml-upload='),4)
        self.assertIn('data-sml-upload="processed"',html)
        self.assertIn('id="sml-upload-processed" role="tabpanel" hidden',html)
        self.assertEqual(html.count('data-sml-import='),1)
        self.assertIn('id="smlBatchDetails"',html)
        self.assertIn('id="sml-upload-video" role="tabpanel"',html)
        self.assertIn('id="sml-upload-image" role="tabpanel" hidden',html)
        self.assertIn('id="sml-upload-folder" role="tabpanel" hidden',html)
    def test_full_width_youtube_queue_is_sibling_of_import_form(self):
        html=(SML/"index.html").read_text()
        css=(SML/"sml_shared_theme.css").read_text()
        self.assertEqual(html.count('class="panel sml-import-queue sml-import-wide"'),1)
        self.assertEqual(html.count('class="panel sml-batch-card sml-batch-wide"'),1)
        self.assertIn('id="importProxyState"',html)
        self.assertIn("SML's private profiles are separate from Video Merger.",html)
        grid=html.split('class="sml-input-grid"',1)[1].split('class="panel sml-input-history"',1)[0]
        self.assertRegex(grid,r'</article>\s*</div>\s*<section class="panel sml-import-queue sml-import-wide"')
        self.assertLess(grid.index('class="panel sml-import-queue sml-import-wide"'),
                        grid.index('class="panel sml-batch-card sml-batch-wide"'))
        self.assertIn('.sml-input-grid > .sml-import-wide',css)
        self.assertIn('grid-column:1 / -1',css)
        self.assertIn('grid-template-columns:repeat(2,minmax(0,1fr))',css)
        self.assertIn('@media(max-width:1100px)',css)
        self.assertIn('SML-QUEUE-WIDE-001',css)

    def test_ui_switching_without_backend_changes(self):
        js=(SML/"app.js").read_text(); css=(SML/"sml_shared_theme.css").read_text()
        for token in ("setSmlUploadTab","setSmlImportSource","smlImageDrop","smlChooseFolder","updateInputEligibility","/api/v1/images/upload","/api/v1/media/imports","/api/sources/upload"):
            self.assertIn(token,js)
        for token in (".sml-input-grid","[hidden]{display:none!important}","@media(max-width:950px)","@media(max-width:760px)"):
            self.assertIn(token,css)
        self.assertNotIn("localStorage",js[js.index("// Owner-approved compact SML Input controls"):js.index("const sourceFile=$('#sourceFile')")])

    def test_process_queue_manual_start_control(self):
        js=(SML/"app.js").read_text()
        self.assertIn("Awaiting start",js)
        if not shutil.which("node"): return
        # Evaluate just the pure row renderer, avoiding network/browser side effects.
        row_fn="function sourceRow("+js.split("function sourceRow(",1)[1].split("\nfunction bindSourceActions(",1)[0]
        harness=(
            "const esc=v=>String(v??'');const fmtTime=v=>String(v);const fmtSize=v=>String(v);\n"
            +row_fn+
            "\nconst row={id:'SRC-Q',filename:'Alexandra Eala.mp4',size_bytes:1024,status:'queued',stage:'queued',clip_count:0,progress:0};"
            "const queued=sourceRow(row,true);"
            "if(!queued.includes('Awaiting start')||!queued.includes('data-process=\\\"SRC-Q\\\"'))process.exit(2);"
            "const running=sourceRow({...row,status:'processing',stage:'detecting_shots'},true);"
            "if(running.includes('data-process='))process.exit(3);"
            "const complete=sourceRow({...row,status:'completed',clip_count:4},true);"
            "if(complete.includes('data-process='))process.exit(4);"
        )
        subprocess.run(["node","-e",harness],check=True,capture_output=True,timeout=10)

if __name__=="__main__": unittest.main()

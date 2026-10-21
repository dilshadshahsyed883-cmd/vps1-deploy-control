"""Owner-approved compact SML Input UI contract (SML-INPUT-UI-001)."""
import unittest
from html.parser import HTMLParser
from pathlib import Path

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
        self.assertEqual(html.count('data-sml-upload='),3)
        self.assertEqual(html.count('data-sml-import='),2)
        self.assertIn('id="smlBatchDetails"',html)
        self.assertIn('id="sml-upload-video" role="tabpanel"',html)
        self.assertIn('id="sml-upload-image" role="tabpanel" hidden',html)
        self.assertIn('id="sml-upload-folder" role="tabpanel" hidden',html)
    def test_ui_switching_without_backend_changes(self):
        js=(SML/"app.js").read_text(); css=(SML/"sml_shared_theme.css").read_text()
        for token in ("setSmlUploadTab","setSmlImportSource","smlImageDrop","smlChooseFolder","updateInputEligibility","/api/v1/images/upload","/api/v1/media/imports","/api/sources/upload"):
            self.assertIn(token,js)
        for token in (".sml-input-grid","[hidden]{display:none!important}","@media(max-width:950px)","@media(max-width:760px)"):
            self.assertIn(token,css)
        self.assertNotIn("localStorage",js[js.index("// Owner-approved compact SML Input controls"):js.index("const sourceFile=$('#sourceFile')")])
if __name__=="__main__": unittest.main()

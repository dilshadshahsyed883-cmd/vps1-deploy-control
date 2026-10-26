"""SML Library & Preview subtab contracts; preserve original media APIs."""
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "apps/self-media-library/part01"
TABS = ("media", "ai", "people", "face", "duplicates", "trash")


class LibraryMarkup(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.ids = []
        self.ancestry = {}
        self.tabs = {}
        self.panes = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ident = attrs.get("id")
        if ident:
            self.ids.append(ident)
            self.ancestry[ident] = [x[1] for x in self.stack if x[1]]
        if "data-library-tab" in attrs:
            self.tabs[attrs["data-library-tab"]] = attrs
        if "data-library-pane" in attrs:
            self.panes[attrs["data-library-pane"]] = attrs
        if tag not in self.VOID:
            self.stack.append((tag, ident))

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


class LibrarySubtabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "static/index.html").read_text()
        cls.js = (ROOT / "static/app.js").read_text()
        cls.faces = (ROOT / "static/face_people.js").read_text()
        cls.markup = LibraryMarkup()
        cls.markup.feed(cls.html)

    def test_six_exclusive_accessible_subtabs_and_media_switch(self):
        m = self.markup
        self.assertEqual(tuple(m.tabs), TABS)
        self.assertEqual(tuple(m.panes), TABS)
        self.assertEqual(m.tabs["media"]["aria-selected"], "true")
        for name in TABS:
            self.assertEqual(m.tabs[name]["aria-controls"], "smlLibraryPane" + name)
            self.assertEqual(m.panes[name]["role"], "tabpanel")
            if name != "media":
                self.assertIn("hidden", m.panes[name])
        self.assertEqual(self.html.count('data-library-media="videos"'), 1)
        self.assertEqual(self.html.count('data-library-media="images"'), 1)
        self.assertIn('id="smlMediaPaneImages"', self.html)
        self.assertIn('data-media-pane="images" role="tabpanel" aria-labelledby="smlLibraryMediaImages" hidden', self.html)
        self.assertIn('data-library-pane][hidden]', (ROOT / "static/library_subtabs.css").read_text())

    def test_previous_controls_remain_unique_in_the_right_pane(self):
        m = self.markup
        self.assertEqual(len(m.ids), len(set(m.ids)), "duplicate ID would break document.querySelector handlers")
        groups = {
            "media": ("clipSearch", "libraryGrid", "loadMore", "bulkBar",
                      "imagesGrid", "imageEditor", "imageBulkBar",
                      "selectAllImages", "identifyImage", "localFrom", "localTo"),
            "ai": ("aiSearchForm", "aiQuery", "aiResults", "aiSaveSearch",
                   "aiReference", "aiSelectedCount", "aiBulkTags"),
            "people": ("manualPeoplePanel", "manualPersonSave", "manualAssignImages",
                       "manualAssignClips", "manualPersonAssets"),
            "face": ("peoplePanel", "peopleCreate", "peoplePicker", "peopleMatches",
                     "peopleApprove", "peopleReject", "peopleUndo"),
            "duplicates": ("aiDuplicates", "aiCompare", "aiDuplicateResults"),
            "trash": ("trashGrid", "imageTrashGrid"),
        }
        for tab, ids in groups.items():
            for ident in ids:
                self.assertIn(ident, m.ancestry, ident)
                self.assertIn("smlLibraryPane" + tab, m.ancestry[ident],
                              ident + " belongs in " + tab)
        # Compatibility anchor for old links exists but no second Trash content.
        self.assertIn("page-trash", m.ids)
        self.assertEqual(self.html.count('id="trashGrid"'), 1)

    def test_navigation_and_backend_actions_not_reimplemented(self):
        js = self.js
        self.assertIn("function setLibraryTab(tab, focus=false)", js)
        self.assertIn("function setLibraryMediaTab(tab,focus=false)", js)
        self.assertIn("const trashRequest=page==='trash'", js)
        self.assertIn("if(tab==='trash') loadTrash()", js)
        self.assertIn("imageSelected.clear();updateImageBulk()", js)
        self.assertIn("data-library-tab", js)
        self.assertIn("setLibraryTab('face')", self.faces)
        self.assertIn("inspectReference(selectedImage.id)", self.faces)
        self.assertIn("/api/clips/bulk/trash", js)
        self.assertIn("/api/v1/images/bulk/trash", js)
        self.assertIn("async function loadTrash()", js)
        self.assertIn("async function loadImageTrash()", js)
        self.assertIn("id=\"aiDuplicateResults\"", self.html)


if __name__ == "__main__":
    unittest.main()

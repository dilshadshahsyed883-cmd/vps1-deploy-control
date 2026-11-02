"""Guarded Video Merger frozen Library-only installer contract tests."""
from pathlib import Path
import unittest
from production.video_merger_library_release import (
    frozen_library_overlay, LibraryReleaseBlocked
)

ROOT=Path(__file__).resolve().parents[1]
SOURCE=(ROOT/"apps/video-merger/part01/templates/index.html")
BASELINE=(ROOT/"production/video_merger_library_20261102_baseline.html")


class FrozenLibraryRelease(unittest.TestCase):
    def setUp(self):
        self.source=SOURCE.read_text(encoding="utf-8")
        self.live=BASELINE.read_text(encoding="utf-8")

    def test_overlay_installs_full_library_without_touching_other_app_sections(self):
        result=frozen_library_overlay(self.live,self.source,self.live)
        self.assertIn('id="libraryUploadDrive" type="checkbox"',result)
        self.assertIn('id="libraryPreviewDialog"',result)
        self.assertIn("function viewLibrary(id)",result)
        self.assertIn("upload_to_drive:libraryUploadDrive.checked",result)
        self.assertEqual(result.count('id="libraryUploadDrive"'),1)
        self.assertEqual(result.count('id="libraryPreviewDialog"'),1)
        for start,end in [
            ('  <div class="tabs"', '    <div id="tab-library"'),
            ('  <div id="tab-proxy"', 'const P='),
            ('function renderProxy(){', 'newMerge();')
        ]:
            self.assertIn(start,self.live)
            self.assertIn(end,self.live)
            self.assertEqual(
                result[result.index(start):result.index(end,result.index(start))],
                self.live[self.live.index(start):self.live.index(end,self.live.index(start))]
            )

    def test_overlay_is_idempotent_and_preserves_unrelated_parallel_markup(self):
        live=self.live.replace('  <div id="tab-proxy" class="tab-panel">',
                               '  <div id="tab-proxy" class="tab-panel"><div class="parallel-safe-marker">Untouched</div>',1)
        updated=frozen_library_overlay(live,self.source,self.live)
        self.assertIn('parallel-safe-marker',updated)
        self.assertEqual(frozen_library_overlay(updated,self.source,self.live),updated)

    def test_missing_library_retry_handler_is_inserted_once(self):
        callback=next(line for line in self.live.splitlines()
                      if line.startswith("async function retryLibrary(id){"))
        old=self.live.replace(callback+"\n","",1)
        updated=frozen_library_overlay(old,self.source,self.live)
        self.assertEqual(updated.count(callback),1)
        self.assertEqual(frozen_library_overlay(updated,self.source,self.live),updated)

    def test_unreviewed_library_retry_handler_drift_is_rejected(self):
        callback=next(line for line in self.live.splitlines()
                      if line.startswith("async function retryLibrary(id){"))
        changed=self.live.replace(callback,callback+" /* unexpected */",1)
        with self.assertRaisesRegex(LibraryReleaseBlocked,"retry handler drift"):
            frozen_library_overlay(changed,self.source,self.live)

    def test_live_library_drift_fails_closed(self):
        changed=self.live.replace('Add Library Video','Modified Other Library UI',1)
        with self.assertRaisesRegex(LibraryReleaseBlocked,"live Library panel drift"):
            frozen_library_overlay(changed,self.source,self.live)

    def test_live_callback_drift_fails_closed(self):
        changed=self.live.replace("function resetLibraryForm(){","function resetLibraryForm(){/*unexpected*/",1)
        with self.assertRaisesRegex(LibraryReleaseBlocked,"live Library callback drift"):
            frozen_library_overlay(changed,self.source,self.live)

    def test_no_missing_controls_or_duplicated_markers_accepted(self):
        edited=self.source.replace('id="libraryUploadDrive" type="checkbox"',
                                   'id="libraryUploadDrive" type="checkbox" id="libraryUploadDrive"')
        with self.assertRaises(LibraryReleaseBlocked):
            frozen_library_overlay(self.live,edited,self.live)
        edited=self.source.replace("function viewLibrary(id)","function viewLibraryOther(id)",1)
        with self.assertRaisesRegex(LibraryReleaseBlocked,"preview handler missing"):
            frozen_library_overlay(self.live,edited,self.live)


if __name__=="__main__":
    unittest.main()

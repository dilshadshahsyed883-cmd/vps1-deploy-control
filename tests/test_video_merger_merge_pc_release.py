"""Strict Video Merger direct-PC merge UI overlay contract tests."""
from pathlib import Path
import unittest
from production.video_merger_merge_pc_release import frozen_merge_pc_overlay, MergePcUiDrift

ROOT=Path(__file__).resolve().parents[1]
BASELINE=ROOT/"production/video_merger_merge_pc_pre_20261113.html"
SOURCE=ROOT/"apps/video-merger/part01/templates/index.html"


class MergePcUiReleaseTests(unittest.TestCase):
    def setUp(self):
        self.live=BASELINE.read_text(encoding="utf-8")
        self.source=SOURCE.read_text(encoding="utf-8")

    def test_exact_merge_patch_reproduces_source_without_changing_library_or_shell(self):
        patched=frozen_merge_pc_overlay(self.live,self.source,self.live)
        self.assertEqual(patched,self.source)
        self.assertEqual(patched.count('class="video-pc-file"'),1)
        self.assertEqual(patched.count('class="vm-pc-merge-choice"'),1)
        self.assertEqual(frozen_merge_pc_overlay(patched,self.source,self.live),patched)
        for start,end in (
            ('    <div id="tab-library" class="tab-panel">',
             '  <div id="tab-proxy" class="tab-panel">'),
            ('function renderLibrary(){','function toggleImportTrim(){'),
            ('function renderProxy(){','newMerge();'),
        ):
            self.assertEqual(
                patched[patched.index(start):patched.index(end,patched.index(start))],
                self.live[self.live.index(start):self.live.index(end,self.live.index(start))])

    def test_unrelated_parallel_markup_is_preserved(self):
        old=self.live.replace('  <div id="tab-proxy" class="tab-panel">',
                              '  <div id="tab-proxy" class="tab-panel"><b>Keep separate work</b>',1)
        result=frozen_merge_pc_overlay(old,self.source,self.live)
        self.assertIn('Keep separate work',result)
        self.assertEqual(frozen_merge_pc_overlay(result,self.source,self.live),result)

    def test_modified_merge_url_row_and_queue_are_blocked(self):
        changed=self.live.replace('Video ${n} URL','Tampered merge UI',1)
        with self.assertRaisesRegex(MergePcUiDrift,"drift"):
            frozen_merge_pc_overlay(changed,self.source,self.live)
        changed=self.live.replace("async function queueMerge(){",
                                  "async function queueMerge(){/*unexpected*/",1)
        with self.assertRaisesRegex(MergePcUiDrift,"drift"):
            frozen_merge_pc_overlay(changed,self.source,self.live)

    def test_duplicate_pc_control_is_rejected(self):
        changed=self.source.replace('class="video-pc-file"',
                                    'class="video-pc-file" class="video-pc-file"',1)
        with self.assertRaises(MergePcUiDrift):
            frozen_merge_pc_overlay(self.live,changed,self.live)

    def test_no_unauthorized_global_html_replacement(self):
        merged=frozen_merge_pc_overlay(self.live,self.source,self.live)
        self.assertEqual(merged.count('id="libraryUploadDrive"'),1)
        self.assertEqual(merged.count("function renderProxy(){"),1)
        self.assertEqual(merged.count("function queueMerge(){"),1)
        self.assertIn('function specFromItem(',merged)
        self.assertIn('function scheduleAutoName(){',merged)


if __name__=="__main__":
    unittest.main()

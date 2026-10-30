"""Video Merger legacy interpreter pointer: preserve all live and broken owned links."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from production.runtime import ensure_video_merger_venv_pointer


class VideoMergerManagedVenvPointerTests(TestCase):
    def test_existing_dangling_owned_symlink_is_preserved(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "venvs/video-merger"
            managed.mkdir(parents=True)
            media = root / "video-merger"
            media.mkdir()
            older = managed / "old-immutable-release"
            incoming = managed / "new-immutable-release"
            pointer = media / "venv"
            pointer.symlink_to(older)
            self.assertTrue(pointer.is_symlink())
            self.assertFalse(pointer.exists())
            self.assertEqual(ensure_video_merger_venv_pointer(media, incoming, managed),
                             "PRESERVED_EXISTING")
            self.assertEqual(pointer.readlink(), older)

    def test_existing_live_symlink_is_never_replaced(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "venvs/video-merger"
            old = managed / "old"
            old.mkdir(parents=True)
            new = managed / "new"
            media = root / "video-merger"
            media.mkdir()
            (media / "venv").symlink_to(old)
            self.assertEqual(ensure_video_merger_venv_pointer(media,new,managed),
                             "PRESERVED_EXISTING")
            self.assertEqual((media / "venv").readlink(),old)

    def test_empty_pointer_is_created_with_immutable_target(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "venvs/video-merger"
            managed.mkdir(parents=True)
            media = root / "video-merger"
            media.mkdir()
            new = managed / "new-immutable-release"
            self.assertEqual(ensure_video_merger_venv_pointer(media,new,managed),"CREATED")
            self.assertEqual((media / "venv").readlink(),new)

    def test_unowned_symlink_or_real_folder_is_refused_without_deletion(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "venvs/video-merger"
            managed.mkdir(parents=True)
            media = root / "video-merger"
            media.mkdir()
            target = root / "other"
            target.mkdir()
            pointer = media / "venv"
            pointer.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError,"Unowned"):
                ensure_video_merger_venv_pointer(media,managed / "new",managed)
            self.assertEqual(pointer.readlink(),target)
            pointer.unlink()
            pointer.mkdir()
            with self.assertRaisesRegex(RuntimeError,"Unmanaged"):
                ensure_video_merger_venv_pointer(media,managed / "new",managed)
            self.assertTrue(pointer.is_dir())

    def test_candidate_escape_is_refused(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            managed=root/"venvs/video-merger"
            managed.mkdir(parents=True)
            media=root/"video-merger"
            media.mkdir()
            with self.assertRaisesRegex(RuntimeError,"escaped"):
                ensure_video_merger_venv_pointer(media,root/"foreign/release",managed)
            self.assertFalse((media/"venv").exists())

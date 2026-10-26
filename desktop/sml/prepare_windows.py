"""Make an independent Windows copy of the SML product; do not edit production."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "apps" / "self-media-library" / "part01"
STAGE = REPO / "build" / "sml_desktop" / "stage"


def replace_checked(path: Path, before: str, after: str) -> None:
    content = path.read_text(encoding="utf-8")
    if before not in content:
        raise RuntimeError(f"Upstream source changed, review Windows adapter: {path.name}")
    path.write_text(content.replace(before, after), encoding="utf-8")


def main() -> None:
    if not (SOURCE / "app.py").exists():
        raise RuntimeError("SML source not found")
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    for path in SOURCE.glob("*.py"):
        shutil.copy2(path, STAGE / path.name)
    shutil.copytree(SOURCE / "static", STAGE / "static")
    (STAGE / "fcntl.py").write_text(
        '''"""Windows implementation of the file-lock subset used by desktop SML."""
import msvcrt
import os

LOCK_EX = 1
LOCK_NB = 4
LOCK_UN = 8


def flock(fileobj, operation):
    if operation & LOCK_UN:
        mode = msvcrt.LK_UNLCK
    elif operation & LOCK_NB:
        mode = msvcrt.LK_NBLCK
    else:
        mode = msvcrt.LK_LOCK
    fileobj.seek(0)
    if os.fstat(fileobj.fileno()).st_size == 0:
        fileobj.write("1")
        fileobj.flush()
        fileobj.seek(0)
    msvcrt.locking(fileobj.fileno(), mode, 1)
''', encoding="utf-8")
    replace_checked(
        STAGE / "ai_workspace.py",
        "self.process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',str(self.root)],env=",
        "self.process=subprocess.Popen(([sys.executable,'--sml-ai-worker',str(self.root)] if os.getenv('SML_DESKTOP_MODE')=='1' else [sys.executable,str(Path(__file__).resolve()),'--worker',str(self.root)]),env=",
    )
    replace_checked(
        STAGE / "video_merger_downloader.py",
        "info.st_uid != os.geteuid()",
        "(os.name != 'nt' and info.st_uid != os.geteuid())",
    )
    replace_checked(
        STAGE / "video_merger_downloader.py",
        "info.st_mode & 0o077",
        "(os.name != 'nt' and info.st_mode & 0o077)",
    )
    replace_checked(
        STAGE / "sml_proxy_profiles.py",
        "st.st_mode & 0o077",
        "(os.name != 'nt' and st.st_mode & 0o077)",
    )
    html = STAGE / "static" / "index.html"
    html.write_text(html.read_text(encoding="utf-8").replace("VPS", "Desktop"), encoding="utf-8")
    print("Desktop stage ready, independent of the VPS runtime:", STAGE)


if __name__ == "__main__":
    main()

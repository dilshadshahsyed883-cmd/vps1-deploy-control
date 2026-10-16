from __future__ import annotations

import sys
import time
import traceback

from PySide6.QtWidgets import QApplication

from app_constants import APP_NAME, ORG_NAME
from app_main_window import MainWindow
from smart_scroll.local_ai_tools import app_root


def _install_crash_logger() -> None:
    """Write uncaught UI crashes to _crash_logs instead of failing silently."""
    def handle_exception(exc_type, exc_value, exc_tb):
        try:
            crash_dir = app_root() / "_crash_logs"
            crash_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            log_path = crash_dir / f"crash_{stamp}.txt"
            log_path.write_text(
                "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
                encoding="utf-8",
            )
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = handle_exception


def main() -> int:
    _install_crash_logger()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

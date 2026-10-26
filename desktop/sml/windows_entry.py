"""Native Windows SML desktop entry. Local web UI, no external server connection."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path


def runtime_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def app_config_path() -> Path:
    p = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SML_Desktop"
    p.mkdir(parents=True, exist_ok=True)
    return p / "desktop_settings.json"


def default_data_path() -> Path:
    preferred = Path("D:/SML_Desktop_Data")
    if preferred.drive and Path(preferred.anchor).exists():
        return preferred
    return Path.home() / "Documents" / "SML_Desktop_Data"


def choose_data_root() -> Path:
    raw = os.environ.get("SML_DESKTOP_DATA_ROOT")
    if raw:
        return Path(raw).expanduser().resolve()
    try:
        saved = json.loads(app_config_path().read_text(encoding="utf-8"))
        root = saved.get("data_folder")
        if isinstance(root, str) and root:
            return Path(root).expanduser().resolve()
    except (ValueError, OSError):
        pass
    return default_data_path()


def change_data_folder() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    root = tk.Tk()
    root.withdraw()
    selected = filedialog.askdirectory(title="Select Self Media Library data folder", mustexist=True)
    if selected:
        target = Path(selected).resolve()
        target.mkdir(parents=True, exist_ok=True)
        app_config_path().write_text(json.dumps({"data_folder": str(target)}, indent=2), encoding="utf-8")
        messagebox.showinfo("SML Desktop", "Saved new library location. Existing data is NOT moved or deleted.\nRestart SML Desktop to use this location.")
    root.destroy()


def reserve_local_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def prepare_environment() -> Path:
    bundled = runtime_root()
    sys.path.insert(0, str(bundled))
    os.environ["SML_DATA_ROOT"] = str(choose_data_root())
    os.environ["SML_DESKTOP_MODE"] = "1"
    os.environ.setdefault("SML_AI_WORKER", "1")
    data = Path(os.environ["SML_DATA_ROOT"])
    data.mkdir(parents=True, exist_ok=True)
    os.environ["PATH"] = str(bundled) + os.pathsep + os.environ.get("PATH", "")
    return data


def start() -> None:
    if "--choose-data" in sys.argv:
        change_data_folder()
        return
    data = prepare_environment()
    if len(sys.argv) >= 3 and sys.argv[1:3] == ["-m", "yt_dlp"]:
        import yt_dlp
        sys.argv = [sys.executable] + sys.argv[3:]
        raise SystemExit(yt_dlp.main())
    if "--self-test" in sys.argv:
        from app import app as sml_app
        required = {"/api/health", "/api/clips", "/api/sources", "/api/clips/processed/upload"}
        routes = {str(route.path) for route in sml_app.routes}
        missing = sorted(required - routes)
        if missing:
            raise RuntimeError("Missing packaged API routes: " + ",".join(missing))
        (data / "packaged-self-test.json").write_text(json.dumps({"ok": True, "routes": len(routes)}), encoding="utf-8")
        return
    if "--sml-ai-worker" in sys.argv:
        from ai_workspace import run_worker
        run_worker(Path(sys.argv[sys.argv.index("--sml-ai-worker") + 1]))
        return
    try:
        import uvicorn
        from app import app as sml_app
        port = reserve_local_port()
        def serve() -> None:
            uvicorn.run(sml_app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        for _ in range(120):
            with socket.socket() as sock:
                sock.settimeout(.4)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(.2)
        else:
            raise RuntimeError("SML local server failed to start")
        url = f"http://127.0.0.1:{port}/"
        try:
            import webview
            webview.create_window("Self Media Library - Desktop", url, width=1450, height=920, min_size=(980, 650), background_color="#101828")
            webview.start(debug=False)
        except Exception:
            import webbrowser
            import tkinter as tk
            from tkinter import ttk
            webbrowser.open(url)
            w = tk.Tk()
            w.title("SML Desktop - Local Server")
            w.geometry("400x135")
            ttk.Label(w, text="SML is running locally in your browser.").pack(pady=16)
            ttk.Button(w, text="Open SML", command=lambda: webbrowser.open(url)).pack()
            ttk.Button(w, text="Stop and exit", command=w.destroy).pack(pady=6)
            w.mainloop()
    except Exception:
        message = traceback.format_exc()
        (data / "desktop_startup_error.log").write_text(message, encoding="utf-8")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message[-3000:], "SML Desktop startup error", 0x10)
        except Exception:
            print(message, file=sys.stderr)
        raise


if __name__ == "__main__":
    start()

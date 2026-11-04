"""Concat Studio desktop control centre: four functional tabs, Windows EXE entry.

Native editing remains in official Concat; the studio creates and exports real
Concat projects via the documented MCP interface. No live project is overwritten.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import (ConcatMCP, StudioError, assign_media, build_project,
                   parse_plan, parse_srt)
from .gpu import Gateway, GatewayError, WindowsSecrets


def initial_exe() -> str:
    candidates = [
        Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "Concat" / "concat.exe",
        Path(os.getenv("PROGRAMFILES", "C:/Program Files")) / "Concat" / "concat.exe",
    ]
    return str(next((p for p in candidates if p.is_file()), candidates[0]))


class Studio(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Concat Studio — Z Builder + Salad GPU")
        self.geometry("1180x790")
        self.minsize(980, 670)
        self.home = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ConcatStudio"
        self.home.mkdir(parents=True, exist_ok=True)
        self.config_file = self.home / "settings.json"
        try:
            self.config = json.loads(self.config_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            self.config = {}
        self.events = queue.Queue()
        self.busy = False
        self.last_project = None
        self.last_gpu_job = ""
        self._configure_style()
        self._render_tabs()
        self.after(120, self._process_events)

    def _configure_style(self):
        self.configure(bg="#101a2b")
        theme = ttk.Style(self)
        theme.theme_use("clam")
        theme.configure("TFrame", background="#101a2b")
        theme.configure("TLabel", background="#101a2b", foreground="#e7eefb", font=("Segoe UI", 10))
        theme.configure("Header.TLabel", background="#101a2b", foreground="#ffffff",
                        font=("Segoe UI", 15, "bold"))
        theme.configure("TButton", padding=(12, 8), background="#2369a5",
                        foreground="#ffffff", font=("Segoe UI", 9, "bold"))
        theme.map("TButton", background=[("active", "#3585d0")])
        theme.configure("TEntry", fieldbackground="#1a2b42", foreground="#ffffff")
        theme.configure("TNotebook", background="#101a2b", borderwidth=0)
        theme.configure("TNotebook.Tab", background="#1d2f47", foreground="#e5effe", padding=(17, 10))
        theme.map("TNotebook.Tab", background=[("selected", "#2669ad")])
        theme.configure("Treeview", background="#15243a", foreground="#ffffff",
                        fieldbackground="#15243a", rowheight=26)
        theme.configure("Treeview.Heading", background="#253c59", foreground="#ffffff")
        theme.map("Treeview", background=[("selected", "#286da9")])

    def _render_tabs(self):
        root = ttk.Frame(self, padding=15)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="CONCAT STUDIO", style="Header.TLabel").pack(anchor="w")
        ttk.Label(root, text="Z Builder scene planning → native Concat editing → PC / Salad GPU rendering",
                  foreground="#a8bdd6").pack(anchor="w", pady=(2, 14))
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True)
        self.ai = ttk.Frame(self.tabs, padding=14)
        self.editor = ttk.Frame(self.tabs, padding=14)
        self.render = ttk.Frame(self.tabs, padding=14)
        self.gpu = ttk.Frame(self.tabs, padding=14)
        for frame, title in [(self.ai, "1  AI Builder"), (self.editor, "2  Editor"),
                             (self.render, "3  Render Queue"), (self.gpu, "4  GPU Config")]:
            self.tabs.add(frame, text=title)
        self.status = tk.StringVar(value="Ready — existing media and projects are never overwritten")
        ttk.Label(root, textvariable=self.status, foreground="#8cbce9").pack(
            fill="x", pady=(10, 0))
        self._ai_tab()
        self._editor_tab()
        self._render_tab()
        self._gpu_tab()

    @staticmethod
    def _line(frame, label, variable, command=None, hidden=False):
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text=label, width=22).pack(side="left")
        entry = ttk.Entry(row, textvariable=variable, show="*" if hidden else "")
        entry.pack(side="left", fill="x", expand=True, padx=(8, 6))
        if command:
            ttk.Button(row, text="Browse", command=command).pack(side="left")
        return entry

    def _choose_file(self, var, types):
        selected = filedialog.askopenfilename(filetypes=types)
        if selected:
            var.set(selected)

    def _choose_folder(self, var):
        selected = filedialog.askdirectory()
        if selected:
            var.set(selected)

    def _ai_tab(self):
        ttk.Label(self.ai, text="Z Builder Scene Plan → Concat Project",
                  style="Header.TLabel").pack(anchor="w", pady=(0, 10))
        self.scene_file = tk.StringVar()
        self.media_dir = tk.StringVar()
        self.voice_file = tk.StringVar()
        self.srt_file = tk.StringVar()
        self.projects_dir = tk.StringVar(
            value=self.config.get("projects_dir") or str(Path.home() / "Documents" / "Concat Studio Projects"))
        self.concat_exe = tk.StringVar(value=self.config.get("concat_exe") or initial_exe())
        self._line(self.ai, "Scene Plan (.txt)", self.scene_file,
                   lambda: self._choose_file(self.scene_file, [("Scene Plan", "*.txt")]))
        self._line(self.ai, "Media Folder", self.media_dir,
                   lambda: self._choose_folder(self.media_dir))
        self._line(self.ai, "Narration (optional)", self.voice_file,
                   lambda: self._choose_file(self.voice_file, [("Audio", "*.mp3 *.wav *.m4a *.flac")]))
        self._line(self.ai, "SRT (optional)", self.srt_file,
                   lambda: self._choose_file(self.srt_file, [("Subtitles", "*.srt")]))
        self._line(self.ai, "Project Destination", self.projects_dir,
                   lambda: self._choose_folder(self.projects_dir))
        self._line(self.ai, "Concat.exe", self.concat_exe,
                   lambda: self._choose_file(self.concat_exe, [("Concat editor", "*.exe")]))
        commands = ttk.Frame(self.ai)
        commands.pack(fill="x", pady=12)
        ttk.Button(commands, text="Preview & Validate", command=self.preview).pack(side="left")
        ttk.Button(commands, text="Generate Editable Concat Project",
                   command=self.generate).pack(side="left", padx=8)
        self.preview_table = ttk.Treeview(self.ai, columns=("range", "type", "file", "trim"),
                                          show="headings", height=12)
        for name, size in (("range", 180), ("type", 75), ("file", 530), ("trim", 80)):
            self.preview_table.heading(name, text=name.upper())
            self.preview_table.column(name, width=size, stretch=(name == "file"))
        self.preview_table.pack(fill="both", expand=True)
        self.ai_message = tk.StringVar(value="Import a timed Z Builder V6.3 Scene Plan.")
        ttk.Label(self.ai, textvariable=self.ai_message, foreground="#9fb7d3",
                  wraplength=950).pack(fill="x", pady=8)

    def _input_plan(self):
        source = Path(self.scene_file.get()).read_text(encoding="utf-8-sig")
        plan = assign_media(parse_plan(source), Path(self.media_dir.get()))
        subtitles = []
        if self.srt_file.get():
            subtitles = parse_srt(Path(self.srt_file.get()).read_text(encoding="utf-8-sig"),
                                  plan.duration)
        return plan, subtitles

    def preview(self):
        try:
            plan, captions = self._input_plan()
            self.preview_table.delete(*self.preview_table.get_children())
            for i, shot in enumerate(plan.shots, 1):
                self.preview_table.insert("", "end", values=(
                    "{:.3f} — {:.3f}s".format(shot.start, shot.end),
                    shot.kind, shot.file.name, "{:.3f}s".format(shot.source_in)))
            self.ai_message.set("{} scenes • {:.2f}s • {}×{} • {} FPS • {} captions • all media resolved".format(
                len(plan.shots), plan.duration, plan.width, plan.height, plan.fps, len(captions)))
            return plan, captions
        except Exception as exc:
            self._error(str(exc))
            return None

    def generate(self):
        checked = self.preview()
        if not checked:
            return
        plan, captions = checked
        self._save_options()
        destination = Path(self.projects_dir.get())
        narration = Path(self.voice_file.get()) if self.voice_file.get() else None
        def do():
            destination.mkdir(parents=True, exist_ok=True)
            return build_project(Path(self.concat_exe.get()), plan, destination,
                                 narration=narration, captions=captions,
                                 progress=lambda msg: self.events.put(("progress", msg)))
        def done(project):
            self.project_path.set(str(project))
            self.last_project = project
            self.tabs.select(self.editor)
            self.status.set("Created native Concat project: " + str(project))
        self._background(do, done)

    def _editor_tab(self):
        ttk.Label(self.editor, text="Native Concat Editor", style="Header.TLabel").pack(
            anchor="w", pady=(0, 10))
        self.project_path = tk.StringVar()
        self._line(self.editor, "Concat Project Folder", self.project_path,
                   lambda: self._choose_folder(self.project_path))
        row = ttk.Frame(self.editor)
        row.pack(fill="x", pady=10)
        ttk.Button(row, text="Launch Concat Editor", command=self.open_concat).pack(side="left")
        ttk.Button(row, text="Open Project Folder", command=self.open_folder).pack(
            side="left", padx=8)
        ttk.Button(row, text="Read Native Timeline", command=self.read_timeline).pack(side="left")
        ttk.Label(self.editor, text="Open the saved project from Concat Home. The native Concat window "
                  "handles clip editing, effects, keyframes and live preview; Studio never rewrites "
                  "the project while Concat has it open.", foreground="#b2c5dc",
                  wraplength=950).pack(anchor="w", pady=12)
        self.timeline = ttk.Treeview(self.editor, columns=("start", "end", "kind", "name", "track"),
                                      show="headings")
        for name, width in (("start", 120), ("end", 120), ("kind", 110),
                            ("name", 410), ("track", 110)):
            self.timeline.heading(name, text=name.upper())
            self.timeline.column(name, width=width, stretch=(name == "name"))
        self.timeline.pack(fill="both", expand=True)

    def _project(self) -> Path:
        project = Path(self.project_path.get()).expanduser().resolve(strict=True)
        if not (project / "concat.json").is_file():
            raise StudioError("Select a folder containing concat.json")
        return project

    def open_concat(self):
        try:
            binary = Path(self.concat_exe.get())
            if not binary.is_file():
                raise StudioError("Concat.exe not found")
            subprocess.Popen([str(binary)], cwd=str(binary.parent))
            self.status.set("Concat editor launched — open your project from its Home screen")
        except Exception as exc:
            self._error(str(exc))

    def open_folder(self):
        try:
            project = self._project()
            if sys.platform == "win32":
                os.startfile(str(project))
            else:
                raise StudioError("Open-folder launcher is available on Windows")
        except Exception as exc:
            self._error(str(exc))

    def read_timeline(self):
        try:
            project = self._project()
            binary = Path(self.concat_exe.get())
        except Exception as exc:
            self._error(str(exc))
            return
        def do():
            with ConcatMCP(binary, [project, project.parent]) as api:
                api.call("project_open", path=str(project))
                try:
                    return api.call("timeline_read", path=str(project))
                finally:
                    api.call("project_close", path=str(project), save=False)
        def done(summary):
            self.timeline.delete(*self.timeline.get_children())
            for clip in summary.get("timeline", {}).get("clips", []):
                self.timeline.insert("", "end", values=(
                    clip.get("start"), clip.get("end"), clip.get("kind"),
                    clip.get("name", ""), clip.get("track", "")))
            self.status.set("Native timeline loaded. If Concat GUI is open, close project first.")
        self._background(do, done)

    def _render_tab(self):
        ttk.Label(self.render, text="Render Queue", style="Header.TLabel").pack(
            anchor="w", pady=(0, 10))
        ttk.Label(self.render, text="Select project in Editor, then choose PC or saved Salad GPU. "
                  "One active job at a time; native Concat output is never overwritten.",
                  foreground="#b2c5dc", wraplength=950).pack(anchor="w", pady=6)
        row = ttk.Frame(self.render)
        row.pack(fill="x", pady=10)
        ttk.Button(row, text="Render MP4 on PC", command=self.render_pc).pack(side="left")
        ttk.Button(row, text="Send Project to Salad GPU", command=self.send_gpu).pack(
            side="left", padx=8)
        ttk.Button(row, text="Check GPU Job", command=self.check_job).pack(side="left")
        ttk.Button(row, text="Download GPU MP4", command=self.download_gpu).pack(
            side="left", padx=8)
        self.job_id = tk.StringVar()
        self._line(self.render, "GPU Job ID", self.job_id)
        self.job_log = tk.Text(self.render, bg="#15243a", fg="#e8f1fa", height=17,
                               insertbackground="#ffffff", state="disabled", relief="flat")
        self.job_log.pack(fill="both", expand=True, pady=10)
        self._append_log("Ready for PC export or GPU project upload.")

    def render_pc(self):
        try:
            project = self._project()
        except Exception as exc:
            self._error(str(exc))
            return
        outfile = filedialog.asksaveasfilename(defaultextension=".mp4",
                                                filetypes=[("MP4", "*.mp4")],
                                                initialfile=project.name + ".mp4")
        if not outfile:
            return
        target = Path(outfile)
        if target.exists():
            self._error("MP4 already exists; choose a new filename")
            return
        def do():
            with ConcatMCP(Path(self.concat_exe.get()), [project, project.parent, target.parent]) as api:
                api.call("project_open", path=str(project))
                try:
                    begun = api.call("export_start", path=str(project), output=str(target),
                                     codec="h264")
                    job = begun.get("job")
                    if not job:
                        raise StudioError("Concat did not return an export job")
                    for _ in range(600):
                        result = api.call("export_wait", job=job, timeoutSeconds=30)
                        self.events.put(("progress", "PC Render: " + str(result.get("state"))))
                        if result.get("state") in ("done", "failed", "cancelled"):
                            if result["state"] != "done":
                                raise StudioError("Native Concat export: " + str(result.get("error", result["state"])))
                            break
                    else:
                        raise StudioError("PC export did not finish")
                finally:
                    api.call("project_close", path=str(project), save=False)
            if not target.is_file() or target.stat().st_size < 12:
                raise StudioError("Concat reported success without an MP4")
            with target.open("rb") as stream:
                if stream.read(12)[4:8] != b"ftyp":
                    raise StudioError("Native output MP4 signature failed")
            return target
        self._background(do, lambda path: self._append_log("PC MP4 ready: " + str(path)))

    def _gateway(self):
        return Gateway(self.gpu_url.get(), self.vault.get("salad_api_key"),
                       self.vault.get("renderer_token"))

    def send_gpu(self):
        try:
            project = self._project()
            client = self._gateway()
        except Exception as exc:
            self._error(str(exc))
            return
        self._background(lambda: client.upload(project,
                         on_progress=lambda msg: self.events.put(("progress", msg))),
                         lambda job: (self.job_id.set(job),
                                      self._append_log("GPU submitted: " + job)))

    def check_job(self):
        try:
            job = self.job_id.get().strip()
            if not job:
                raise StudioError("No GPU job ID")
            client = self._gateway()
        except Exception as exc:
            self._error(str(exc))
            return
        self._background(lambda: client.job(job),
                         lambda item: self._append_log("GPU job {}: {} {}".format(
                             job, item.get("state"), item.get("error", ""))))

    def download_gpu(self):
        try:
            job = self.job_id.get().strip()
            if not job:
                raise StudioError("No GPU job ID")
            client = self._gateway()
        except Exception as exc:
            self._error(str(exc))
            return
        target = filedialog.asksaveasfilename(defaultextension=".mp4",
                                              initialfile=job + ".mp4",
                                              filetypes=[("MP4", "*.mp4")])
        if target:
            self._background(lambda: client.download(job, Path(target)),
                             lambda path: self._append_log("Verified GPU MP4: " + str(path)))

    def _gpu_tab(self):
        ttk.Label(self.gpu, text="Salad GPU Configuration",
                  style="Header.TLabel").pack(anchor="w", pady=(0, 10))
        self.gpu_url = tk.StringVar(value=self.config.get("gpu_url", ""))
        self.salad_key = tk.StringVar()
        self.renderer_key = tk.StringVar()
        self._line(self.gpu, "Gateway HTTPS URL", self.gpu_url)
        self._line(self.gpu, "Salad API Key", self.salad_key, hidden=True)
        self._line(self.gpu, "RENDERER_API_TOKEN", self.renderer_key, hidden=True)
        try:
            self.vault = WindowsSecrets()
            state = ("Saved Salad key: " + ("Yes" if self.vault.get("salad_api_key") else "No")
                     + "  |  Saved renderer token: "
                     + ("Yes" if self.vault.get("renderer_token") else "No"))
        except Exception:
            self.vault = None
            state = "Windows Credential Manager is unavailable"
        self.secret_state = tk.StringVar(value=state)
        ttk.Label(self.gpu, textvariable=self.secret_state, foreground="#9eb6d2").pack(
            anchor="w", pady=7)
        row = ttk.Frame(self.gpu)
        row.pack(fill="x", pady=12)
        ttk.Button(row, text="Save GPU Profile", command=self.save_gpu).pack(side="left")
        ttk.Button(row, text="Test GPU & Vulkan", command=self.test_gpu).pack(
            side="left", padx=8)
        ttk.Button(row, text="Clear Saved Keys", command=self.delete_gpu_keys).pack(side="left")
        ttk.Label(self.gpu, text="Renderer defaults: H.264 · 1080p · 30 FPS · concurrency 1",
                  foreground="#ffffff").pack(anchor="w", pady=7)
        ttk.Label(self.gpu, text="Salad priority, VRAM and replica allocation are configured "
                  "on Salad itself. NVIDIA Vulkan (not CUDA-only/Mesa fallback) is required "
                  "before GPU jobs are sent. Cloud Render VPS receives verified MP4 output "
                  "when the Salad worker has its VPS receiver configured.",
                  foreground="#b1c6df", wraplength=950).pack(anchor="w", pady=7)
        self.gpu_diag = tk.Text(self.gpu, height=12, bg="#15243a", fg="#e7f1fe",
                                relief="flat", state="disabled")
        self.gpu_diag.pack(fill="both", expand=True, pady=12)

    def save_gpu(self):
        if self.vault is None:
            self._error("Windows Credential Manager unavailable")
            return
        try:
            from .gpu import origin_only
            value = origin_only(self.gpu_url.get())
            self.gpu_url.set(value)
            if self.salad_key.get():
                self.vault.set("salad_api_key", self.salad_key.get())
            if self.renderer_key.get():
                self.vault.set("renderer_token", self.renderer_key.get())
            self.salad_key.set("")
            self.renderer_key.set("")
            self._save_options()
            self.secret_state.set("Keys saved securely to Windows Credential Manager")
            self.status.set("GPU profile saved")
        except Exception as exc:
            self._error(str(exc))

    def delete_gpu_keys(self):
        if self.vault is None or not messagebox.askyesno("Delete credentials", "Delete both saved GPU keys?"):
            return
        self.vault.delete("salad_api_key")
        self.vault.delete("renderer_token")
        self.secret_state.set("Saved keys cleared")

    def test_gpu(self):
        try:
            gateway = self._gateway()
        except Exception as exc:
            self._error(str(exc))
            return
        def done(data):
            printable = {key: data.get(key) for key in (
                "vulkan_adapters", "nvidia_vulkan_visible", "nvidia_compute_visible",
                "software_vulkan_visible", "renderer_hint")}
            self.gpu_diag.configure(state="normal")
            self.gpu_diag.delete("1.0", "end")
            self.gpu_diag.insert("end", json.dumps(printable, indent=2))
            self.gpu_diag.configure(state="disabled")
            self.status.set("GPU diagnosis complete; NVIDIA Vulkan " +
                            ("visible" if data.get("nvidia_vulkan_visible") else "NOT visible"))
        self._background(gateway.diagnose, done)

    def _save_options(self):
        config = {"concat_exe": self.concat_exe.get(),
                  "projects_dir": self.projects_dir.get(),
                  "gpu_url": self.gpu_url.get()}
        staged = self.config_file.with_suffix(".tmp")
        staged.write_text(json.dumps(config, indent=2), encoding="utf-8")
        staged.replace(self.config_file)

    def _background(self, fn, on_done):
        if self.busy:
            self._error("Another Studio task is running")
            return
        self.busy = True
        self.status.set("Working...")
        def runner():
            try:
                result = fn()
                self.events.put(("done", on_done, result))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("idle",))
        threading.Thread(target=runner, daemon=True).start()

    def _append_log(self, message):
        self.job_log.configure(state="normal")
        self.job_log.insert("end", message + "\n")
        self.job_log.see("end")
        self.job_log.configure(state="disabled")

    def _error(self, message):
        self.status.set("Error: " + message[:180])
        messagebox.showerror("Concat Studio", message[:500])

    def _process_events(self):
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if item[0] == "progress":
                self.status.set(item[1])
                self._append_log(item[1])
            elif item[0] == "done":
                item[1](item[2])
            elif item[0] == "error":
                self._error(item[1])
            elif item[0] == "idle":
                self.busy = False
        self.after(120, self._process_events)


def main():
    Studio().mainloop()


if __name__ == "__main__":
    main()

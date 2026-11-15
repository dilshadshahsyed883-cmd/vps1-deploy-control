"""Short Maker Desktop: local-only Windows Short generator and persistent queue.

No server, Drive access, profiles, scheduler, or YouTube integration.
Concat's motion/transition catalogue is adapted to CPU FFmpeg primitives,
not presented as byte-for-byte CUDA rendering equivalence.
"""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")) / "ShortMakerDesktop"
QUEUE_PATH = APP_DIR / "queue.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".jfif"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
FPS = 30
WIDTH, HEIGHT = 1080, 1920

# Stable Concat procedural style names, approximated with documented FFmpeg
# xfade operations so they run on non-CUDA Windows desktops.
CONCAT_TRANSITIONS = {
    "ink-drying": "circleopen",
    "shake-flash": "fadewhite",
    "image-blend": "fade",
    "torn-paper": "smoothleft",
    "matrix-scan": "wipeup",
    "ghost-radio": "dissolve",
    "explosion-glitch": "hblur",
    "comic-cut": "pixelize",
    "red-chaos": "fadewhite",
    "cyber-beehive": "pixelize",
    "heat-flash": "fadewhite",
    "shards-scatter": "diagbl",
    "violet-fuzz": "dissolve",
    "warp-portal": "radial",
    "starlink-fence": "horzopen",
    "liquid-melt": "smoothdown",
    "burning-flame": "fadeblack",
    "pulse-wipe": "circleopen",
    "twinkle-zoom": "zoomin",
    "wispy-wipe": "smoothright",
}
# Includes the six named Concat physics patterns. Their CPU representations
# approximate motion using animated zoom/pan; rotation is not identical.
MOTIONS = (
    "zoom-in", "zoom-out", "pan-left", "pan-right",
    "spring-zoom", "pendulum", "gravity-bounce", "elastic-pop",
    "inertia-glide", "damped-shake",
)


def binary(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    packed = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / exe
    return str(packed) if packed.is_file() else (shutil.which(exe) or exe)


def scan_folder(path: str) -> tuple[list[str], list[str]]:
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise ValueError("Select an existing input folder.")
    pictures, audios = [], []
    for file in folder.rglob("*"):
        if not file.is_file() or file.is_symlink():
            continue
        suffix = file.suffix.lower()
        if suffix in IMAGE_EXTS:
            pictures.append(str(file.resolve()))
        elif suffix in AUDIO_EXTS:
            audios.append(str(file.resolve()))
    return sorted(pictures), sorted(audios)


def validate_paths(input_dir: str, output_dir: str) -> tuple[Path, Path]:
    if not input_dir.strip() or not output_dir.strip():
        raise ValueError("Input and output folders are both required.")
    source = Path(input_dir).expanduser().resolve()
    target = Path(output_dir).expanduser().resolve()
    if not source.is_dir() or not target.is_dir():
        raise ValueError("Both folders must already exist.")
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Choose separate input/output folders that do not contain one another.")
    return source, target


def picked_audio(audios: list[str], seed: int, video_index: int) -> str | None:
    if not audios:
        return None
    shuffled = sorted(set(audios))
    random.Random(seed ^ 0xAB531).shuffle(shuffled)
    return shuffled[video_index % len(shuffled)]


def image_sequence(images: list[str], count: int, seed: int) -> list[str]:
    if not images:
        raise ValueError("Input folder has no supported images.")
    rng = random.Random(seed)
    result = []
    while len(result) < count:
        pool = list(images)
        rng.shuffle(pool)
        if result and len(pool) > 1 and pool[0] == result[-1]:
            pool.append(pool.pop(0))
        result.extend(pool)
    return result[:count]


def motion_params(style: str, frames: int) -> tuple[str, str, str]:
    n = max(1, frames - 1)
    cx, cy = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    if style == "zoom-in":
        return f"1.02+0.09*on/{n}", cx, cy
    if style == "zoom-out":
        return f"1.11-0.09*on/{n}", cx, cy
    if style == "pan-left":
        return "1.12", f"(iw-iw/zoom)*(1-on/{n})", cy
    if style == "pan-right":
        return "1.12", f"(iw-iw/zoom)*on/{n}", cy
    if style == "spring-zoom":
        return "1.07+0.05*exp(-on/18)*cos(on/3)", cx, cy
    if style == "pendulum":
        return "1.10", "(iw-iw/zoom)*(0.5+0.23*sin(on/8)*exp(-on/35))", cy
    if style == "gravity-bounce":
        return "1.12", cx, "(ih-ih/zoom)*(0.2+0.5*abs(sin(on/9))*exp(-on/40))"
    if style == "elastic-pop":
        return "1.07+0.04*sin(on/5)*exp(-on/16)", cx, cy
    if style == "inertia-glide":
        return "1.12", "(iw-iw/zoom)*(0.5-0.4*exp(-on/18))", cy
    return "1.12", "(iw-iw/zoom)*(0.5+0.2*sin(on/2)*exp(-on/20))", cy


def build_command(images: list[str], audio: str | None, output: str, *,
                  seconds: float, image_seconds: float, seed: int,
                  fps: int = FPS, width: int = WIDTH, height: int = HEIGHT,
                  crf: int = 23) -> list[str]:
    """Construct one bounded FFmpeg job; frames and transitions stay in sync."""
    if not 1 <= len(images) or not 1 <= seconds <= 300 or not 0.5 <= image_seconds <= 30:
        raise ValueError("Invalid render input or duration.")
    if fps < 5 or width < 32 or height < 32:
        raise ValueError("Invalid render dimensions.")
    total = max(1, round(seconds * fps))
    desired = max(1, round(image_seconds * fps))
    overlap = min(12, max(2, round(desired * 0.22)), max(1, desired - 1))
    pieces = max(1, min(32, math.ceil((total - overlap) / max(1, desired - overlap))))
    overlap = min(overlap, max(1, (total // max(1, pieces)) // 2))
    segment = max(overlap + 1, math.ceil((total + (pieces - 1) * overlap) / pieces))
    duration = total / fps
    rng = random.Random(seed)
    chosen = image_sequence(images, pieces, seed ^ 0x9812)
    command = [binary("ffmpeg"), "-hide_banner", "-nostdin", "-y",
               "-loglevel", "error", "-stats_period", "0.5",
               "-progress", "pipe:1"]
    for path in chosen:
        command += ["-i", path]
    if audio:
        command += ["-stream_loop", "-1", "-i", audio]
    filters = []
    last_motion = ""
    for i in range(pieces):
        available = [m for m in MOTIONS if m != last_motion]
        last_motion = rng.choice(available)
        z, x, y = motion_params(last_motion, segment)
        filters.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={segment}:s={width}x{height}:fps={fps},"
            f"settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
        )
    previous = "v0"
    last_kind = ""
    if pieces > 1:
        for i in range(1, pieces):
            choices = [x for x in CONCAT_TRANSITIONS if x != last_kind]
            last_kind = rng.choice(choices)
            effect = CONCAT_TRANSITIONS[last_kind]
            offset = i * (segment - overlap) / fps
            filters.append(
                f"[{previous}][v{i}]xfade=transition={effect}:"
                f"duration={overlap / fps:.6f}:offset={offset:.6f}[x{i}]"
            )
            previous = f"x{i}"
    filters.append(
        f"[{previous}]trim=end_frame={total},setpts=PTS-STARTPTS,format=yuv420p[vout]"
    )
    command += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if audio:
        command += ["-map", f"{pieces}:a:0", "-c:a", "aac", "-b:a", "160k"]
    else:
        command += ["-an"]
    command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-r", str(fps), "-threads", "3",
                "-movflags", "+faststart", "-t", f"{duration:.6f}", output]
    return command


def verified_video(file: Path, expected_seconds: float) -> bool:
    if not file.is_file() or file.stat().st_size < 4096:
        return False
    try:
        result = subprocess.run(
            [binary("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(file)],
            capture_output=True, text=True, timeout=20, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return abs(float(result.stdout.strip()) - expected_seconds) < 1.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


class RenderQueue:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = self._read()
        self.worker: threading.Thread | None = None
        self.process: subprocess.Popen | None = None
        self.control = ""

    def _read(self) -> list[dict]:
        if not QUEUE_PATH.exists():
            return []
        data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("Saved queue has invalid format.")
        for job in data:
            if job.get("status") in ("running", "rendering"):
                job["status"] = "paused"
                job["message"] = "Interrupted by app exit; resume to restart current video"
        return data

    def save(self):
        with self.lock:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            temp = QUEUE_PATH.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(self.jobs, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, QUEUE_PATH)

    def enqueue(self, input_dir: str, output_dir: str, extra_audio: list[str],
                seconds: float, image_seconds: float, count: int) -> dict:
        source, target = validate_paths(input_dir, output_dir)
        pics, audios = scan_folder(str(source))
        if not pics:
            raise ValueError("Input folder has no supported pictures.")
        if count < 1 or count > 999:
            raise ValueError("Video count must be between 1 and 999.")
        if not 1 <= seconds <= 300 or not 0.5 <= image_seconds <= 30:
            raise ValueError("Video duration: 1-300 sec; image duration: 0.5-30 sec.")
        extras = [str(Path(x).resolve()) for x in extra_audio
                  if Path(x).is_file() and Path(x).suffix.lower() in AUDIO_EXTS]
        job = {
            "id": uuid.uuid4().hex[:12], "created": datetime.now().isoformat(timespec="seconds"),
            "input": str(source), "output": str(target),
            "images": pics, "audios": sorted(set(audios + extras)),
            "seconds": seconds, "image_seconds": image_seconds, "count": count,
            "cursor": 0, "seed": random.SystemRandom().randrange(1, 2**31),
            "status": "queued", "progress": 0, "message": "Waiting to render",
        }
        with self.lock:
            self.jobs.append(job)
            self.save()
        return job

    def item(self, job_id: str) -> dict:
        for job in self.jobs:
            if job["id"] == job_id:
                return job
        raise ValueError("Select a queue job.")

    def action(self, job_id: str, action: str):
        with self.lock:
            job = self.item(job_id)
            status = job["status"]
            if action == "pause":
                if status == "queued":
                    job["status"], job["message"] = "paused", "Paused"
                elif status == "running":
                    self.control = "paused"
                    self._terminate()
                else:
                    raise ValueError("Only queued/running jobs can be paused.")
            elif action == "cancel":
                if status in ("queued", "paused"):
                    job["status"], job["message"] = "cancelled", "Cancelled; outputs retained"
                elif status == "running":
                    self.control = "cancelled"
                    self._terminate()
                else:
                    raise ValueError("Job cannot be cancelled in this status.")
            elif action in ("resume", "retry"):
                allowed = ("paused",) if action == "resume" else ("failed", "cancelled")
                if status not in allowed:
                    raise ValueError("This job cannot be " + action + "d.")
                job["status"], job["message"] = "queued", "Waiting to render"
            elif action == "delete":
                if status not in ("completed", "failed", "cancelled", "paused"):
                    raise ValueError("Pause/cancel an active job before deleting.")
                # Deleting a queue row NEVER deletes input or output videos.
                self.jobs.remove(job)
            else:
                raise ValueError("Invalid action")
            self.save()
        if action in ("resume", "retry"):
            self.start()

    def _terminate(self):
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass

    def start(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            self.worker = threading.Thread(target=self._run, daemon=True, name="ShortMakerRender")
            self.worker.start()

    def _run(self):
        while True:
            with self.lock:
                job = next((x for x in self.jobs if x["status"] == "queued"), None)
                if job is None:
                    return
                job["status"], job["message"] = "running", "Rendering"
                self.control = ""
                self.save()
            try:
                self._render(job)
                with self.lock:
                    if self.control:
                        job["status"] = self.control
                        job["message"] = "Stopped; completed videos kept"
                    else:
                        job["status"], job["progress"] = "completed", 100
                        job["message"] = f"Completed {job['count']} MP4 files"
                    self.save()
            except Exception as exc:
                with self.lock:
                    if self.control:
                        job["status"], job["message"] = self.control, "Stopped; completed videos kept"
                    else:
                        job["status"], job["message"] = "failed", str(exc)[-500:]
                    self.save()
            finally:
                with self.lock:
                    self.process = None
                    self.control = ""

    def _render(self, job: dict):
        count = job["count"]
        for index in range(job["cursor"], count):
            with self.lock:
                if self.control:
                    return
            output = Path(job["output"]) / f"Short_{job['id']}_{index + 1:03d}.mp4"
            partial = output.with_name(output.stem + ".partial.mp4")
            if output.exists():
                if not verified_video(output, job["seconds"]):
                    raise RuntimeError(f"Existing output needs manual inspection: {output.name}")
                with self.lock:
                    job["cursor"] = index + 1
                    self.save()
                continue
            if partial.exists():
                partial.unlink()
            audio = picked_audio(job["audios"], job["seed"], index)
            command = build_command(job["images"], audio, str(partial),
                                    seconds=job["seconds"], image_seconds=job["image_seconds"],
                                    seed=job["seed"] + index * 1709)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            last_error = ""
            try:
                proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace",
                                        bufsize=1, creationflags=flags)
                with self.lock:
                    self.process = proc
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.strip()
                    if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                        try:
                            microseconds = int(line.split("=", 1)[1])
                            percentage = min(0.99, microseconds / (job["seconds"] * 1_000_000))
                            with self.lock:
                                job["progress"] = round(100 * (index + percentage) / count, 1)
                                job["message"] = f"Rendering {index + 1}/{count}"
                                self.save()
                        except ValueError:
                            pass
                    elif line and not line.startswith(("progress=", "frame=", "fps=", "bitrate=",
                                                      "total_size=", "out_time=", "speed=",
                                                      "dup_frames=", "drop_frames=", "stream_")):
                        last_error = line[-500:]
                code = proc.wait()
            finally:
                with self.lock:
                    self.process = None
            with self.lock:
                stopped = bool(self.control)
            if stopped:
                partial.unlink(missing_ok=True)
                return
            if code != 0 or not verified_video(partial, job["seconds"]):
                partial.unlink(missing_ok=True)
                raise RuntimeError(last_error or f"FFmpeg failed or invalid output (exit {code})")
            if output.exists():
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"Refusing to overwrite existing video {output.name}")
            partial.rename(output)
            with self.lock:
                job["cursor"] = index + 1
                job["progress"] = round(100 * (index + 1) / count, 1)
                job["message"] = f"Saved {index + 1}/{count}"
                self.save()

    def close(self):
        with self.lock:
            self.control = "paused"
            self._terminate()
            for job in self.jobs:
                if job["status"] == "running":
                    job["status"], job["message"] = "paused", "App closed; resume to continue"
            self.save()


class ShortMakerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Short Maker Desktop")
        root.geometry("1010x710")
        root.minsize(820, 600)
        root.configure(bg="#111827")
        self.queue = RenderQueue()
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.duration_var = tk.StringVar(value="16")
        self.image_var = tk.StringVar(value="3")
        self.count_var = tk.StringVar(value="1")
        self.media_var = tk.StringVar(value="Select a source folder to detect images and audio")
        self.audio_var = tk.StringVar(value="No extra audio chosen (optional)")
        self.extras: list[str] = []
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background="#111827", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(20, 12), font=("Segoe UI", 11, "bold"))
        style.configure("TFrame", background="#111827")
        style.configure("TLabel", background="#111827", foreground="#E5E7EB",
                        font=("Segoe UI", 10))
        style.configure("TButton", padding=(9, 7), font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=33, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        header = tk.Frame(root, bg="#111827")
        header.pack(fill="x", padx=24, pady=(20, 10))
        tk.Label(header, text="Short Maker", font=("Segoe UI", 23, "bold"),
                 fg="white", bg="#111827").pack(side="left")
        tk.Label(header, text="LOCAL • OFFLINE • RANDOM EFFECTS",
                 font=("Segoe UI", 10), fg="#B9A7FF", bg="#111827").pack(side="right")
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True, padx=20, pady=8)
        maker = ttk.Frame(self.tabs)
        queue_tab = ttk.Frame(self.tabs)
        settings = ttk.Frame(self.tabs)
        self.tabs.add(maker, text="Maker")
        self.tabs.add(queue_tab, text="Queue")
        self.tabs.add(settings, text="Settings")
        self._maker(maker)
        self._queue_tab(queue_tab)
        self._settings(settings)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh()

    def _row(self, parent, title, var, choose, index):
        ttk.Label(parent, text=title).grid(row=index, column=0, sticky="w", pady=13)
        ttk.Entry(parent, textvariable=var, width=74).grid(row=index, column=1,
                                                             sticky="ew", padx=12)
        ttk.Button(parent, text="Browse…", command=choose).grid(row=index, column=2)
        parent.grid_columnconfigure(1, weight=1)

    def _maker(self, panel):
        container = ttk.Frame(panel, padding=20)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="One input folder • automatic audio detection",
                  font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(4, 16))
        source = ttk.Frame(container)
        source.pack(fill="x")
        self._row(source, "Input folder *", self.input_var, self.choose_input, 0)
        self._row(source, "Output folder *", self.output_var, self.choose_output, 1)
        ttk.Label(container, textvariable=self.media_var).pack(anchor="w", pady=(9, 6))
        audio = ttk.Frame(container)
        audio.pack(fill="x", pady=10)
        ttk.Button(audio, text="Add optional audio files", command=self.choose_audio).pack(side="left")
        ttk.Button(audio, text="Clear extras", command=self.clear_audio).pack(side="left", padx=10)
        ttk.Label(audio, textvariable=self.audio_var).pack(side="left", padx=10)
        opts = ttk.Frame(container)
        opts.pack(fill="x", pady=(18, 14))
        for column, (label, value) in enumerate(
                (("Video duration (sec)", self.duration_var),
                 ("Image duration (sec)", self.image_var),
                 ("Number of videos", self.count_var))):
            box = ttk.Frame(opts)
            box.grid(row=0, column=column, padx=(0, 24), sticky="w")
            ttk.Label(box, text=label).pack(anchor="w")
            ttk.Entry(box, textvariable=value, width=17).pack(pady=8)
        ttk.Label(container, text="Automatic: random pictures + audio + Concat-style animations / transitions",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=12)
        ttk.Label(container, text="1080×1920 • 9:16 • 30 FPS • MP4. No Drive upload, no profiles.",
                  font=("Segoe UI", 10)).pack(anchor="w", pady=4)
        ttk.Button(container, text="Generate Videos → Add to Queue",
                   command=self.add_batch).pack(fill="x", pady=(26, 8))
        ttk.Label(container, text="Files stay in your output folder. Upload to Google Drive yourself.").pack(anchor="w")

    def _queue_tab(self, panel):
        holder = ttk.Frame(panel, padding=16)
        holder.pack(fill="both", expand=True)
        fields = ("id", "status", "count", "progress", "message")
        self.table = ttk.Treeview(holder, columns=fields, show="headings", height=13)
        labels = {"id": "Batch", "status": "Status", "count": "Videos",
                  "progress": "Progress", "message": "Details"}
        widths = {"id": 125, "status": 95, "count": 75, "progress": 90, "message": 440}
        for name in fields:
            self.table.heading(name, text=labels[name])
            self.table.column(name, width=widths[name], anchor="w",
                              stretch=name == "message")
        self.table.pack(fill="both", expand=True)
        actions = ttk.Frame(holder)
        actions.pack(fill="x", pady=12)
        for name, function in (
            ("Start Queue", self.queue.start), ("Pause", lambda: self.job_action("pause")),
            ("Resume", lambda: self.job_action("resume")),
            ("Cancel", lambda: self.job_action("cancel")),
            ("Retry", lambda: self.job_action("retry")),
            ("Delete Row", lambda: self.job_action("delete")),
            ("Open Output", self.open_output),
        ):
            ttk.Button(actions, text=name, command=function).pack(side="left", padx=(0, 6))
        ttk.Label(holder, text="Pause restarts the current partial render; already completed MP4s are preserved.").pack(anchor="w")

    def _settings(self, panel):
        box = ttk.Frame(panel, padding=25)
        box.pack(fill="both", expand=True)
        for text in (
            "Short Maker Desktop — Windows x64",
            "Output: 1080 × 1920 / H.264 / 30 FPS / CRF 23",
            "Animations: automatic random zoom / pan / physics-inspired motion",
            "Transitions: automatic random Concat-inspired FFmpeg approximations",
            "Audio: automatically detected inside input folder + optional additional audio",
            "Rendered videos: PC output folder only. No Drive authentication or upload.",
            "Queue persistence: local app data; input/output videos are never auto-deleted.",
        ):
            ttk.Label(box, text=text).pack(anchor="w", pady=12)

    def choose_input(self):
        folder = filedialog.askdirectory(title="Select one input folder")
        if folder:
            self.input_var.set(folder)
            try:
                pics, audio = scan_folder(folder)
                self.media_var.set(f"Detected: {len(pics)} pictures • {len(audio)} audio files (random per video)")
            except Exception as exc:
                self.media_var.set(str(exc))

    def choose_output(self):
        folder = filedialog.askdirectory(title="Select output folder")
        if folder:
            self.output_var.set(folder)

    def choose_audio(self):
        names = filedialog.askopenfilenames(title="Optional extra audio",
            filetypes=[("Audio files", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus *.wma"),
                       ("All files", "*.*")])
        self.extras = sorted(set(self.extras + list(names)))
        self.audio_var.set(f"{len(self.extras)} extra audio file(s)")

    def clear_audio(self):
        self.extras = []
        self.audio_var.set("No extra audio chosen (optional)")

    def add_batch(self):
        try:
            self.queue.enqueue(self.input_var.get(), self.output_var.get(), self.extras,
                               float(self.duration_var.get()), float(self.image_var.get()),
                               int(self.count_var.get()))
            self.tabs.select(1)
            self.queue.start()
        except Exception as exc:
            messagebox.showerror("Short Maker", str(exc))

    def selected_job(self):
        selected = self.table.selection()
        if not selected:
            raise ValueError("Select a row from the queue first.")
        return selected[0]

    def job_action(self, action):
        try:
            self.queue.action(self.selected_job(), action)
        except Exception as exc:
            messagebox.showerror("Queue", str(exc))

    def open_output(self):
        try:
            with self.queue.lock:
                directory = self.queue.item(self.selected_job())["output"]
            if os.name == "nt":
                os.startfile(directory)
            else:
                subprocess.Popen(["xdg-open", directory])
        except Exception as exc:
            messagebox.showerror("Output", str(exc))

    def _refresh(self):
        selected = self.table.selection()
        with self.queue.lock:
            snapshots = [dict(job) for job in self.queue.jobs]
        current = set(self.table.get_children())
        active = set()
        for job in snapshots:
            jid = job["id"]
            active.add(jid)
            values = (jid, job["status"], f"{job['cursor']}/{job['count']}",
                      f"{job.get('progress', 0):.1f}%", job.get("message", ""))
            if jid in current:
                self.table.item(jid, values=values)
            else:
                self.table.insert("", "end", iid=jid, values=values)
        for old in current - active:
            self.table.delete(old)
        for jid in selected:
            if jid in active:
                self.table.selection_set(jid)
        self.root.after(750, self._refresh)

    def close(self):
        self.queue.close()
        self.root.destroy()


def main():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    try:
        ShortMakerApp(root)
    except Exception as exc:
        messagebox.showerror("Short Maker startup", str(exc))
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()

"""Concat Studio: safe Z Builder scene-plan -> native Concat MCP project bridge.

Only the native Concat editor writes concat.json. This module never fabricates
Concat documents or modifies the Linux renderer's pinned native engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import queue
import re
import subprocess
import threading
import uuid
from typing import Callable


IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEOS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
AUDIO = {".mp3", ".wav", ".m4a", ".flac"}
SCENE = re.compile(
    r"^\s*(.*?)\s*-->\s*(.*?)\s*\|\s*(image|video)\s*\|\s*([^|]+)(.*)$",
    re.IGNORECASE,
)
TIME = re.compile(r"^\d+(?::\d{1,2}){0,2}(?:[.,]\d{1,3})?$")
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class StudioError(ValueError):
    pass


@dataclass(frozen=True)
class Shot:
    start: float
    end: float
    kind: str
    requested: str
    source_in: float = 0.0
    text: str = ""
    file: Path | None = None

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


@dataclass(frozen=True)
class Plan:
    name: str
    width: int
    height: int
    fps: int
    shots: tuple[Shot, ...]

    @property
    def duration(self) -> float:
        return self.shots[-1].end if self.shots else 0


def seconds(value: str) -> float:
    value = value.strip().replace(",", ".")
    if not TIME.fullmatch(value):
        raise StudioError("Invalid timestamp: " + value)
    fields = value.split(":")
    if len(fields) >= 2 and float(fields[-1]) >= 60:
        raise StudioError("Seconds must be below 60: " + value)
    if len(fields) == 3 and int(fields[-2]) >= 60:
        raise StudioError("Minutes must be below 60: " + value)
    n = 0.0
    for part in fields:
        n = n * 60 + float(part)
    return round(n, 3)


def parse_plan(source: str) -> Plan:
    """Z Builder V6.3 simple timed scene-plan format, strict and gapless."""
    settings = {"name": "Concat Video", "aspect": "16:9", "resolution": "1080p", "fps": "30"}
    shots: list[Shot] = []
    for line_no, raw in enumerate(source.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        match = SCENE.fullmatch(line)
        if match:
            start, end = seconds(match.group(1)), seconds(match.group(2))
            kind, requested = match.group(3).lower(), match.group(4).strip()
            extras = match.group(5).strip()
            source_in, caption = 0.0, ""
            for part in extras.split("|"):
                part = part.strip()
                if not part:
                    continue
                if part.startswith("in="):
                    try:
                        source_in = float(part[3:])
                    except ValueError as exc:
                        raise StudioError("Invalid in= at line " + str(line_no)) from exc
                elif part.startswith("text="):
                    caption = part[5:]
                else:
                    raise StudioError("Unknown scene option at line " + str(line_no) + ": " + part)
            if not requested or start < 0 or end <= start or source_in < 0:
                raise StudioError("Invalid scene range/media at line " + str(line_no))
            if abs(start - (shots[-1].end if shots else 0)) > 0.001:
                raise StudioError("Gap/overlap at line " + str(line_no))
            shots.append(Shot(start, end, kind, requested, source_in, caption))
            continue
        if "=" in line and not shots:
            key, val = [x.strip() for x in line.split("=", 1)]
            key = {"aspect_ratio": "aspect", "project": "name"}.get(key.lower(), key.lower())
            if key not in settings or not val:
                raise StudioError("Unknown/empty setting at line " + str(line_no))
            settings[key] = val
            continue
        raise StudioError("Invalid Scene Plan at line " + str(line_no))
    if not shots:
        raise StudioError("Scene Plan has no shots")
    if len(shots) > 10000:
        raise StudioError("Too many scenes")
    if settings["resolution"] not in ("1080p", "720p", "4k"):
        raise StudioError("Supported resolution: 720p / 1080p / 4k")
    if settings["aspect"] not in ("16:9", "9:16"):
        raise StudioError("Aspect must be 16:9 or 9:16")
    try:
        fps = int(settings["fps"])
    except ValueError as exc:
        raise StudioError("FPS must be an integer") from exc
    if fps not in (24, 25, 30, 60):
        raise StudioError("FPS must be 24 / 25 / 30 / 60")
    w, h = {"720p": (1280, 720), "1080p": (1920, 1080), "4k": (3840, 2160)}[settings["resolution"]]
    if settings["aspect"] == "9:16":
        w, h = h, w
    return Plan(settings["name"], w, h, fps, tuple(shots))


def assign_media(plan: Plan, folder: Path) -> Plan:
    folder = Path(folder).expanduser().resolve(strict=True)
    if not folder.is_dir():
        raise StudioError("Media folder is not a directory")
    files = [p for p in folder.rglob("*") if p.is_file() and not p.is_symlink()
             and p.suffix.lower() in IMAGES | VIDEOS]
    index: dict[str, list[Path]] = {}
    for file in files:
        index.setdefault(file.name.casefold(), []).append(file)
    bound = []
    for i, shot in enumerate(plan.shots, 1):
        requested = shot.requested
        kind_ext = IMAGES if shot.kind == "image" else VIDEOS
        if requested.lower().startswith("keyword:"):
            words = requested[8:].casefold().strip().split()
            candidates = [p for p in files if p.suffix.lower() in kind_ext
                          and all(word in p.stem.casefold() for word in words)]
        else:
            # Only match a basename. No arbitrary absolute path from a plan.
            if Path(requested).name != requested or "\\" in requested or "/" in requested:
                raise StudioError("Scene " + str(i) + ": media must be a filename")
            candidates = [p for p in index.get(requested.casefold(), [])
                          if p.suffix.lower() in kind_ext]
        if len(candidates) != 1:
            raise StudioError("Scene " + str(i) + ": " + requested +
                              (" missing" if not candidates else " ambiguous (duplicate matches)"))
        bound.append(Shot(shot.start, shot.end, shot.kind, requested,
                          shot.source_in, shot.text, candidates[0]))
    return Plan(plan.name, plan.width, plan.height, plan.fps, tuple(bound))


def parse_srt(source: str, total: float) -> list[dict]:
    captions = []
    for block in re.split(r"\n\s*\n", source.replace("\r\n", "\n").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        when = next((j for j, line in enumerate(lines) if "-->" in line), -1)
        if when < 0:
            if lines:
                raise StudioError("SRT block has no time range")
            continue
        left, right = lines[when].split("-->", 1)
        start, end = seconds(left), seconds(right.split(" ")[0])
        words = " ".join(lines[when + 1:]).strip()
        if not words or start < 0 or end <= start or end > total + 0.01:
            raise StudioError("SRT caption has invalid text/timing")
        captions.append({"start": start, "end": min(end, total), "text": words})
    if any(b["start"] < a["start"] for a, b in zip(captions, captions[1:])):
        raise StudioError("SRT captions are out of order")
    return captions


def safe_name(name: str) -> str:
    name = INVALID_NAME.sub("_", name).strip(" .")
    if not name or name.upper() in {"CON", "PRN", "AUX", "NUL"}:
        raise StudioError("Invalid project name")
    return name[:90]


class ConcatMCP:
    """Uses Concat's documented JSON-RPC MCP stdio protocol; never edits JSON by hand."""
    def __init__(self, executable: Path, roots: list[Path]):
        self.executable = Path(executable)
        if not self.executable.is_file():
            raise StudioError("Concat executable not found")
        self.roots = roots
        self.proc = None
        self.replies = queue.Queue()
        self.sequence = 0

    def __enter__(self):
        env = os.environ.copy()
        env["CONCAT_MCP_ROOTS"] = os.pathsep.join(str(Path(p).resolve()) for p in self.roots)
        self.proc = subprocess.Popen([str(self.executable), "--mcp", "headless"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True,
                                     bufsize=1, env=env)
        def reader():
            try:
                for line in self.proc.stdout:
                    try:
                        self.replies.put(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            finally:
                self.replies.put(None)
        threading.Thread(target=reader, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2025-11-25",
                     "capabilities": {}, "clientInfo": {"name": "Concat Studio", "version": "1.0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def send(self, payload: dict):
        if not self.proc or self.proc.poll() is not None:
            raise StudioError("Concat automation process stopped")
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        target = self.sequence
        self.send({"jsonrpc": "2.0", "id": target, "method": method, "params": params})
        while True:
            try:
                msg = self.replies.get(timeout=90)
            except queue.Empty as exc:
                raise StudioError("Concat automation response timed out") from exc
            if msg is None:
                raise StudioError("Concat automation stopped without a response")
            if msg.get("id") != target:
                continue  # progress notifications
            if "error" in msg:
                raise StudioError("Concat API: " + str(msg["error"])[:250])
            return msg.get("result", {})

    def call(self, name: str, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise StudioError("Concat " + name + ": " + str(result.get("content", []))[:300])
        data = result.get("structuredContent")
        if not isinstance(data, dict):
            raise StudioError("Concat returned no structured data for " + name)
        return data

    def __exit__(self, *_):
        if self.proc:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None


def build_project(executable: Path, plan: Plan, root: Path, *,
                  narration: Path | None = None, captions: list[dict] | None = None,
                  progress: Callable[[str], None] | None = None) -> Path:
    """Preflights all files; saves a native Concat project with exact scene timing.

    The project is unique; existing projects and original media are never overwritten.
    """
    progress = progress or (lambda msg: None)
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise StudioError("Project location must be an existing directory")
    project_name = safe_name(plan.name)
    destination = root / project_name
    if destination.exists():
        raise StudioError("Project already exists: " + str(destination))
    for shot in plan.shots:
        if not shot.file or not shot.file.is_file():
            raise StudioError("Unresolved shot: " + shot.requested)
        if shot.kind == "video" and shot.source_in < 0:
            raise StudioError("Negative source in-point")
    if narration and (not narration.is_file() or narration.suffix.lower() not in AUDIO):
        raise StudioError("Narration must be a local MP3/WAV/M4A/FLAC file")
    captions = captions or []
    for cap in captions:
        if not cap.get("text") or not 0 <= cap["start"] < cap["end"] <= plan.duration + 0.01:
            raise StudioError("Invalid captions")
    roots = [root, *{p.file.parent for p in plan.shots if p.file}]
    if narration:
        roots.append(narration.parent)
    with ConcatMCP(executable, roots) as api:
        api.call("project_create", location=str(root), name=project_name,
                 video={"width": plan.width, "height": plan.height,
                        "rateNum": plan.fps, "rateDen": 1})
        try:
            overview = api.call("timeline_read", path=str(destination))
            tracks = overview.get("timeline", {}).get("tracks", [])
            if not tracks:
                raise StudioError("Concat project has no visual track")
            # Concat lists tracks top to bottom; bottom track holds the visual cut.
            visual = tracks[-1]["id"]
            imported = {}
            for i, shot in enumerate(plan.shots, 1):
                progress("Scene " + str(i) + "/" + str(len(plan.shots)))
                file = str(shot.file)
                if file not in imported:
                    item = api.call("media_import", path=str(destination), file=file)
                    media = next((m for m in item.get("media", []) if
                                  os.path.normcase(m.get("path", "")) == os.path.normcase(file)), None)
                    if not media:
                        raise StudioError("Concat did not import: " + shot.file.name)
                    imported[file] = media
                media = imported[file]
                length = media.get("duration")
                if shot.kind == "video" and length is not None and shot.source_in + shot.duration > float(length) + 0.05:
                    raise StudioError(shot.file.name + ": scene exceeds source video length")
                added = api.call("clip_add", path=str(destination), mediaId=media["id"],
                                 trackId=visual, start=shot.start, ripple=False)
                clip_id = added.get("createdId")
                if not clip_id:
                    raise StudioError("Concat did not create a clip for scene " + str(i))
                native = next((c for c in added["timeline"]["clips"] if c["id"] == clip_id), None)
                if not native:
                    raise StudioError("Concat could not read the new clip")
                if shot.source_in:
                    if shot.kind != "video" or shot.source_in >= native["duration"] - 1/60:
                        raise StudioError("Invalid source in-point for " + shot.file.name)
                    # Magnetic head trim preserves the timeline start, advancing sourceStart.
                    api.call("clip_trim", path=str(destination), clipId=clip_id,
                             edge="start", delta=shot.source_in, ripple=True)
                    native["duration"] -= shot.source_in
                difference = round(shot.duration - native["duration"], 4)
                if abs(difference) > 0.001:
                    api.call("clip_trim", path=str(destination), clipId=clip_id,
                             edge="end", delta=difference, ripple=False)
                if shot.kind == "video" and narration:
                    api.call("clip_update", path=str(destination), clipId=clip_id,
                             patch={"muted": True})
                checked = api.call("timeline_read", path=str(destination))
                after = next((c for c in checked["timeline"]["clips"] if c["id"] == clip_id), None)
                if not after or abs(after["start"]-shot.start) > 0.04 or abs(after["duration"]-shot.duration) > 0.04 or abs(after.get("sourceStart", 0)-shot.source_in) > 0.04:
                    raise StudioError("Concat timing mismatch in scene " + str(i))
            if narration:
                voice = api.call("media_import", path=str(destination), file=str(narration))
                item = next((m for m in voice["media"] if
                             os.path.normcase(m.get("path", "")) == os.path.normcase(str(narration))), None)
                if not item:
                    raise StudioError("Narration media import failed")
                lane = api.call("track_add", path=str(destination), bottom=True)
                voice_track = lane.get("createdId")
                if not voice_track:
                    raise StudioError("Narration track could not be created")
                added = api.call("clip_add", path=str(destination), mediaId=item["id"],
                                 trackId=voice_track, start=0.0, ripple=False)
                cid = added.get("createdId")
                duration = next(c["duration"] for c in added["timeline"]["clips"] if c["id"] == cid)
                if duration > plan.duration:
                    api.call("clip_trim", path=str(destination), clipId=cid, edge="end",
                             delta=plan.duration-duration, ripple=False)
            if captions:
                api.call("captions_add", path=str(destination), captions=captions, offsetY=0.35)
            # Include any inline scene text as additional editable titles.
            inline = [{"start": s.start, "end": s.end, "text": s.text}
                      for s in plan.shots if s.text]
            if inline:
                api.call("captions_add", path=str(destination), captions=inline, offsetY=-0.3)
            progress("Saving native Concat project")
            api.call("project_save", path=str(destination))
        finally:
            try:
                api.call("project_close", path=str(destination), save=False)
            except Exception:
                pass
    if not (destination / "concat.json").is_file():
        raise StudioError("Concat did not write concat.json")
    return destination

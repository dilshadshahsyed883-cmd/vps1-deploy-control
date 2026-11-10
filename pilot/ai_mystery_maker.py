"""One-job, non-deploying AI mystery-video prototype.

External services are explicitly configured; missing dependencies fail closed.
Does not read or mutate existing VPS databases, queues, models, or production files.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class MakerError(RuntimeError):
    pass


def request_json(url: str, payload: dict | None = None, timeout: int = 120) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise MakerError("Unexpected service response")
    return result


def request_bytes(url: str, timeout: int = 120) -> bytes:
    with urlopen(Request(url), timeout=timeout) as response:
        return response.read()


def base(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or parsed.port is None):
        raise MakerError("Prototype endpoints must be localhost HTTP with a port; use an SSH tunnel for remote workers")
    return f"http://{parsed.hostname}:{parsed.port}"


def safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")[:48] or "story"


def valid_story(data: dict) -> dict:
    if not isinstance(data, dict):
        raise MakerError("AI response must be a JSON object")
    title, narration, scenes = data.get("title"), data.get("narration"), data.get("scenes")
    if not isinstance(title, str) or not 5 <= len(title.strip()) <= 100:
        raise MakerError("Invalid story title")
    if not isinstance(narration, str) or not 70 <= len(narration.split()) <= 180:
        raise MakerError("Expected 70-180 narrated words")
    if not isinstance(scenes, list) or not 6 <= len(scenes) <= 10:
        raise MakerError("Expected 6-10 storyboard scenes")
    for scene in scenes:
        if not isinstance(scene, dict) or not isinstance(scene.get("prompt"), str) or not 15 <= len(scene["prompt"]) <= 700:
            raise MakerError("Invalid image prompt")
    return {"title": title.strip(), "narration": narration.strip(), "scenes": [{"prompt": s["prompt"].strip()} for s in scenes]}


def script_from_ollama(topic: str, url: str, model: str) -> dict:
    if not 5 <= len(topic.strip()) <= 200:
        raise MakerError("Topic must be 5-200 characters")
    instruction = (
        "Write one ORIGINAL fictional American-English sci-fi mystery short story. "
        "Avoid impersonating real people or presenting fiction as real news. "
        "Return ONLY JSON with keys title, narration, scenes. "
        "Narration must be 115-140 words, strong hook in the first sentence, escalating suspense, "
        "and a satisfying coherent twist ending. Scenes must be exactly 8 objects, each with one "
        "detailed cinematic 9:16 image prompt; maintain consistent main character and setting. "
        "Do not put text, letters, captions, watermarks, or UI inside image prompts. "
        f"Story concept: {topic.strip()}"
    )
    response = request_json(base(url) + "/api/generate", {
        "model": model, "prompt": instruction, "format": "json", "stream": False,
        "options": {"temperature": 0.85},
    }, timeout=240)
    try:
        return valid_story(json.loads(response["response"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise MakerError("Ollama did not return valid story JSON") from exc


def synthesize_kokoro(narration: str, url: str, folder: Path, *, voice: str = "am_michael", language: str = "en-us", timeout: int = 420) -> Path:
    host = base(url)
    info = request_json(host + "/tts/api/kokoro/generate", {
        "text": narration, "language": language, "voice": voice, "speed": 1.0,
    })
    job_id = info.get("job_id", "")
    if not re.fullmatch(r"KOJ-[A-F0-9]{16}", str(job_id)):
        raise MakerError("Kokoro returned invalid job ID")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = request_json(host + "/tts/api/kokoro/jobs?limit=250").get("jobs", [])
        record = next((j for j in jobs if j.get("job_id") == job_id), None)
        if record and record.get("status") in ("failed", "error", "cancelled", "canceled"):
            raise MakerError("Kokoro synthesis failed")
        if record and record.get("audio_url"):
            data = request_bytes(host + "/tts/api/kokoro/audio/" + job_id)
            if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                raise MakerError("Invalid WAV returned from Kokoro")
            path = folder / "narration.wav"
            path.write_bytes(data)
            return path
        time.sleep(3)
    raise MakerError("Kokoro timeout; check worker status and job ownership")


def generate_comfy_image(prompt: str, url: str, workflow: dict, prompt_node: str, target: Path, *, timeout: int = 420) -> Path:
    host = base(url)
    graph = copy.deepcopy(workflow)
    try:
        graph[prompt_node]["inputs"]["text"] = prompt
    except (KeyError, TypeError) as exc:
        raise MakerError("Workflow prompt node must expose inputs.text") from exc
    submit = request_json(host + "/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex})
    prompt_id = submit.get("prompt_id", "")
    if not re.fullmatch(r"[a-fA-F0-9-]{16,64}", str(prompt_id)):
        raise MakerError("ComfyUI rejected prompt; inspect workflow/model installation")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = request_json(host + "/history/" + prompt_id).get(prompt_id)
        if result:
            if result.get("status", {}).get("status_str") == "error":
                raise MakerError("ComfyUI generation failed")
            for output in result.get("outputs", {}).values():
                images = output.get("images", [])
                if images:
                    metadata = images[0]
                    query = urlencode({"filename": metadata["filename"],
                                     "subfolder": metadata.get("subfolder", ""),
                                     "type": metadata.get("type", "output")})
                    image = request_bytes(host + "/view?" + query)
                    if not (image.startswith(b"\x89PNG\r\n\x1a\n") or image.startswith(b"\xff\xd8\xff")):
                        raise MakerError("ComfyUI returned non-image payload")
                    target.write_bytes(image)
                    return target
        time.sleep(3)
    raise MakerError("ComfyUI timeout; generated assets are retained by worker")


def run(command: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MakerError("Required video tool failed or timed out") from exc
    if result.returncode:
        raise MakerError(f"Media tool failed ({command[0]}): {(result.stderr or '')[-500:]}")
    return result.stdout


def srt_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    h, milliseconds = divmod(milliseconds, 3600000)
    m, milliseconds = divmod(milliseconds, 60000)
    s, milliseconds = divmod(milliseconds, 1000)
    return f"{h:02}:{m:02}:{s:02},{milliseconds:03}"


def captions(narration: str, count: int, seconds: float) -> str:
    words = narration.split()
    lines = []
    # Even segments for MVP; alignment is approximate, not word-level transcription.
    for i in range(count):
        a, b = round(i * len(words) / count), round((i + 1) * len(words) / count)
        start, end = i * seconds / count, (i + 1) * seconds / count
        lines.append(f"{i + 1}\n{srt_time(start)} --> {srt_time(end)}\n{' '.join(words[a:b])}\n")
    return "\n".join(lines) + "\n"


def ffconcat_quote(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "'\\''") + "'"


def render(images: list[Path], voice: Path, narration: str, folder: Path) -> Path:
    if not images:
        raise MakerError("No images to render")
    try:
        stats = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "json", str(voice)]))
        duration = float(stats["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise MakerError("Cannot measure voice duration") from exc
    if not 15 <= duration <= 120:
        raise MakerError("Voice duration outside safe Shorts prototype range (15-120s)")
    slice_s = duration / len(images)
    playlist = ["ffconcat version 1.0"]
    for image in images:
        playlist += ["file " + ffconcat_quote(image), f"duration {slice_s:.6f}"]
    playlist.append("file " + ffconcat_quote(images[-1]))
    (folder / "timeline.ffconcat").write_text("\n".join(playlist) + "\n", encoding="utf-8")
    (folder / "captions.srt").write_text(captions(narration, len(images), duration), encoding="utf-8")
    output = folder / "video.mp4"
    run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-safe", "0", "-f", "concat",
         "-i", "timeline.ffconcat", "-i", "narration.wav", "-map", "0:v:0", "-map", "1:a:0",
         "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                "fps=30,format=yuv420p,subtitles=captions.srt",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac",
         "-b:a", "160k", "-t", f"{duration:.3f}", "-movflags", "+faststart", "video.mp4"], cwd=folder)
    if not output.is_file() or output.stat().st_size < 1000:
        raise MakerError("FFmpeg did not produce a valid output file")
    return output


def remote_md5(remote: str) -> str | None:
    """Distinguish a genuinely missing object from API/auth/quota errors."""
    try:
        proc = subprocess.run(["rclone", "md5sum", remote], capture_output=True,
                              text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MakerError("Drive verification command failed") from exc
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).lower()
        if any(marker in detail for marker in ("directory not found", "file not found", "object not found", "path not found", "doesn't exist", "not found")):
            return None
        raise MakerError("Drive verification error; refusing unverified upload")
    rows = proc.stdout.strip().splitlines()
    if not rows:
        return None
    candidate = rows[0].split()[0].lower()
    if not re.fullmatch(r"[a-f0-9]{32}", candidate):
        raise MakerError("Drive did not return a valid MD5 receipt")
    return candidate


def upload_verified(video: Path, remote: str) -> str:
    """Optional rclone/Drive Bridge handoff. Never overwrite, delete, or fabricate a Drive URL."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9 _./-]+", remote) or ".." in remote.split(":", 1)[1].split("/"):
        raise MakerError("Drive remote must be an explicit safe rclone destination")
    md5 = hashlib.md5(video.read_bytes()).hexdigest()
    existing = remote_md5(remote)
    if existing:
        if existing == md5:
            return remote
        raise MakerError("Drive destination exists with different content; refusing overwrite")
    run(["rclone", "copyto", "--ignore-existing", str(video), remote])
    if remote_md5(remote) != md5:
        raise MakerError("Drive upload not verified; local MP4 retained")
    return remote


def produce(topic: str, folder: Path, config: dict, *, drive_remote: str = "") -> dict:
    job = safe_name(topic) + "-" + uuid.uuid4().hex[:8]
    output = folder / job
    output.mkdir(parents=True, exist_ok=False)
    (output / "scenes").mkdir(mode=0o700)
    state = {"job": job, "topic": topic, "status": "running", "files": {}}
    state_file = output / "result.json"
    def save():
        state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    save()
    try:
        story = script_from_ollama(topic, config["ollama_url"], config.get("ollama_model", "qwen3:4b"))
        (output / "story.json").write_text(json.dumps(story, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        voice = synthesize_kokoro(story["narration"], config["kokoro_url"], output,
                                 voice=config.get("voice", "am_michael"))
        graph = json.loads(Path(config["comfy_workflow"]).read_text(encoding="utf-8"))
        images = []
        for idx, scene in enumerate(story["scenes"], 1):
            # The workflow must use a locally configured FLUX.1-schnell checkpoint/model.
            image = generate_comfy_image(scene["prompt"], config["comfy_url"], graph,
                                         str(config["comfy_prompt_node"]), output / "scenes" / f"{idx:02}.png")
            images.append(image)
        video = render(images, voice, story["narration"], output)
        state["files"] = {"video": str(video), "story": str(output / "story.json"),
                          "voice": str(voice), "captions": str(output / "captions.srt")}
        if drive_remote:
            state["drive_verified_remote"] = upload_verified(video, drive_remote.rstrip("/") + "/" + job + ".mp4")
        state["status"] = "ready_for_review"
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}" if isinstance(exc, MakerError) else type(exc).__name__
        save()
        raise
    save()
    return state


def main() -> None:
    p = argparse.ArgumentParser(description="Generate one fictional AI mystery Short for manual review")
    p.add_argument("--topic", required=True)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--output", default="./ai-mystery-output", type=Path)
    p.add_argument("--drive-remote", default="", help="Optional rclone remote folder, e.g. gdrive:AI Mystery Videos")
    args = p.parse_args()
    conf = json.loads(args.config.read_text(encoding="utf-8"))
    for key in ("ollama_url", "kokoro_url", "comfy_url", "comfy_workflow", "comfy_prompt_node"):
        if key not in conf:
            p.error(f"Missing config value: {key}")
    for key in ("ollama_url", "kokoro_url", "comfy_url"):
        base(conf[key])
    args.output.mkdir(parents=True, exist_ok=True)
    result = produce(args.topic, args.output, conf, drive_remote=args.drive_remote)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

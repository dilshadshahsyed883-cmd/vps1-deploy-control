"""Single-command 30-second real Concat document CUDA/NVENC hardware acceptance.

Run inside the CUDA candidate Salad container:
    python3 -m cloud_render.concat.gpu.hardware_smoke
No credentials needed for the local self-test. Does not mutate user projects.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from cloud_render.concat.gpu.cuda_backend import probe_cuda, parse_project, render


def fixture(root: Path) -> Path:
    project = root / "project"
    media = project / "media"
    media.mkdir(parents=True)
    stills = []
    for index in range(2):
        path = media / f"scene-{index}.png"
        stills.append(path)
    music = media / "voice.m4a"
    doc = {
        "concat": "0.1.0",
        "version": 1,
        "name": "CUDA 30s acceptance",
        "activeTimelineId": "TL1",
        "media": [
            {"id": "m1", "kind": "image", "name": stills[0].name, "path": str(stills[0])},
            {"id": "m2", "kind": "image", "name": stills[1].name, "path": str(stills[1])},
            {"id": "m3", "kind": "audio", "name": music.name, "path": str(music)},
        ],
        "timelines": [{
            "id": "TL1", "name": "CUDA Test",
            "video": {"width": 1920, "height": 1080, "rateNum": 30, "rateDen": 1},
            "tracks": [{"id": "T1", "visible": True}, {"id": "A1", "muted": False}],
            "clips": [
                {"id": "scene-1", "kind": "image", "trackId": "T1", "mediaId": "m1",
                 "start": 0, "duration": 15, "scale": 1.1,
                 "keys": [
                     {"property": "offsetX", "at": 0, "value": -0.035},
                     {"property": "offsetX", "at": 1, "value": 0.035}
                 ],
                 "videoEffects": [{"id": "unavailable_optional_test_filter", "enabled": True}]},
                {"id": "scene-2", "kind": "image", "trackId": "T1", "mediaId": "m2",
                 "start": 15, "duration": 15, "scale": 1.05},
                {"id": "test-audio", "kind": "audio", "trackId": "A1", "mediaId": "m3",
                 "start": 0, "duration": 30, "volume": 1.0},
            ],
        }]
    }
    (project / "concat.json").write_text(json.dumps(doc, indent=2))
    return project


def ffmpeg_source(project: Path) -> None:
    media = project / "media"
    for index in range(2):
        path = media / f"scene-{index}.png"
        source = "testsrc2=s=1920x1080:r=1:d=1" if index == 0 else "smptebars=s=1920x1080:r=1:d=1"
        p = subprocess.run([
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", source,
            "-frames:v", "1", "-threads:v", "1", str(path)
        ], capture_output=True, text=True, timeout=60)
        if p.returncode:
            raise RuntimeError("Fixture frame failed: " + p.stderr[-400:])
    p = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=30",
        "-c:a", "aac", "-b:a", "128k", "-t", "30", str(media / "voice.m4a")
    ], capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError("Fixture AAC failed: " + p.stderr[-400:])


def main() -> int:
    available = probe_cuda()
    if not available["cuda_nvenc_ready"]:
        print(json.dumps({"result": "FAIL", "reason": "CUDA_NVENC_UNAVAILABLE",
                          "diagnostic": available}, indent=2))
        return 2
    output_dir = Path("/worker/outputs")
    if not output_dir.is_dir():
        output_dir = Path(tempfile.gettempdir())
    output_dir.mkdir(parents=True, exist_ok=True)
    identity = f"cuda-hw-smoke-{int(time.time())}"
    output = output_dir / (identity + ".mp4")
    with tempfile.TemporaryDirectory(prefix="concat-cuda-acceptance-") as scratch:
        project = fixture(Path(scratch))
        ffmpeg_source(project)
        parse_project(project)
        start = time.perf_counter()
        details = render(project, output, job_id=identity)
        elapsed = time.perf_counter() - start
        result = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,width,height,nb_frames:format=duration",
            "-of", "json", str(output)
        ], capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise RuntimeError("Smoke output ffprobe failed")
        streams = json.loads(result.stdout)
        video = next((s for s in streams["streams"] if s["codec_type"] == "video"), None)
        audio = next((s for s in streams["streams"] if s["codec_type"] == "audio"), None)
        duration = float(streams["format"]["duration"])
        if (not video or video.get("codec_name") != "h264"
                or video.get("width") != 1920 or video.get("height") != 1080
                or int(video.get("nb_frames") or 0) != 900
                or not audio or audio.get("codec_name") != "aac"
                or abs(duration - 30) > .25
                or details["skipped_effects"] != 1):
            raise RuntimeError("Smoke MP4 metadata/audio/skip acceptance failed: " +
                               json.dumps(streams)[:500])
        log = project.parent / details["event_log"]
        if not log.is_file() or "unavailable_optional_test_filter" not in log.read_text():
            raise RuntimeError("Effect skip log missing")
        permanent_log = output.with_suffix(".events.jsonl")
        shutil.copyfile(log, permanent_log)
        print(json.dumps({
            "result": "PASS", "hardware": "NVIDIA CUDA + NVENC",
            "duration_s": duration, "render_wall_s": round(elapsed, 2),
            "frames_expected": 900, "effective_fps": round(900 / elapsed, 2),
            "video_codec": "h264", "audio_codec": "aac",
            "skipped_effects": 1, "output": str(output), "event_log": str(permanent_log),
            "output_bytes": output.stat().st_size
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

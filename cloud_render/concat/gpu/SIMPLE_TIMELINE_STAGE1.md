# Concat GPU Stage 1 — simple timeline and voiceover

**Status:** experimental CUDA/NVENC source plus offline tests; no live Salad GPU acceptance yet. No production deployment or existing Concat worker changes.

This is Stage 1 of the approved GPU-only plan. The v0.2.8 Windows Concat executable, existing authenticated GPU worker and existing Concat projects remain untouched. This experiment is a GPU video backend, not yet a full native Concat project renderer.

## Strict initial contract

- Sequential **cuts** of 1–80 clips, one narration track; **1920 × 1080, 30 fps, H.264 MP4**.
- Stage 1 sources: **16:9 JPEG** with hardware MJPEG decode, and 16:9 **H.264 / HEVC video at 30 fps** with NVIDIA decoding. Unsupported media fails, without silently invoking CPU visual decode/processing.
- NVIDIA hardware frame scaling through **scale_cuda**, hardware H.264 encoder **h264_nvenc**. Both must be available on the actual Salad GPU and the FFmpeg build.
- Voice narration can be MP3, WAV, M4A or AAC. FFmpeg encodes final audio to **AAC using CPU**, because NVENC does not encode audio. Voice is padded/trimmed to the picture duration; source clip audio is intentionally not mixed at this stage.
- Output bitrate defaults to **6,000,000 bps**; user may set 2–12 Mbps. For an 18-minute video, 6 Mbps video alone is ~810 MB before audio/container overhead, not 2–3 GB.
- Timeline edits are limited to ordering, clip durations, video source start. Transitions, filters, captions, animations, overlays, UI work, queues and other modifications are **out of scope now**.
- Unknown fields are **rejected**, not silently dropped. Missing clip, non-frame-aligned duration, unsupported codec/aspect or GPU error **fails** the job. Original files are never altered. Existing output names cannot be overwritten.

## Input example

Put a JSON file named simple_timeline.json beside a project directory containing media:

~~~json
{
  "version": 1,
  "width": 1920,
  "height": 1080,
  "fps": 30,
  "clips": [
    {"path": "media/shot1.jpg", "kind": "image", "duration": 3},
    {"path": "media/shot2.mp4", "kind": "video", "source_start": 0, "duration": 5}
  ],
  "voice": "media/voice.mp3",
  "video_bitrate": 6000000
}
~~~

To run on a GPU host where FFmpeg has scale_cuda, NVDEC (including mjpeg_cuvid) and NVENC:

~~~bash
python3 -m cloud_render.concat.gpu.simple_timeline_v1 PROJECT_FOLDER PROJECT_FOLDER/simple_timeline.json /tmp/render.mp4
~~~

Use --dry-run to check the plan and built command without rendering.

Offline unit tests:

~~~bash
python3 -m unittest cloud_render.concat.tests.test_gpu_simple_timeline -v
~~~

## Acceptance gates before declaring Stage 1 done

1. Build a **separate candidate image** with a known working FFmpeg + GPU runtime and include this module. Do not replace the running Concat image yet.
2. On Salad RTX 3060 Ti run **30-second mixed image + video cut with voice**, and verify hardware decode, CUDA frame operations, H.264 NVENC, AAC track, exact 30 fps/1080p and audio sync. JPEG GPU decoder availability is a specific risk to verify.
3. Translate a real, sanitized **native concat.json** project to the same operations without changing the Windows editor's saved semantics. This standalone JSON test file is **not** native Concat project-format parity.
4. Reuse the secure chunk uploader, idempotent job receiver and verified VPS transfer only after the native project adapter works. Never create a second unprotected upload path.
5. Run a **5-minute** mixed-media real-project test, measure total PC-to-Salad transfer + rendering + VPS delivery against current 60-minute CapCut render/slow hotspot.
6. Confirm Windows/editor-to-GPU source/version compatibility and rollback. Only then start **Stage 2: transitions**.

This PR is only the first engineering slice; tests without a real NVIDIA GPU **cannot** certify rendering throughput or compatibility.

# Concat GPU Stage 1 — simple timeline and voiceover

**Status (2026-11-07):** First 30-second **JPEG + audio** standalone Salad RTX 3060 Ti smoke **PASS**, reported from the user's actual existing Salad shell (30.000s output, H.264 and AAC, 809267 bytes; module selected h264_nvenc). This is **partial** hardware acceptance, NOT full Stage 1 acceptance: mixed MP4 sources, actual narration/playback synchronization, native Concat project compatibility, authenticated worker integration and VPS delivery remain unverified. Existing Salad group was updated by its owner; no VPS deployment performed by this PR.

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
env -u LD_LIBRARY_PATH python3 -m cloud_render.concat.gpu.simple_timeline_v1 PROJECT_FOLDER PROJECT_FOLDER/simple_timeline.json /tmp/render.mp4
~~~

Use --dry-run to check the plan and built command without rendering.

Offline unit tests:

~~~bash
python3 -m unittest cloud_render.concat.tests.test_gpu_simple_timeline -v
~~~

## Acceptance gates before declaring Stage 1 done

1. **PASS** — Built and published a checksum-addressable feature-branch Concat candidate image to GHCR. User updated the **existing working Salad instance** as requested in the operating rule. Do not create another group or change GPU/CPU/RAM/gateway settings. Clear the native Concat-only LD_LIBRARY_PATH when invoking system FFmpeg to avoid mixing incompatible linked libraries.
2. **PARTIAL PASS** — Existing Salad RTX 3060 Ti rendered **two 15-second JPEGs + synthetic 30-second WAV** into 30.000s MP4 using the Stage 1 CUDA FFmpeg graph and NVENC, with H.264 and AAC verified by ffprobe. **NEXT:** a mixed JPEG + **H.264/HEVC source MP4** test, then genuine narration and first/middle/last-frame checks. The first smoke did not verify audio perception/sync against speech or native Concat project compatibility.
3. Translate a real, sanitized **native concat.json** project to the same operations without changing the Windows editor's saved semantics. This standalone JSON test file is **not** native Concat project-format parity.
4. Reuse the secure chunk uploader, idempotent job receiver and verified VPS transfer only after the native project adapter works. Never create a second unprotected upload path.
5. Run a **5-minute** mixed-media real-project test, measure total PC-to-Salad transfer + rendering + VPS delivery against current 60-minute CapCut render/slow hotspot.
6. Confirm Windows/editor-to-GPU source/version compatibility and rollback. Only then start **Stage 2: transitions**.

This PR is only the first engineering slice; tests without a real NVIDIA GPU **cannot** certify rendering throughput or compatibility.

## First actual RTX 3060 Ti smoke evidence (2026-11-07)

- Environment: existing Salad shell; NVIDIA GeForce RTX 3060 Ti confirmed by `nvidia-smi`.
- Inputs: two 1920×1080 single-frame JPEGs displayed 15 seconds each; 30-second 48 kHz generated WAV tone.
- Renderer stdout reported: `duration=30.0`, `bytes=809267`, `video=h264_nvenc`, `audio=aac-cpu`.
- FFprobe verified `codec_name=h264`, `codec_name=aac`, container duration `30.000000` and size `809267`.
- No errors in the supplied terminal output. **Do not overclaim:** H.264 label alone does not prove hardware decode, tone is not narration, and output was not human-playback-checked. Source code selects CUDA device, `mjpeg_cuvid`, `scale_cuda` and `h264_nvenc` explicitly; actual command returned success.
- Resume work on the **same** Salad group and correct observed errors in place. Do not clone by default or move to transitions until real-video and native-project acceptance passes.

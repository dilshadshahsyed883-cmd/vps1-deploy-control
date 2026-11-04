# Concat Studio — CUDA + NVENC GPU-Only Rendering Plan

**Decision date:** 2026-11-04  
**Status:** APPROVED DESIGN; IMPLEMENTATION / REAL-PROJECT ACCEPTANCE PENDING  
**Scope:** Windows Concat Studio editor + Salad Cloud Linux GPU worker + VPS Cloud Render receiver  
**Canonical project:** `cloud_render/concat/source.lock.json` (`concat.json`, bundle v1)

## 1. Locked decisions

1. **Primary rendering:** NVIDIA CUDA video-frame processing + NVIDIA NVENC H.264/HEVC output. RTX 3060 Ti (8 GiB class) is the current validated candidate GPU; this does not lock out other capable NVIDIA GPUs.
2. **No CPU video-frame fallback.** Zoom/pan, per-frame scaling, compositing, captions/word highlighting, visual animations, transitions, color/effects and image processing must use GPU paths. Never silently select `zoompan`, `xfade`, `subtitles`, `drawtext`, `overlay`, or other CPU pixel/frame-processing filters as replacements for unsupported CUDA functionality.
3. **Unsupported optional GPU effects:** **AUTO-SKIP + STRUCTURED LOG**; preserve the media itself and the original project. Continue rendering without the optional effect only when output duration, media identity, chronology and synchronization remain correct.
4. **Unsafe skip / missing essential media / unavailable GPU:** **FAIL the job with explicit reason.** Never silently drop a required visual clip, truncate the timeline, alter required timing, replace a missing media asset, or claim success for a broken result.
5. **Initial concurrency:** **2 parallel GPU renders**, gated by measured full-project acceptance, worker capacity and resource safety. Existing worker currently has `max_workers=1`; concurrency is *not yet implemented*.
6. **Delivery:** PC project/media -> authenticated resumable Salad upload -> GPU render -> checksum-verified MP4 -> VPS Cloud Render. Manual Drive upload remains available; automatic Drive upload is optional and OFF by default. Keep inputs/failed outputs until verified acknowledgments and cleanup eligibility.
7. **Version parity:** Maintain a single compatible/versioned project and effect contract across Windows EXE and Linux GPU image. Apply `source.lock.json` release rules and test both deliverables before promotion.
8. **Preserve original:** Keep original Windows Concat executable, saved projects, media and production service safe. No Auto Script changes; no unverified production switching.

## 2. Clarification: CPU responsibilities versus GPU rendering

“GPU-only” here is a **hard prohibition on CPU *video-frame processing***, not an assertion that a working computer can avoid the CPU altogether. CPU remains necessary for application control, file and network I/O, parsing configuration, job scheduling, GPU command submission and verification metadata. These activities must not conceal a CPU filter-processing fallback.

**Decode:** Prefer NVIDIA NVDEC for supported compressed video sources. For an input format with no safe GPU decoding/upload path, expose a compatibility gate and fail safely; do not secretly decode/process video frames with CPU as a substituted rendering path. Image asset ingestion requires explicit compatibility tests. Never represent all codecs as GPU-ready without proving the path.

**Audio is a separate acceptance issue:** NVENC does **not** encode audio. The final pipeline must specify an audio-processing/encoding path and prove synchronization; do not advertise fully GPU-only audio or fully CPU-free execution without an implemented/verified solution. No hidden CPU fallback for GPU-designated visual effects.

## 3. Feature capability and integration policy

| Feature | Target implementation | Current evidence / release gate |
|---|---|---|
| Fixed Zoom + animated Pan | `scale_cuda` + animated `overlay_cuda` | Verified in Salad benchmark |
| Multiple layers, moving picture-in-picture | `scale_cuda`, `overlay_cuda` | Four visual layers passed |
| Smooth animated Zoom In/Out | Custom per-frame CUDA transform / tested GPU kernel | **Not yet verified**; cannot substitute CPU `zoompan` |
| Crossfade/dissolve, wipes, slide transitions | CUDA kernels/graphics-compatible GPU transitions | **Pending**; CPU `xfade` forbidden |
| SRT/ASS captions, karaoke word highlighting | GPU glyph raster/atlas + timed GPU compositing, with CPU permitted for text parsing/timing only | **Pending**; CPU text compositing forbidden |
| Text entrance/exit and kinetic animation | GPU keyframe transform / blend | **Pending** |
| Color enhancement, LUTs, filters | Explicit supported CUDA effect registry | **Pending**; skip unsupported optional effects and log |
| Chroma key | Compatible CUDA filter such as `chromakey_cuda`, where available and tested | **Unverified for actual image/container** |
| NVENC final video encode | `h264_nvenc` / other explicitly tested codec | Verified H.264 NVENC |

A published **effect support manifest** must be versioned, used by desktop preflight and server render-time validation, and record: effect ID, params, GPU backend, min driver/filter version, supported formats, required/optional flag and skip safety. Unknown effect identifiers are unsupported, never silently reinterpreted.

## 4. Unsupported-effect behavior — mandatory

At render planning time and immediately before execution:

- **Optional + safe to omit:** skip the effect, render the original clip with its untouched time range, and emit a structured log warning. The video must not lose the clip or shift timing.
- **Required or unsafe to omit:** fail early with a specific error. Examples: missing required source video, invalid timeline, required keyframe geometry, effects whose omission breaks A/V sync or composition correctness.
- **GPU runtime failure mid-effect:** do **not** silently retry that effect on CPU. Retry on the same supported GPU path only if safe/idempotent, otherwise mark job failed while preserving source and diagnostics.
- **No unadvertised quality downgrade:** export metadata and UI show `done_with_skips` (or equivalent explicit warning) and skipped count, never an unqualified “all effects applied” success.
- **Project is immutable for this decision:** log and render-plan adjustments do not rewrite original `concat.json`.

**Required structured log fields:** timestamp (ISO-8601 UTC), job ID, renderer/manifest version, project ID or safe project reference, scene/clip ID, effect ID/name, position/time range, decision (`applied`, `skipped`, `failed`), machine-readable reason code, human-readable reason. Do not log access tokens, user media content, or secrets.

Example (illustrative only):

```json
{"timestamp":"2026-11-04T12:00:00Z","job_id":"studio-example","renderer":"cuda-nvenc-v1","clip_id":"scene-04","effect_id":"zoom_blur_3d","time_range_s":[42.0,44.0],"decision":"skipped","reason_code":"GPU_EFFECT_UNSUPPORTED","reason":"No validated CUDA implementation; original clip retained"}
```

Render Queue must show e.g. `Skipped effects: 3`, list the affected scene/effects, provide downloadable/viewable per-job logs and final applied/skipped/failed summary. Surface a visible warning if output was completed with skips. Make skip counts deterministic and testable.

## 5. Integration sequence

1. **Inventory native project semantics.** Map existing `concat.json`/timeline attributes, linked images/videos, frame rate, image transforms, transitions, overlays, subtitles/captions and audio; distinguish essential clip content from optional effects. Create fixtures from real (sanitized) projects. Do not claim complete compatibility until verified.
2. **Build isolated CUDA render backend.** Add an FFmpeg/CUDA pipeline and optional custom CUDA kernels within `cloud_render/concat/gpu/`. Detect GPU/NVENC/CUDA filter readiness using actual frame-processing smoke tests, not just NVIDIA device enumeration or NVIDIA Vulkan presence. Explicitly prevent CPU visual filter fallback.
3. **Define shared render-plan/effect manifest.** Deterministic translation from editable Concat project to GPU operations. Pin versions across Studio, worker image and source lock; keep old editor and existing worker contract intact until acceptance.
4. **Extend worker API without breaking upload.** Preserve authenticated resumable chunks, SHA-256 checks, idempotent job IDs, durable spool/state and VPS delivery. Add backend selection/capabilities/skip logs. Move from the existing `max_workers=1` to a guarded 2-job executor only after concurrency acceptance; set explicit queue limits and GPU memory/encoder safeguards.
5. **Extend Windows Studio GPU Config.** Expose selected CUDA backend, hardware readiness, image/renderer version, capability status and 2-job setting. Do not mark Vulkan unavailable as a CUDA failure. Credentials remain in Windows Credential Manager; do not store secrets in project JSON or logs.
6. **Preserve render queue semantics.** Retain Start/Pause/Resume/Skip/Retry/Delete, durable SQLite job states and no double paid submission. Queue pause does not cancel an active GPU render. Cancellation may detach an already submitted remote job; show truthful state. Include engine/version, per-job progress, effect skip count and log access.
7. **Real-project acceptance.** Desktop authoring -> verified bundled media -> Salad GPU -> video/audio output -> verified VPS receiver. First 30 seconds; then 5 minutes; then 2 distinct five-minute projects in parallel. Verify frames/visual transitions/captions, timeline, durations, audio sync, resolution, codec, output checksums, logs and cleanup.
8. **Release gates.** Build Windows portable ZIP and SHA-pinned Linux image from coherent contract/source revision; CI tests, GPU runtime acceptance and rollback check. No change to production services unless health checks and guards pass.

## 6. Verified benchmark evidence (Salad shell; NOT Concat end-to-end acceptance)

| Test | Result | Notes |
|---|---|---|
| CPU `zoompan` + NVENC | ~28 FPS | CPU-side transform bottleneck; different workload |
| 60-sec CUDA fixed Zoom + animated Pan, one job | 60 sec in 19.49 sec; 92.3 combined FPS | H.264 NVENC, synthetic source |
| 60-sec same CUDA test, two parallel jobs | Two 60-sec videos in 11.60 sec; 310.3 combined FPS | Warm-up and caching affect comparisons |
| 5-minute heavy CUDA layers + two-source audio mix | 300-sec output in 96.22 sec; 93.5 FPS; PASS | Four visual layers, three moving overlays, fixed zoom/animated pan, 154.5 MB MP4; observed peak 1298 MB VRAM / 40% GPU / 56% NVENC |
| 2 x 5-minute parallel CUDA test | Both 300-sec MP4s PASS in 70.9 sec; 253.82 combined FPS | Two outputs 294.6 MB each, two audio tracks reported each; workload need not equal single test |

**Benchmark caution:** Synthetic test passes prove these CUDA paths run on that Salad RTX 3060 Ti, not smooth zoom, karaoke, native Concat project parity, all effects, or end-to-end worker integration. Do not promise fixed speedup from small samples or benchmark different workloads as equal. After-render GPU readings are not peak measurements.

## 7. Minimum automated and manual acceptance gates

- Static filter-graph assertion: **zero CPU-side video processing / subtitle rendering fallbacks** in GPU rendering mode.
- Test a supported GPU effect, unsupported optional effect, unknown effect, and unsafe-to-skip required effect; check exact warning/error schema, counts, timestamps and unchanged clip duration.
- Compare expected vs rendered first/middle/last frames; verify overlay motion, real **smooth** zoom when implemented, transitions, caption word timing and no missing source clips.
- Validate FPS, duration tolerance, video codec, dimensions, audio presence/synchronization and no A/V drift after 5-minute render.
- Force CUDA device loss, NVENC unavailability and incomplete bundle: reject safely, preserve data, never switch to CPU.
- Run 1 then 2 parallel independent projects; verify both outputs and fair queue accounting; benchmark warm single vs warmed dual under comparable source/effects.
- Check persisted jobs across restarts, idempotent upload, remote IDs, canceled/detached states, progress and logs, verified receiver SHA-256, safe cleanup, optional Drive behavior and rollback readiness.
- Two-target compatibility: desktop edits saved in `concat.json` must render from same versioned effect manifest on Linux. An unsupported optional effect is visible in the report, not silently erased.

## 8. Current status and scope control

**Approved now:** CUDA + NVENC, hard ban on CPU video-filter fallback, GPU-only visual effects target, auto-skip-and-log optional unsupported effects, fail unsafe skips, candidate 2-way parallel rendering, existing queue/delivery/data safeguards.

**NOT claimed complete:** Production CUDA worker, CPU-free all-audio path, smooth animated GPU zoom, GPU captions/karaoke, complete transitions/filters, 2-concurrent worker implementation, real editable-project Salad parity, safe production rollout.

This is a **documentation-only decision record**, not an authorization to bypass deployment gates or to report unfinished GPU features as implemented.

# Cloud Render — Concat original desktop package

Canonical package: copied from Rajudesktop/concat_original to VPS1/cloud_render/concat.

The Windows EXE is from original Concat v0.2.8, not a custom video renderer.
The EXE is intentionally NOT committed into Git. This repo owns a GitHub Actions workflow that fetches the same official upstream installer, validates its SHA-256 against the official SHA256SUMS, checks the Windows PE header and size, and publishes a new Actions artifact.

Source repository: https://github.com/dilshadshahsyed883-cmd/Rajudesktop
Original successful build: https://github.com/dilshadshahsyed883-cmd/Rajudesktop/actions/runs/41718543140
Original artifact ID: 13270812649; ZIP size: 86108691 bytes; expiration date: 2026-12-03.
Upstream: https://github.com/jub0t/Concat/releases/tag/v0.2.8 (AGPL-3.0-or-later).

Windows installer: Concat-0.2.8-windows-x86_64-setup.exe
Target build artifact: Cloud-Render-Concat-v0.2.8-Windows-x64

IMPORTANT: a Windows EXE does not run directly in Linux Salad Cloud GPU containers.
Concat's separate native Rust/Linux headless engine and API must be packaged and tested before cloud rendering is enabled. This migration does not deploy a Cloud Render app or change the VPS services.

Personal Concat projects/videos are NOT present in the Rajudesktop git tree. They must be exported from the user's PC (concat.json with referenced media) and securely transferred separately, never committed to Git.

Safeguards: no production deployment, no user data deletion, no Auto Script changes, no live Drive or Salad credentials in Git. Keep the Rajudesktop source artifact until target verification passes.

Offline test: python -m unittest discover -s cloud_render/concat/tests -v

## Portable project-data handoff

Concat stores a project in concat.json (legacy wolfcut.json), with absolute references to media files.
Offline packaging copies the project and referenced media into a checksum-verified ZIP; restore rewrites only the extracted project's media paths for the Linux worker.

Desktop command (Python 3.11+):
    python -m cloud_render.concat.project_bundle pack SOURCE_PROJECT_DIR OUTPUT_PROJECT.zip
Linux worker command:
    python3 -m cloud_render.concat.project_bundle restore INPUT_PROJECT.zip NEW_WORKER_PROJECT_DIR

The ZIP remains local until an authenticated transport is implemented. Missing media aborts packaging; archive corruption and path traversal abort restore; existing output paths are never overwritten. Custom fonts, plugins and external script dependencies need separate handling. This package does not render or send jobs to Salad.

## Two deliverables — shared feature rules (2026-11-04)

Cloud Render contains two coordinated deliverables:

1. Desktop editor: Windows Concat `.exe` for project authoring.
2. GPU image: Linux Docker container running Concat native `concat-cli` rendering engine on Salad Cloud GPU.

Shared pinned release: `cloud_render/concat/source.lock.json`.
Desktop package workflow: `.github/workflows/vps1-cloud-render-concat-package.yml`.
GPU image recipe: `cloud_render/concat/gpu/Dockerfile`.
Dual-target contract CI: `.github/workflows/vps1-cloud-render-dual-validation.yml`.

**Any change to the shared editing/rendering engine, effects, output settings or project file schema must be implemented on BOTH targets from the SAME source commit.** Both platform builds and end-to-end output parity are required before promoting a release. A compatible desktop project bundle must render on the GPU without semantic loss.

CURRENT LIMITATION: The Windows `.exe` is an untouched, SHA-256-verified upstream release. The Linux image is only an experimental pinned-source build recipe. Original `.exe` packaging does not automatically incorporate custom feature changes. Before changing Concat functionality, migrate the Windows build to the same maintained source fork/commit as the Linux builder and verify both builds and a 30-second MP4 with audio, frames and duration. No custom feature parity, tested Salad acceleration, live Cloud Render UI, Drive integration, or production deployment is claimed yet.

Keep desktop source media and the Rajudesktop package intact. Do not put personal media, OAuth tokens or credentials in Git. Auto Script remains unchanged.


## Candidate HTTP GPU worker (separate from unchanged Windows Concat)

The worker now boots as a long-running, non-root Python standard-library HTTP
service on port 8080. It delegates project editing/rendering to the *same
pinned upstream* native Rust concat-cli v0.2.8 through the official JSON-RPC
project.open and export.run calls. Its bridge is infrastructure-only: it does
not add or change Concat editing effects or the desktop project schema.

The image-publishing workflow is
.github/workflows/vps1-cloud-render-gpu-image.yml. After protected-main CI and
a local container startup/auth smoke, it publishes ONLY a SHA-tagged candidate:
ghcr.io/dilshadshahsyed883-cmd/concat-gpu-worker:candidate-<40-character-commit>.
Publishing a candidate is NOT a successful Salad GPU render, desktop parity
acceptance, or permission to switch production to this image.

Required Salad environment (configure in Salad; never commit secrets):
- RENDERER_API_TOKEN: unique 32+ character secret for worker HTTP requests.
- CLOUD_RENDER_VPS_BASE_URL: https://app.scenebuilder.online (optional while PC testing).
- RENDER_OUTPUT_TOKEN: the rotating Cloud Render receiver token from VPS1 GPU Config
  (required only if the above receiver URL is configured).
- PORT: 8080 (default); CONCAT_RENDER_TIMEOUT: 7200 seconds (default).

Endpoints:
- GET /health/live and GET /health/ready: unauthenticated platform probes;
  readiness checks the CLI executable, not GPU utilization.
- POST /api/jobs/<id>: authenticated, Content-Type application/zip, raw bytes
  of the portable project bundle, X-Content-SHA256 64-character hex digest.
  Returns 202 immediately after integrity-checked spool; project IDs must
  match [A-Za-z0-9_-]{1,80}; queue serializes rendering.
- GET /api/jobs/<id>: authenticated state/error/output metadata.
- GET /api/jobs/<id>/output: authenticated MP4 download while local output
  is retained (e.g., no VPS receiver configured).
- POST /api/jobs/<id>/retry-delivery: authenticated retry only after an
  output exists and the previous VPS delivery failed.

Every non-health endpoint requires Authorization: Bearer <RENDERER_API_TOKEN>.
The Salad gateway's own networking.auth should ALSO remain enabled.
Failed renders and transfers retain their source ZIP and any MP4 locally.
Only after the VPS chunk receiver verifies full SHA256 and sends state=ready
are GPU temp data and output deleted. VPS1 then manages independent
Drive transfer, remote size/MD5 verification and guarded local cleanup;
auto Drive upload remains OFF by default.

Deployment acceptance outstanding: actual Linux image build/push must pass,
Salad GPU 30-second 1080p render with synchronized audio must pass, and the
unchanged Windows editor must be compatibility-checked against that result.
Until then no worker-online or GPU acceleration claims are justified.

### Salad Container Gateway IPv6

Salad Container Gateway delivers incoming requests to container IPv6 addresses.
The worker must bind `[::]:8080` with `IPV6_V6ONLY=0` to retain IPv4
local/CI health and support Salad IPv6 routing. CI smoke tests both `::1`
and `127.0.0.1` before publishing a SHA-pinned candidate. Set Gateway port
`8080`, Salad gateway authentication ON and one active request per replica.
Use the new SHA-tagged image after protected merge; earlier candidate tags
with the IPv4-only listener must NOT be used for Salad Gateway deployment.
The worker's Bearer token is separate from Salad gateway API-key auth.


## Salad Gateway: resumable Windows project upload

Salad Container Gateway request timeout is at most 100000 milliseconds.
Large project ZIP uploads can fail before being accepted even while readiness
passes. For uploads over a few MB, use the new authenticated 8 MiB chunk API:

- POST /api/uploads/{id} with JSON containing size and sha256: begin or resume.
- GET /api/uploads/{id}: list verified uploaded chunk indices.
- PUT /api/uploads/{id}/chunks/{index}: raw application/octet-stream chunk
  and X-Content-SHA256 of that specific chunk. Identical retries are safe.
- POST /api/uploads/{id}/complete: assemble and verify full SHA256, then
  queue a Concat render exactly once.
- GET /api/jobs/{id}: check actual rendering and VPS delivery status.

All endpoints except health require Salad Gateway API key and a separate
Authorization Bearer RENDERER_API_TOKEN. Chunk files survive failed HTTP
requests and restarts on the same container disk; the final ZIP is assembled
only after all checksums match. Source ZIP and PC media are not modified.

Windows PowerShell 5.1+ client, using the ORIGINAL ZIP made by project_bundle.py:

    Invoke-WebRequest "https://raw.githubusercontent.com/dilshadshahsyed883-cmd/vps1-deploy-control/main/cloud_render/concat/client/upload_job.ps1" -OutFile "$env:TEMP\concat-upload.ps1"
    powershell -ExecutionPolicy Bypass -File "$env:TEMP\concat-upload.ps1"

The script prompts for domain, Salad API key and RENDERER_API_TOKEN.
It writes only ZIP SHA256, domain and job ID in a local upload-state.json
file beside the ZIP so repeated runs skip already-acknowledged parts.
It never writes secrets to the state file or uploads the originals to Git.
It does not modify the Windows Concat editor or its project schema.

VPS receiver transfer is independent: CLOUD_RENDER_VPS_BASE_URL and
RENDER_OUTPUT_TOKEN must be configured to match the actual VPS1 receiver.
If delivery is not verified, the worker retains output and source.
Update Salad to the NEW SHA-tagged image after protected-main image
publishing; old workers cannot accept chunk uploads.


## Salad GPU Vulkan rendering — hardware-first diagnosis

An authenticated project upload can enter state=failed with:
  no GPU or software renderer is available to draw with - install Mesa's Vulkan drivers (lavapipe)
when the native wgpu compositor cannot open a Vulkan adapter. Docker's
libvulkan1 is only the Vulkan loader; the Mesa software ICD had been missing.
The updated runtime includes mesa-vulkan-drivers, vulkan-tools and the
existing NVIDIA_DRIVER_CAPABILITIES=compute,utility,video,graphics setting.

NVIDIA host graphics driver libraries and ICDs are furnished by the Salad
container runtime when supported. Do NOT install an arbitrary nvidia-driver
version into the application image: it could conflict with the driver on the
selected consumer GPU host. NVIDIA_VISIBLE_DEVICES and
NVIDIA_DRIVER_CAPABILITIES alone do not prove that Salad mounted the NVIDIA
Vulkan ICD. CUDA/NVML access alone does not prove Vulkan availability.

The worker now exposes an authenticated read-only endpoint:
    GET /api/diagnostics/gpu
Salad Gateway key and Authorization: Bearer RENDERER_API_TOKEN are BOTH
required. The response includes vulkan_adapters, nvidia_vulkan_visible,
nvidia_compute_visible, software_vulkan_visible, and renderer_hint.
This is an adapter enumeration, not a successful frame export.
- nvidia_vulkan_visible=true: try a real render, still verify GPU use/output.
- software_vulkan_visible=true AND nvidia_vulkan_visible=false:
  only Mesa Lavapipe CPU rendering is detected; this is NOT GPU acceleration.
- no adapters: host GPU driver/ICD exposure remains unavailable; check Salad
  GPU instance and graphics capabilities, not the Windows app.

For user-facing Windows work, client/upload_job.ps1 preflights this endpoint
before sending 107 MB or starting an expensive render. By default it
REFUSES to submit to an instance with no detected NVIDIA Vulkan adapter.
AllowSoftwareVulkan can be passed ONLY for an explicit slow fallback test.
When no hardware adapter is available, stop the paid Salad container rather
than repeatedly retrying full 1080p jobs.

CI checks Mesa Vulkan ICD enumeration as the nonroot runtime user and a
tiny two-frame decoder->wgpu Vulkan->MP4 encoder render on an ordinary
non-GPU GitHub runner. It does not certify the remote NVIDIA host path.
After protected CI and GHCR candidate publishing, update the Salad image
to the NEW SHA-tagged candidate and run /api/diagnostics/gpu, then a
30-second 1080p export plus VPS verified output transfer. A job that
previously failed needs a NEW job ID on the refreshed container; keep
original Concat-Test-GPU.zip and media unchanged.

## Approved CUDA + NVENC GPU-only plan (2026-11-04)

See [CUDA_GPU_ONLY_RENDER_PLAN.md](CUDA_GPU_ONLY_RENDER_PLAN.md) for the approved video-frame GPU-only architecture, unsupported optional-effect auto-skip with structured logs, no CPU visual fallback, 2-job parallel candidate, and validation/release gates. This is an approved plan only; the CUDA worker, full effect implementations and real Concat project acceptance are still pending.

## CUDA/NVENC candidate — opt-in first real-project path (2026-11-04)

Implementation PR: [CUDA worker candidate #703](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/703).
Approved policy: [CUDA_GPU_ONLY_RENDER_PLAN.md](CUDA_GPU_ONLY_RENDER_PLAN.md).

**Status: development candidate; NOT Salad hardware-accepted, NOT production.** Keep the current production image untouched until actual GPU acceptance, output parity and recovery checks pass.

The same HTTP worker and Windows desktop gateway now support an explicitly selected `CONCAT_RENDER_BACKEND=cuda_nvenc` in the *candidate image*. Existing default remains `native`, with no automatic CPU/GPU fallback. Docker installs Ubuntu FFmpeg separately from the native Concat CLI's pinned libav dependencies, and subprocesses isolate the respective library paths.

First-project supported subset:

- Native `concat.json` (version 1), active timeline, sequential, gap-free image clips, 1080p/30 recommended, fixed scale between 1.0 and 1.8 and at most two **linear** position-keyframe endpoints. Per-frame compositing, scaling, pan and H.264 encoding use CUDA filters + NVENC.
- Up to one unmodified AAC audio clip, starting at zero and spanning the visual timeline. Audio is **stream-copied** without CPU re-encoding. Project with other/multiple audio treatment fails rather than silently losing audio.
- Images are decoded and YUV-normalized **once on CPU during ingestion** before hardware frame processing; this disclosed limitation does *not* yet satisfy an absolute CPU-free imagery pipeline. All **render-time per-frame visual filters** are CUDA-only. Additional GPU-native still image intake must be accepted before claiming stricter 100% GPU content preparation.
- Unsupported optional color effects, fixed rotations, and safe nonoverlapping transitions: skip with structured `GPU_EFFECT_UNSUPPORTED` logs and unchanged clip timing. Essential unsupported text, shape, video clips, multi-layer composition, unsafe timeline overlaps, incompatible aspect and missing inputs: fail with explicit reason; never silently render as CPU.
- Worker status includes `backend`, `skipped_effects` and `event_log`; authenticated `GET /api/jobs/{id}/logs` provides structured events. Logs persist after acknowledged VPS delivery.
- GPU readiness uses selected backend: CUDA/NVENC checks `nvidia-smi`, FFmpeg filter availability and `h264_nvenc`; it no longer depends on NVIDIA Vulkan. A successful readiness report does **not** replace a successful hardware render.
- First acceptance is **one** real 30-second project. Do not enable 2-job parallelism (current pool is still `max_workers=1`) until first output's video and audio verification succeeds.

**Operator acceptance sequence (candidate only):** build candidate image from reviewed GitHub PR, configure a *test* Salad container with `CONCAT_RENDER_BACKEND=cuda_nvenc` and existing auth secrets, verify `/health/ready` and authenticated `/api/diagnostics/gpu`, use the Windows Studio Render Queue to submit a supported real `concat.json`, then confirm `/api/jobs/{id}` and logs, MP4 audio/video streams, exact duration, visual motion, and verified VPS receiver. Keep original media and production untouched. Only after this may more editing features and 2-parallel GPU workers be enabled.

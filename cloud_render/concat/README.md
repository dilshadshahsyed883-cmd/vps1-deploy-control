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

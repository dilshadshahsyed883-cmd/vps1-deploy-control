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

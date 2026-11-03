# Cloud Render Concat migration record — 2026-11-04

Source: Rajudesktop/concat_original; upstream Concat v0.2.8.
Original GitHub Actions run 41718543140 succeeded; artifact Concat-v0.2.8-Windows-x64 (ID 13270812649, 86108691-byte ZIP, expires 2026-12-03).

Target: VPS1 cloud_render/concat — the packaging verifier, tests, README and a new target-owned Windows installer Actions workflow.

This is a copy-first migration: Rajudesktop original package remains intact until target-built installer SHA-256 and action results are verified. Existing binary is in Actions artifact storage, not Git.
User PC Concat projects (concat.json plus media) are NOT stored in Rajudesktop, so there are no actual PC files to transfer from that repository.
Windows EXE is not a Linux cloud executable; native Linux Concat headless rendering, API bridge, Salad GPU validation, Experiment Studio Cloud Render tab, tenant storage and Google Drive upload are separate steps.
No production deployment, no service restart, no Auto Script modification and no removal of original Google Drive files.

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

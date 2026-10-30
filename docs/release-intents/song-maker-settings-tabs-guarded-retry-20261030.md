# VPS_1 Song Maker: guarded settings tabs production release

Date: 2026-10-30

## Scope

Release the already-merged, reviewed Song Maker UI changes in PR #452 (Export Settings and Video Settings sub-tabs). This file is the release intent only; it changes **no application code**.

## Exact release selection

- App: `song-maker` only
- Existing protected-main workflow: `vps1-song-maker-release.yml`
- Merge-commit message selectors: `[vps1-song-maker-update-release]` and `[vps1-song-maker-only]`
- Auto Script: **OFFLINE**

## Guardrails

- Do not restart, cancel, delete, reorder, or modify active/queued Song Maker projects.
- Honor installer `require_idle_jobs` before switching immutable release pointers.
- Keep database, Drive credentials/settings, workers, source input files, and neighboring Studio apps intact.
- Use the existing host-wide serialized deployment lock and automatic rollback, authenticated HTTPS/health verification, and immutable SHA release.
- If the queue is busy, fail closed; leave running production unchanged. Do not bypass the idle fence.
- A GitHub successful merge is not proof of production installation; only release workflow PASS and live UI/health evidence prove deployment.

## Expected UI

Settings contains two sub-tabs: **Export Settings** (resolution, image fit, quality, Drive controls) and **Video Settings** (music video overlays, style, intensity, pulse speed). Saved preferences persist and existing API/queue behavior is preserved.

# VPS_1 Song Maker — independent app-scoped production release intent

- VPS_1 / Agent 1. Source-only PR #405, release selector #400 and quota recovery #401/#402 merged. Shared route/nav #399 merged without automatic deployment.
- Baseline `2b470afc566adac04d81d45af9cb4da6a5eb6726`; protected CI required, release only after Audio Downloader scoped deploy has verifiable success and no active host deployment.
- Single guarded selector: `[vps1-song-studio-release] [vps1-song-maker-only]`. No combined Song Studio release, no Auto Script or unrelated app restart.
- Exact approved `song-maker` installer via Tailscale; host-wide FIFO; idle admission; immutable revision, backups, health/authenticated route verification and automatic rollback on failure.
- Preserve all SQL job state, existing queues, archive receipts, local outputs and Drive OAuth credentials. Never force if provider quota or idle gate blocks.
- Required acceptance: authenticated `/song-maker/`, SSO isolation, 1440px Kokoro UI, independent queue, filename stem matching, validated audio-length MP4, input /used archive BEFORE output upload, persisted retry after 180 seconds, resumable upload, actual Google Drive MP4 presence and checksum. Provider quota may block E2E; record that as not verified.
- Only after workflow confirmed success may coordinator begin Song Uploader rollout.

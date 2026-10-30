# VPS_1 Song Uploader — final app-scoped guarded release

- VPS_1 / Agent 1; protected main `42bd85529e1b132d80cd74ed0256251516944dfd` contains current #399 Studio code, #404 Kokoro UI, #405 Song Maker and #406 Audio Downloader.
- Song Maker guarded release succeeded on 2026-10-30 via workflow #41000896186. Audio Downloader guarded Voice deployment refused due active/pending audio queue; never bypass that queue.
- This PR only authorizes the existing selector `[vps1-song-studio-release] [vps1-song-uploader-only]` to deploy `local-upload-manager` through Tailscale; NO combined Song Studio, Scene Builder, Voice, Short Maker or Song Maker release.
- Require host-wide deployment FIFO, Local Upload Manager/Song uploader idle admission, immutable source, SQLite backup, scheduler/worker health, authenticated Song engine web route checks and rollback on failure.
- Preserve OAuth tokens, real channels, upload schedules, existing files and jobs. Do not trigger any user publishing.
- Verify actual production Song engine health, thumbnail-required validation, scheduler isolation and no regression to original Auto Uploader. Canonical `/song-uploader/` requires independent shared Scene navigation release; do not assert it live until authenticated HTTPS proof.
- No claim of provider uploads or production success until workflow and service evidence are available.

# VPS_1 — Song Maker Quality/ETA app-scoped release

Date: 2026-10-30. Owner-authorized Tab 2 completion.

## Content
- The Quality Settings + ETA implementation already merged via PR #405 and passed protected CI.
- Previous production Song Maker release at commit `88ca3a3a0611944230d93a9b988df124b4a57ad5` completed, but the PR #405 release was **skipped**; this PR requests the guarded production update using the latest main snapshot.
- Preserve PR #403 Kokoro-width UI, PR #402 durable 180s Drive cooldown, owner-scoped Drive Sync, pre-upload verified `used/` archive, local output retention, and historical presets.

## Deployment gate (Song Maker only)
- Merge on protected main with **both** `[vps1-song-studio-release]` and `[vps1-song-maker-only]` markers; never invoke a broad Studio deployment.
- Existing production Tailscale OIDC, SSH fingerprint, same-host serialization, immutable revision, idle admission, database backup and scoped rollback MUST pass; reject busy queues, drift and missing helper capability.
- The release workflow success proves code deployment and health only. Real Drive end-to-end remains **PENDING** until production confirms an authorized isolated Google Drive image/audio input pair, validated full-duration MP4, post-validation source archival to `Song Studio/Audio downloader/used`, upload to `Song Studio/song maker`, independently verified remote ID/size/hash, and a persisted completed receipt. Fake tests/CI do not satisfy this gate.
- Do not touch Audio Downloader, Song Uploader, Scene app queues, existing Drive credentials, live data, or Auto Script.

## Release evidence to record
- GitHub Actions workflow run ID, installed source SHA, app-scoped PASS logs, service/route health, and a separate real Drive MP4 receipt (or an explicit reason acceptance remains blocked).

# VPS_1 Agent 1 — Audio Downloader retry policy guarded release

Release candidate: merged PR #413 (fix only confirmed Drive API rate limits, stop all other retries).
Environment: VPS_1 only; use existing protected Tailscale release. Do not touch Song Maker/Uploader, Auto Script, credentials, or other app jobs.

## Behavior
- Explicit Drive API rate limit or HTTP 429: durable upload-only 180-second retry from preserved local file.
- Any other error (including unexpected anonymous content type, public permission or content mismatch, auth, network, storage, conflict): immediately mark failed with no future automatic retry; preserve MP3/thumbnail and manual Retry.
- Existing non-rate-limit retry loops migrate to Failed on first safe worker startup; no media removed.
- Verified successes keep 60-second pacing and immutable Video ID destinations.

## Safety gate
Use the existing audio-thumbnail-only deploy workflow; it checks host serialized lock, SQLite BEGIN IMMEDIATE queue idle fence (no queued/downloading/downloaded/uploading), backup, immutable SHA overlay, health checks, and rollback. Do **not** bypass or edit this gate when queue is busy. A refused release is NOT a success. Do not start extra uploads or mutate production SQLite via SSH. Follow up with actual deploy run receipt and real Drive acceptance; CI alone is not evidence of live behavior.

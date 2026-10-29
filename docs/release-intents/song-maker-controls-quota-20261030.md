# Song Maker controls and Drive request budget — 30 October 2026

VPS_1 / Agent 1. Existing production and other applications remain independently owned.

- Replace Output tab with Settings; show preview/download/Drive links in Queue.
- Persist resolution/fit and scan interval; apply saved export defaults to new manual projects.
- Show actual FFmpeg FPS, speed, frame count, processed seconds and elapsed time.
- Owner-scoped durable pause/resume prevents new lane claims; active work finishes. Selected retry/cancel/delete use existing ownership and worker guards.
- Shared image/audio input scans use one listing instead of two. Default automatic interval is 300 seconds (manual Sync Now remains available); saved 60-second settings remain supported.
- Retain concurrent PR #389 quota optimization (exact output stat, shared archive snapshots) and add literal filename filtering for input download verification. Keep remote ID/size/MD5 checks.
- Drive profile/sync remains administrator-scoped because existing server credentials must not expose another owner's files. Disabled Sync now explains the role requirement; actual reported owner-role fault still needs live diagnosis.

Validation: 31 focused real-media/API/release tests and 20 Song Uploader/navigation regressions PASS locally. Chromium PASS at 360/390/768/1024/1440 with real render/download. Real Google Drive transfer and authenticated production verification remain separate gates; no live E2E claim.

Release via existing Song Maker-only idle fence, SQLite backup, immutable pointer and rollback. Do not force queued/active work, reset state or change OAuth/scheduling. Existing Auto Script runtime is untouched (current owner-confirmed ONLINE per Master); static historical OFFLINE labels are not measurements.

# Auto Stream V1 — Independent streaming engine

Source: apps/short-stream/part01/auto_stream_engine.py

This is the first backend engine milestone. The existing Short Stream app
(`/short-stream/`) keeps all legacy sessions, destinations and UI untouched.
The new endpoints are added at (`/short-stream/engine/*` after the existing
Nginx gateway mapping). They are protected by the existing Scene session SSO
proxy; do not directly expose the loopback port. Auto Uploader databases,
tokens, queues and upload modes are NEVER read or written here.

## Current implementation

- Independent SQLite WAL job store under SHORT_STREAM_DATA/auto-stream-engine.
- Durable DRAFT → READY/SCHEDULED → STARTING → INGESTING → STOPPED/FAILED
  workflow; CANCELLED and INTERRUPTED states are fail-closed.
- Per-channel unique active-session lease and deterministic idempotency key.
- One worker per job, FFmpeg 720p H.264/AAC RTMPS ingest to YouTube.
- Read-only visibility at GET /engine/health and GET /engine/jobs.
- Separate read-only Short Stream media catalog path validation, not uploader
  media. No symlinks and no absolute/traversal paths.
- Stream credentials resolved from channel-specific 0600 files in
  AUTO_STREAM_ENGINE_DATA/secrets/<channel_id>.key at launch, never from web
  request payload, database, Google Sheets, or app logs.
- Existing Short Stream sessions are intentionally unaffected.
- On restart, previously active jobs become INTERRUPTED, never falsely LIVE.

## Key controls

Publishing is OFF unless AUTO_STREAM_PUBLISH_ENABLED=1. Scheduled execution
is separately OFF unless AUTO_STREAM_SCHEDULER_ENABLED=1; tick() does
nothing unless BOTH controls are enabled. There is deliberately **no**
auto-scheduler service installed in V1 and no automatic publishing on
application startup. New jobs require explicit POST /jobs/<id>/prepare;
manual streaming requires POST /jobs/<id>/start and explicit ops provisioning
of channel stream keys. STOP is a separate authenticated API request.

INGESTING means an encoder process has started, **not** that YouTube reports
a real LIVE broadcast. youtube_live_verified remains false pending OAuth
Live Streaming API integration. No live stream is claimed without remote
verification. The first milestone intentionally does not include OAuth,
channel connection UI or Google Sheets Sync, which are next milestones.

## Route contract

- GET /engine/health: engine and feature-gate status.
- GET /engine/jobs: metadata-only; no keys.
- POST /engine/jobs: idempotent DRAFT. Body requires channel_id (YouTube UC
  ID), media_path (relative to own media), title, idempotency_key; optional
  timezone-aware scheduled_at and continuous boolean.
- GET /engine/jobs/{job_id}: current job metadata.
- POST /engine/jobs/{job_id}/prepare: validate and arm a READY/SCHEDULED job.
- POST /engine/jobs/{job_id}/start: require explicit publishing gate and
  private channel key; prevents two simultaneous jobs for the same channel.
- POST /engine/jobs/{job_id}/stop: cancel prepared job or stop owned FFmpeg.

## Follow-on work

OAuth 2.0 (channel identity, offline refresh and Live API), Sheets Sync and
channel registry, durable independent scheduler process, accurate YouTube
broadcast transition/verification, and web UI. Add runtime health and real
opt-in private live end-to-end verification before enabling a production
publishing gate. Roll out through scoped GitHub–Tailscale deployment only
after CI and guarded change review. Auto Script remains OFFLINE.

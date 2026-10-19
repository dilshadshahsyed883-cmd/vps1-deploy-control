# Auto Stream V3 — YouTube Live API controller (gated)

## Implemented
- Independent channel OAuth verification at operation time (YouTube channels.mine), not mere local flags.
- Explicit provision: create **private** YouTube broadcast with auto-start and auto-stop OFF, create 720p/30 RTMPS stream, encrypt ingest credentials, bind and confirm.
- Provisioning reservation is durable and fail-closed. A partial/timeout operation is flagged NEEDS_RECONCILIATION, never automatically retried/recreated.
- Read-only remote broadcast/ingest state verification with the actual Google YouTube Live API.
- Explicit ingest start via existing V1 FFmpeg worker, with encrypted per-job YouTube stream key, no browser key storage.
- Explicit transitions `testing`, `live`, `complete` only after YouTube ingest `active` (for testing/live), with separate opt-in gates.
- Distinct control states: local encoder `INGESTING` does **not** mean public YouTube Live.

## Safety and environment
By default all mutation/control switches remain OFF.
- AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED=1: allow explicit private broadcast provisioning and ingest API operations.
- AUTO_STREAM_PUBLISH_ENABLED=1: permit existing FFmpeg worker ingest.
- AUTO_STREAM_LIVE_TRANSITIONS_ENABLED=1: permit explicit transition actions.
- AUTO_STREAM_GO_LIVE_ENABLED=1: additionally allow public live transitions.
All require valid OAuth authorization granted by the user and owned channel. Do not activate in production without owner approval and a successful private end-to-end test.
YouTube `RTMPS` credentials are Fernet-encrypted in Auto Stream's own 0600 DB. No uploader credentials are read or changed. No background publication/scheduler is enabled.

## APIs under existing Scene SSO gateway
- GET /short-stream/engine/youtube/health — flags only.
- GET /short-stream/engine/jobs/{job_id}/broadcast — safe metadata.
- POST /short-stream/engine/jobs/{job_id}/broadcast/provision — explicit private create, stream insert, bind.
- GET /short-stream/engine/jobs/{job_id}/broadcast/verify — live YouTube readback.
- POST /short-stream/engine/jobs/{job_id}/broadcast/ingest — guarded worker ingest.
- POST /short-stream/engine/jobs/{job_id}/broadcast/transition body {"status":"testing"|"live"|"complete"} — explicit gate & Google state validation.

## Limits / pending
A Google Cloud OAuth web-client is still required; it must be supplied securely on the VPS, along with a stable Fernet key. OAuth consent and channel authorization are owner-only actions. No Google account or real channel was contacted during CI. A live stream is not claimed until YouTube API reports status live. A partial Youtube provisioning requires operator reconciliation.

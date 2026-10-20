# Voice Studio — additive Kokoro/Edge output metadata contract

Tracking: VS-ISS-20261020-01 and VS-ISS-20261020-03. This is the queue workstream's read-only metadata contract, not authorization to change queue state transitions.

## Submission

`POST /tts/api/{kokoro|edge}/generate`: optional JSON string `output_name` (max 120 Unicode characters). The existing JSON settings and response remain accepted/unchanged.

`POST /tts/api/{kokoro|edge}/batch`: optional multipart field `output_name` applied to all files; when present it is suffixed with `_2`, `_3`, etc. for later items. Existing `files`, `batch_name`, engine parameters and responses remain supported.

Naming precedence for **new** jobs: sanitized optional `output_name`; otherwise original TXT filename stem; otherwise first two meaningful Unicode letters of supplied narration text plus `_<job-id>`. No extension is accepted inside stored `output_basename`; generated artifact extension always comes from the format emitted by its engine. Kokoro WAV remains WAV; Edge MP3/WAV/SRT/TXT remain distinct. Each job occupies an isolated `data/jobs/<job_id>/` directory; Kokoro Drive paths also include `job_id` to prevent cross-job overwrites.

Existing job identifiers, original names, queued text, generated audio, historical rows, schema columns and download routes are **not rewritten**.

## Jobs API: new optional additive fields

Both `GET /tts/api/{kokoro|edge}/jobs` return existing fields unchanged, plus:

| Field | Type | Semantics |
| --- | --- | --- |
| `naming_method` | string | `custom`, `source_txt`, `text_fallback`, or `legacy` for pre-migration jobs |
| `output_filename` | string | audio name (Kokoro `.wav`, Edge primary `.mp3`), independent of any Drive state |
| `drive_file_id` | string or null | primary WAV/MP3 Google Drive ID, only persisted after remote receipt verification |
| `drive_url` | string or null | primary Google Drive `https://drive.google.com/file/d/<id>/view` link derived from a verified ID; never assumes public sharing |
| `drive_verified_at` | UTC ISO8601 string or null | timestamp of verified primary+required ancillary file receipts |
| `drive_files` | object | per-extension `{ext: {id, url, name, size}}` remote receipts, empty prior to verification or for legacy rows lacking receipts |
| `delivery_retries` | integer | existing retry count; legacy `delivery_status`, `delivery_error`, remote pathname fields stay available |

New persistent SQLite columns: `naming_method`, `drive_file_id`, `drive_url`, `drive_verified_at`, `drive_files_json`. Idempotent migration adds missing columns only. The queue workstream **must not create duplicate columns or rename existing statuses**.

**Verified UI link rule:** show Drive link only if `delivery_status` is `delivered` or `cleaned`, `drive_verified_at`, an ID and the matching URL are all present. Legacy remote path text is not proof of an accessible file.

## Delivery-only retry endpoint

`POST /tts/api/{kokoro|edge}/jobs/{job_id}/delivery/retry` returns `{job_id, delivery_status:'retry_pending'}` when the job completed synthesis but its upload is unverified and local audio+TXT are available. It resets Drive retry delay but **does not enqueue audio synthesis, rewrite the output or touch queue processing controls**. A verified/cleaned job responds 409. The Drive spool is recoverable after restart and uploads via the existing rclone configuration without changing permissions.

## Coordination / release guard

This branch overlaps Voice Studio template and worker files owned concurrently by queue and UI workstreams. Keep PR draft until coordinated merge/rebase; do not deploy it alone or use the HTML-only/queue-only release overlay, because those intentionally omit worker changes. A later release must snapshot SQLite/WAL, preflight active synthesis jobs, install the exact immutable merged SHA, preserve worker data/models, verify signed-in Kokoro+Edge downloads/Drive status, and rollback release pointers/services on failure. No real paid TTS test workload without authorization; mock Drive integration in CI.

Status of contract: source candidate awaiting coordinated release; **not proof of a successful production Drive upload**.

# Self Media Library — VPS Local Media

SML stores reusable video clips and images on the VPS under `SML_DATA_ROOT`.
The existing SQLite catalog, keyword editor, AI workspace, collections, website
shell and authenticated Scene Builder Editor API are retained.

Upload videos/images or import authorized YouTube media. Process videos with the
existing FFmpeg engine. In **Process → Pair review**, compare each consecutive
pair in two playable previews and choose **Keep One / Discard Other**. Decide
independently whether to keep the odd last clip. Decisions persist across restart.
Both candidates and the source remain until every decision is complete.

After local SHA-256 and full decode verification, kept clips become approved;
rejected files and the source are removed. Incomplete/failed jobs, pending AI
work, active reservations and committed editor references block removal. Deferred
cleanup retries after jobs finish; the UI also offers a verification retry.
Legacy media remains intact and is never automatically selected or purged.

Library has separate Video Clips and Images sections, date range, filename,
description and keyword search, orientation/status filters, previews, metadata,
saved search collections and bulk deletion. Dashboard and Settings show library
size, VPS usage/free space, counts and pending cleanup.

The owner adds and deletes media directly in Library & Preview. No staged
replacement workflow or prerequisite new library is required. Active processing
and required editor references remain protected during deletion.

Google Drive upload, retry, remote restoration and remote deletion are retired
for SML only. Historical Drive columns/receipts and existing credentials remain
untouched for provenance. No other application's Drive policy changes.

Editor routes/authentication remain `/api/v1/media/search`, `/reservations`,
`/reservations/{id}/commit`, `/release`, and `/video/{id}/content`, with the
existing `X-SML-Editor-Token`. Scene Builder matching, Media Audit selection,
rendering and Pexels fallback use their existing connector. Unavailable media
returns a no-match/unavailable response, never fabricated availability.

Standalone: `./install.sh`, then `./start.sh` (port 8090). Production uses the
existing protected GitHub → Tailscale SML-only immutable release transaction,
SQLite backup, idle checks, functional verification and source rollback.

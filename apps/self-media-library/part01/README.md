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

## Already processed clips (direct save)

Input → Processed Clips accepts local MP4/MOV/MKV/WEBM/M4V/AVI files through
`POST /api/clips/processed/upload` (`multipart/form-data`, field `file`).
Each valid clip is stored unchanged with a generated JPEG preview, an SHA-256,
and an `approved`/`kept` catalog record. Processing, clip splitting, AI and
pair review are skipped. Identical active clips return `duplicate: true` without
changing their current review state. A synthetic completed source preserves
filename/orientation provenance without storing an extra source copy. Approved
clips are available through the existing authenticated Scene Builder V2 API.

## People & Face Recognition (opt-in, approval only)

In Library & Preview → Images, open an image and choose Identify person.
A face selector appears if multiple people are present. Enter the name,
save the reference and begin a CPU-only scan. People & Face Recognition displays
pending matches with similarity scores. Select matching images and press
Approve selected before SML adds the name to keyword tags. Reject and Undo
are also available. Approved tags work with existing keyword search.

The detector and recognizer are OpenCV Zoo YuNet and SFace. SHA-256-pinned
ONNX weights download only on first explicit scan/identification into
SML_DATA_ROOT/face_models, not Git. Download failure is recoverable.
Embeddings and approval history are kept in local SQLite under SML_DATA_ROOT.
No user media, face vectors, names, or secret credentials belong in GitHub.

Similarity scores are not identity probabilities, and all suggestions require
owner approval. Existing person-name keywords are preserved on undo.
No image files are renamed, moved, uploaded or deleted.
Restart an interrupted or failed scan from the People panel; image face
embeddings are cached by image SHA and model version.

Image-only v1: video frame face indexing and automatic new-upload review are
not enabled yet. Users supply their own reference images and names.

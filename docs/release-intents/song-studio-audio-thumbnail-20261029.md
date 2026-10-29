# VPS_1 Song Studio / Audio Downloader — guarded release intent

Date: 2026-10-29
VPS: VPS_1
Agent: Agent 1

## Approved scope
Promote the merged Audio Downloader source from PRs #362, #363 and #365 using
the existing app-scoped **thumbnail-only** production installer. Audio files are
named by YouTube Video ID, and (when explicitly checked) a matching JPEG
thumbnail is delivered alongside the audio. New Drive media is written directly
under Audio Downloader without project or batch subfolders.

The existing installer requires an idle Audio queue, saves rollback metadata,
preserves the other Voice engines/workers, checks authenticated API behavior and
refuses collisions/partial remote receipts. It fails closed for locked queues,
authentication failures, proxy problems, or release drift.

## Trigger
Only after protected-main CI is green, merge the release intent with BOTH:
`[vps1-audio-thumbnail-release]` and `[vps1-audio-thumbnail-only]`
in the final merge commit title. The production environment and Tailscale SSH
identity checks remain mandatory.

## Preserved
- Existing Song Maker state and independent installer
- Original Auto Uploader and Song Uploader engine/queues
- Scene Builder, Auto Script runtime configuration and scheduling
- TTS output state, queues, workers and OAuth credentials
- Existing Drive files and verified receipts

## Required production receipt
Successful workflow output `PASS_AUDIO_THUMBNAIL_ONLY` plus auth/health,
queue preservation, and real Google Drive validation. A CI pass or merge does
not itself certify a production deployment. No source-only test claims an actual
new YouTube or Drive upload.

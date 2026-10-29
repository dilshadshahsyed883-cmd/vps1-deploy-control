# Audio Downloader — Song Studio default Drive folder release

Protected source merge: PR #376 (commit `c731c5fb12fe5b9eec39d156af25402251ada858`).

## Scope
- Apply the **existing** `vps1-audio-thumbnail-release.yml` production overlay to Audio Downloader only.
- Destination: `gdrive:Song Studio/Audio downloader/<VideoID>.mp3` (or WAV), optional `<VideoID>.jpg` alongside.
- Preserve existing Drive files and earlier jobs/receipts. Never move prior files or change historical `drive_path`.
- Preserve all TTS workers, other Voice routes, installed proxy selection, OAuth, Song Maker and Uploader queues, and Auto Script (owner reports its running state as ONLINE).
- Require idle-fenced queue, bounded verifier, backups, authenticated HTTPS/status checks, automatic scoped rollback.

## Release authorization
Once required protected CI finishes, squash merge using both existing markers:
`[vps1-audio-thumbnail-release] [vps1-audio-thumbnail-only]`

Production success must be based on a completed GitHub Actions workflow showing `PASS_AUDIO_THUMBNAIL_ONLY`. This document is not a proof of live delivery.

## Song Maker follow-up
Song Maker's shared input and output defaults are merged, but the installed Song Maker updater currently blocks existing-instance updates until idle-release admission exists. Do not work around the safety guard with the generic installer.

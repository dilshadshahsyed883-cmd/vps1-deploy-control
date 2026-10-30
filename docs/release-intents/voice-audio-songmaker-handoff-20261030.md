# Audio Downloader → Song Maker local intake — guarded Voice Studio app-only rollout

2026-10-30, VPS_1, Auto Script OFFLINE.

## Source / dependencies
- PR #433 merged: Audio Downloader Auto-add to Song Maker Queue defaults OFF, automatically disables Drive auto-upload and enables thumbnail, streams original local audio + thumbnail to Song Maker, retries rejected handoff without re-download.
- PR #440 merged, Song Maker production guarded release PASS: Song Maker accepts only opted-in owner's Audio Downloader handoff, and can route output to local Song Uploader instead of Drive.
- PR #442 merged, Song Uploader production guarded release PASS: same-owner local output import and existing channel Sheet Video-ID matching, scheduled queue, thumbnail validation and duplicate prevention.

## Target
Deploy **voice-studio only** via existing app-scoped, serialized, protected-main Tailscale installer. This release includes both Audio Downloader API and UI under Voice Studio; do not use a template-only overlay. Existing Song Maker/Song Uploader data and workers must not be restarted.

## Guards
1. No real YouTube publishing, and no unrequested Drive uploads.
2. User-selected Audio Downloader auto-queue ON implies Drive auto-upload OFF and download-thumbnail ON; default OFF.
3. Song Maker input setting must also be ON to authorize handoff for the same SSO owner. Admission failure retains downloaded originals and retries safely.
4. Existing independent Drive upload option remains unchanged when auto-queue OFF.
5. Guard active queues, backup persistent SQLite, health-check and auto-rollback on failure. No credential or OAuth mutation.
6. Keep Auto Script OFFLINE. Owner UAT after deployed is allowed, not a deployment dependency.

Source changes were already tested in PR #433 and PR #440; this is a deployment-only PR.

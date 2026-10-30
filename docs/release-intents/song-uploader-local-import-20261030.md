# Song Uploader local media import — app-scoped release intent (2026-10-30)

Dependencies:
- PR #433 merged: Audio Downloader local audio+thumbnail -> Song Maker queue.
- PR #440 merged with 19/19 protected checks: Song Maker opt-in preferences and local MP4/JPEG catalog.
- PR #442 merged with 17/17 protected checks: Song Uploader per-channel local import and zero-Drive media mode.
- Song Maker guarded app-only production install must finish successfully before this release.

Release target: **local-upload-manager only** (isolated Song Uploader through the existing shared LUM source deployment). Keep Scene Builder, Voice Studio, Song Maker and other app workers untouched during this step.

Guard requirements:
1. Protected main SHA; serialized Tailscale-only app-scoped installer with existing health, backup and rollback guards.
2. Preserve live uploader mode databases, OAuth, Google Sheet profiles, historical queue and completed records.
3. Verify canonical /song-uploader session/CSRF, local-source POST permission, profile matching, required thumbnail, worker/retry state and production health without publishing a YouTube video.
4. Local sources may not invoke Google Drive upload, listing, download, move or delete; Drive mode remains available when local source is not opted in.
5. Existing Auto Script must remain OFFLINE.
6. Abort/rollback if any dependency, idle admission, channel mapping, startup or health gate fails. A release attempt is not proof of live acceptance.

After a verified release, the owner may import one completed Song Maker MP4+thumbnail to a bound Song Uploader channel profile and use the existing Sheet schedule/queue. Import does not start YouTube publishing.

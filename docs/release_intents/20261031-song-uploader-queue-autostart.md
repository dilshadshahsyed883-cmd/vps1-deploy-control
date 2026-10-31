# Song Uploader queue auto-start — guarded activation

Source feature: PR #536 (merged into protected main).

Scope: activate the Song Uploader portion only, using the existing
[vps1-song-studio-release] [vps1-song-uploader-only] release markers.

No changes to uploader DB, records, queues, thumbnails, schedules, credentials,
or source media are requested by this marker. Existing idle fencing, backup,
health checks and rollback must be honored. Auto Script OFFLINE.

Short Uploader and Copyright Uploader are released independently.
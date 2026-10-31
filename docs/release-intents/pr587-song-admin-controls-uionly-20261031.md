# PR587 Song Uploader shared-controls UI-only release

Apply the merged PR587 `apps/local-upload-manager/part01/templates/app.html` to **only** the isolated Song Uploader runtime using the existing `song_uploader_group_summary_release.install(...,ui_only=True)` protected-main release lane. Do not dispatch Copyright/Short Studio, generic LUM, or Scene Candidate; those have separate protected releases.

The existing installer requires an idle Song queue, protects source ownership, backs up databases, verifies authenticated HTTPS and profiles, and rolls back if health checks fail. Preserve workers/queues/media/OAuth, Auto Script OFFLINE, and shared Google Drive. No new operational system.

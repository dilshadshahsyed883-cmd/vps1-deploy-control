# PR #544 / #577: Copyright Uploader tenant activation

Release intent, not an audit or migration. The protected-main Copyright Studio workflow must select **Copyright Uploader only** via its existing guarded installer, preserving Video Merger, Song/Short Uploader services, all OAuth credentials, channel mappings, profiles, queues, media, uploads and shared Google Drive. Refuse active uploads, preserve SQLite backups and rollback on failure. Auto Script remains OFFLINE. No two-account live isolation claim until tested.

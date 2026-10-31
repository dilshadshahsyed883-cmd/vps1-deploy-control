# PR587 Short Uploader shared-controls UI-only release

Apply the merged PR587 `apps/local-upload-manager/part01/templates/app.html` to **only** the isolated Short Uploader (Short Studio) runtime via the existing protected-main `short_uploader_group_release.install(...,ui_only=True)` lane. Do not dispatch Song, Copyright or generic LUM installers and do not deploy Short Maker.

The existing installer enforces idle/queue guard, source ownership, DB backup, authenticated UI verification and rollback, preserving all other uploaders and active uploads. Auto Script OFFLINE; Google Drive shared and unchanged. No new operational system.

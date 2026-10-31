# PR #544 / #577: Short Uploader tenant activation

Trigger only the existing Short Uploader isolated overlay. This is a release intent, not an audit report.

The guarded installer must preserve existing Short Maker jobs, Song Uploader workers, OAuth credentials, 35 assigned channels, upload history, live queues, and Google Drive shared settings. Stop if any active upload or conflicting host operation is detected. Back up SQLite, verify service and rollback on failure. Auto Script remains OFFLINE. Two-account live UAT is still required.

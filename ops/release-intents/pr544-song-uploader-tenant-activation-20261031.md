# PR #544 / #577: scoped Song Uploader production activation

This is a deployment intent (not a production audit, migration, or report).

Target: existing Song Uploader instance only, via the protected-main Song Studio workflow and existing immutable, rollback-ready Song Uploader overlay installer.

Required protections:
- Keep shared Google Drive unchanged.
- Keep Auto Script OFFLINE.
- Preserve all channel, OAuth, profile, schedule, queue, job, upload and media records.
- Installer must refuse live uploads and enforce host serialization, service-health checks, backup and rollback.
- No changes to Song Maker, Audio Downloader, Short Maker, Copyright Studio or Scene Builder.
- Source PR #544 tenant filters and PR #577 tenant report metadata gate must be packaged.
- Production verification is not complete until release receipt and two-user isolation UAT.

Release dispatch markers are in the squash merge commit title, not in this file.

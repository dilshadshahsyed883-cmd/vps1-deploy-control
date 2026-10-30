# Song Uploader group sources — scoped release intent

New profiles use Group · Multiple Channels. Keep existing channel profiles,
queues, records, OAuth and all uploader databases; no bulk conversion or deletion.
Group membership eligibility, quotas, work mode, scheduling and duplicate guards
remain on the existing group engine. Both video sources (Drive / Song Maker)
and metadata sources (Sheets / Manual on Website) now work in group mode.
Imported local video/thumbnail pairs share the same Song Maker source identity;
strict thumbnail publishing and verified cleanup use the existing worker lifecycle.
Manual metadata never writes to Sheets. Group metadata uses the existing group
schedule, including when saved on the website. Original matching thumbnails are
required; thumbnail rotation cannot substitute an unrelated asset.

Recover the requested Dashboard, Auto Engine, Channels, Sheet Sync, Logs order.
Preserve the prior minimal UI and collapse Sheet format help. Existing individual
profiles remain editable using their saved binding, without a channel selector.

Release only through the existing protected-main, Tailscale app-scoped LUM lane:
`[vps1-song-studio-release] [vps1-song-uploader-only]`.
Retain job-level shared production concurrency (`cancel-in-progress: false`),
host deployment locks, rejection of stale main SHAs, idle fences, database backup,
health checks and rollback. No broad deployment or direct public-IP SSH.
Do not enable Auto Script or start publishing as a deployment check.

Validation: all four source combinations exercised against real isolated SQLite
stores, original thumbnail snapshots, inactive-channel exclusion, source switching,
invalid metadata/group rejection and legacy-profile editing. Full offline suite:
719 tests, PASS with 138 dependency/environment skips; source-of-truth contract:
9 tests PASS; related production/uploader/handoff tests: 36 PASS.
Authenticated production visuals and owner UAT are NOT VERIFIED before deployment.

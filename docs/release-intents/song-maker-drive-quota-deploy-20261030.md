# VPS_1 Song Maker — Drive API quota optimization guarded rollout

Status: production release intent, not proof of delivery. Agent 1, VPS_1 only.

## Scope
Deploy protected-main Song Maker code from PR #389. Changes: 300-second auto-sync default rather than 60 seconds, one shared-folder image/audio listing, fewer redundant archive metadata scans with refresh immediately after each move, and exact-file remote ID/size/MD5 verification after MP4 upload.

Preserve current files, Song Maker queue and SQLite states, settings, original input and /used Drive folder contents, local MP4 retention, remote collision behavior, no-overwrite semantics, and independent Scene/Voice/Uploader apps. Do not touch Auto Script, regardless of its runtime state.

## Production admission and rollback
Use the existing Song Maker-only release workflow and its Tailscale/OIDC fixed host identity. The server's installed privileged helper must explicitly support song-maker; otherwise fail closed, do not upgrade the helper indirectly. The updater must verify the existing owned release, stop scanner, acquire the submission fence, and refuse active or queued jobs; preserve SQLite backup and on failure restore the prior pointer/units.

Success requires a completed protected GitHub Actions release with Song Maker healthy after cutover. Existing test suite is not a real Google Drive upload acceptance test. Do not inject a test pair into the owner's shared folder; real Drive acceptance requires an isolated authorized test.

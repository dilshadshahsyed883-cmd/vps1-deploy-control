# VPS_1 Storage Manager read-only manual-spool owner activation

Activation follows a separate successful guarded root Storage GC installer.
This is intentionally a **read-only Storage Manager candidate release**.

## Source already merged

- GC installer resolves the active Scene web unit's non-root, stable real/effective UID.
- 0700 manual request spool and 0600 private markers have a boot-persistent matching owner.
- Storage Manager `_scene_manual_spool_owner_uid` returns the expected actual Scene web account instead of hardcoded ubuntu.
- Guarded web route remains administrator/session-CSRF protected, explicit confirmation and one-shot only.
- No media deletion, runtime lock bypass, active worker restart, or Auto Script activation.

## Release safety

Use only `[vps1-storage-backend-retirement-release]` on the eventual squash merge.
Guarded Storage Manager service candidate release must verify health, read-only snapshot
and rollback. Do not include any GC release marker in the same commit.
Do not run `--apply` from release process.
The button can queue cleanup after healthy, aligned owner; reclaimable estimates
are not measured bytes freed. Verify actual last-cleanup audit separately.

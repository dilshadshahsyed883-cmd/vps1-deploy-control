# VPS_1 Storage GC — skipped candidate audit (2026-10-30)

## Scope and evidence

Repository-only source audit of `production/storage_gc.py`, the root-owned
status publisher, the Storage Manager read-only endpoint, the Scene Builder
Storage dashboard, tests, install helper, and protected GitHub Actions lanes.
Operator screenshots captured **2026-10-30 17:44 PKT** for the last cleanup:
**0 B freed**, **0 deleted**, **229 skipped**. The subsequent independent
read-only scan showed **13.9 GiB** potentially eligible, while **24.6 GiB**
remained free of **71.6 GiB** total storage.

**Evidence limit:** the screenshots and GitHub source do not contain the
individual private audit skip entries; they do *not* prove any one reason for
the 229 skipped candidates. No root login, private artifact dump, media
deletion, or on-host repair was performed in this source audit.

## Findings

1. **Unexplained cleanup gap — confirmed UI/telemetry issue.** The guarded
   apply records its per-candidate `skipped` array in a root-owned audit,
   but the public publisher and isolated dashboard display only its length.
   This prevents operators from differentiating deployment lock contention,
   runtime process protection, service-inactive status, revalidation changes,
   and unexpected skips.
2. **Different measurement points — confirmed.** The 13.9 GiB is a separate
   read-only scan (`inventory.estimated_bytes`), not an actual deletion
   outcome. The dashboard must not imply 13.9 GiB can all be recovered.
   Application candidates use a 12-hour grace; detached control checkouts
   preserve the latest 12 and use a **48-hour** grace.
3. **Safety gates — confirmed.** Deletion requires root, approved path,
   deployment locks, current active/rollback references, verified SHA
   ownership, service health, live process-reference checks and repeated
   eligibility validation. Unreadable process state is fail-closed.
   The GC does not delete application project/media databases or queues.
4. **Mobile overlap — observed in operator screenshots.** The fixed shell
   navigation button obscures the Storage heading at narrow viewport widths.

## Changes proposed in this branch

- Export only allowlisted, aggregate `skip_reason_counts` and independently
  timestamped candidate counts. The legacy audit list is parsed on the
  root-owned side; checkout SHA, host path and file content are *never*
  exported to the dashboard.
- Match root-owned public audit timestamps to the read-only web-side last
  recorded run. Reject unknown keys, negative/oversized values, mismatched
  reason totals, and stale cross-run associations.
- Show why candidates were skipped; mark the reclaimable value a read-only
  **scan estimate** rather than promised free space.
- Add a mobile hero top offset and lightweight regression tests for
  redaction, backward compatibility and trust boundaries.
- No safety gates, retention windows, active pointers, rollback copies or
  data deletion scopes have been weakened.

## Acceptance / release gates

1. CI: `python3 -m unittest production.test_storage_gc` and
   `python3 -m unittest production.test_storage_auto_clean_view`, plus
   existing Storage GC + Storage dashboard suite.
2. Merge only after required checks pass. Activate **GC publisher first** via
   the protected, scoped `[vps1-storage-gc-release]` workflow. After live
   timer verification, activate the **dashboard adapter + UI** via
   `[vps1-storage-dashboard-release]` (in a separate serialized push).
3. Verify authenticated read-only Storage Manager dashboard: timer active,
   last run time, skip reason summary, scan candidate count, measured freed
   bytes, and no sensitive paths/SHAs.
4. If a fresh audit still reports 0 B removed, use the aggregate reasons to
   investigate. **Do not force-delete** protected or unverified candidates.
   Keep the approved 12-hour timer, existing 48-hour control-checkout grace,
   protection of 12 latest checkouts, and Auto Script OFFLINE.

## Status

Source fixes and tests are on the linked GitHub branch. Production acceptance,
actual cause of the 229 skips, and post-deployment free-space delta are
**not established by this document**.

## 2026-10-31 follow-up: manual safe cleanup + fresh telemetry

- Root-owned read-only status publisher now runs every 30 minutes via a separate
  timer, without deleting anything. Estimates older than 2 hours are rejected.
  A repeated value from a fresh scan is a valid reading, not a forced change.
- Admin-only **Run Safe Cleanup** requires an authenticated admin session,
  session-bound CSRF header, explicit confirmation and a two-minute cooldown.
  It writes a fixed one-shot request in a restricted tmpfiles-managed directory.
- A systemd `.path` starts the same guarded GC service already used by the
  12-hour timer. Web workers do not receive root privileges or delete paths.
  Active deployments, process references, rollback copies, grace periods,
  user media and queues retain every existing deletion guard.
- A queued request is not counted as freed space: only the verified root audit
  supplies successful-run metrics. Manual and timer invocations are distinct
  triggers for the same operation and each run is independently audited.
- Production deployment and actual reclaiming remain unverified until the
  separately scoped GC and dashboard releases complete.

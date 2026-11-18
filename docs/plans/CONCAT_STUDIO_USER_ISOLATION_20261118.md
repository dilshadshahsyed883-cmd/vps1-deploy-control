# VPS_1 — Separate Concat Studio & Owner-Isolated Concat Uploader (PLAN)

**Date:** 2026-11-18  
**Status:** PLANNING ONLY — no application code, PR merge, build or deployment  
**Repository:** `dilshadshahsyed883-cmd/vps1-deploy-control`  
**Planning branch:** `plan/concat-studio-user-isolation-20261118`  
**Related:** [Concat Uploader #766](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/issues/766), [Concat Cloud Render #763](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/issues/763), [VPS thin-relay PR #764](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/764).

## Decision and exact scope

Create a **new top-level website navigation group called Concat Studio** with exactly two entries:

1. **Concat** — existing Cloud Render / Concat production and media workflow; reuse the existing application, queue, verified MP4 and Drive Bridge.
2. **Concat Uploader** — reuse existing Copyright Uploader UI/services, with a Concat completed-video-link source adapter, preserving its agreed four-tab structure.

Do **not** make a new Concat editor, renderer, GPU container, separate upload worker, Drive OAuth system, or media library. The existing Concat desktop/Salad worker remain in the separate `concat` repository; VPS_1 remains a secure transport, persistent queue, and verified output delivery layer. No new dashboard or unrelated studio changes.

The website navigation hierarchy should be:

```text
Main sidebar
├── [other unchanged studios]
├── Experiment Studio      (existing unrelated entries remain)
└── Concat Studio          (NEW top-level group)
    ├── Concat             (existing Cloud Render / Production + Media + GPU Config)
    └── Concat Uploader    (reused uploader with Concat source)
        ├── Sheets Sync
        ├── Channels
        │   ├── Channel Manager
        │   └── Accounts & Tokens
        ├── Upload Engine
        └── Queue
```

**Authoritative tab order** is the latest clarification in #766: **Sheets Sync → Channels → Upload Engine → Queue**. The earlier conversational placeholder order is superseded. Concat Uploader's only video source field is **Concat Video Link**; retain channel, title, description, thumbnail, visibility, schedule and existing supported metadata controls.

## Reuse and routing plan

- Existing Cloud Render/Concat has been labeled `Concat` in Experiment Studio via merged PR #751 while keeping its existing `/cloud-render/` URLs. Add a new owner-aware `/concat-studio/` landing route and a `/concat-studio/concat/` route/view into the same canonical Cloud Render implementation. Establish one canonical app URL; old `/cloud-render/` and bookmarked Experiment Studio deep links should resolve compatibly or redirect after authorization, with no broken POSTs/API clients.
- New `/concat-studio/concat-uploader/` UI routes to the reused existing Copyright Uploader channel, Sheets, OAuth, form and queue services. Keep uploader state distinctly scoped by app=concat and authenticated owner.
- Once new routes work and tests pass, remove Concat and Concat Uploader **navigation links** from Experiment Studio only; do not delete existing code, archives or active jobs. Do not unhide unrelated Experiment Studio entries as a side effect.
- Reuse the website's shared native shell and role-aware navigation; avoid iframes or duplicated app processes. Admin and non-admin get the same **feature controls** in Concat Studio. Privileged server management/credentials stay restricted where operationally necessary; equal UI does not grant cross-user administrative access.
- Verify the actual shared-sidebar, Cloud Render, Copyright Uploader and owner-auth code on the implementation branch before choosing exact files/functions. Historical documentation may be stale.

## Isolation — mandatory backend contract

No identity inferred from submitted `owner_id`, URL path, file name or frontend state. Resolve a stable authenticated Scene user identity server-side and enforce it **at every API query, job transition, read, write, download, preview, delete, media link resolution and worker callback**.

| Resource | Required boundary |
| --- | --- |
| Concat render inputs / project ZIPs / persistent queues / retry state | Owner-scoped, including list, create, status, cancel, retry, delete |
| Completed MP4, previews, downloads and Copy Video Link | Owner-scoped authorized asset ID; never public raw VPS path or unsigned arbitrary remote link |
| Concat Uploader queue, history, job logs, result URLs | Owner + app namespace; no cross-user queue controls |
| YouTube channel inventory, per-channel OAuth tokens and client JSON | Owner-scoped, credentials masked in UI/logs and never exposed across accounts |
| Sheets Sync configurations, rows, status writeback | Owner-scoped config and access to channel mappings |
| Storage / temporary handoffs / canonical Drive uploads / cleanup | Owner-scoped reference and retention; no deletion based only on another job's completion |

**Admin/non-admin parity:** same owner-level operations and panels; admin's own jobs remain isolated by default, and any explicit support/break-glass path must be separately authorized, auditable, and never part of ordinary list/API responses. Remove the present *admin-only* Cloud Render usage restriction only after the new per-user auth/storage invariants are tested; do not simply remove the route guard.

**Legacy records:** inventory ownerless/`__default__`/admin-era render jobs and files; explicitly attribute old records to the original rightful owner or quarantine for manual resolution. Do not make legacy data visible to every signed-in user or guess ownership from file names. Preserve existing job IDs, state and media during migration.

**Link contract:** `Copy Video Link` yields an opaque signed/authorized same-host asset reference with stable association to verified completed MP4, owner, job ID and source checksum. On paste, the server rechecks session and owner, status and media integrity. Prevent SSRF, arbitrary external fetch, local path traversal, stale-link access, cross-owner IDs and unintended Drive redirects. If canonical file moved to Drive, use the **existing Drive Bridge** access and verification; no new OAuth remote and no assumption VPS copy remains. Pin/retain an authorized source until YouTube upload completion; failed/cancelled jobs cannot erase canonical sources.

**Queue/upload contract:** retain manual Add to Queue and Start Queue, idempotency across restart, duplicate prevention (owner + verified source + destination), resumable/validated uploads, token expiry/error handling, timezone-safe schedule, actual YouTube ID/URL recording and cleanup only after verified success. A canceled or failed upload must preserve the source for retry and must never affect another owner's job.

## Engineering phases (implementation is NOT authorized by this plan)

1. **Read-only discovery:** map actual routes/shared navigation, Cloud Render storage and owner model, Copyright Uploader services, OAuth/Sheets/queue, Drive Bridge and available migration/test infrastructure. Capture current role restrictions and production-active queues.
2. **Ownership foundation:** add owner-scoped storage/query/media/queue/worker auth, owner attribution migration, authenticated asset links, strict API tests. Verify owner A vs owner B, admin vs non-admin.
3. **Move navigation / preserve URLs:** create top-level Concat Studio and two entries; reuse existing Concat route with compatible redirects and app shell; preserve current API/worker integrations.
4. **Mount Concat Uploader:** execute #766's minimal variant; reuse Sheets Sync, Channels (two inner tabs), Upload Engine, Queue, with secure Concat-video-link adapter and isolated persistence.
5. **Verification:** regression/security tests, cross-user negative cases, queue restarts, Drive-local and Drive-moved media, legacy bookmarks/jobs, OAuth/scheduling, no other uploader regressions. CI only on separate feature branch.
6. **Future release gate:** seek explicit implementation/deployment approval; then PR review, protected main CI, guarded Tailscale app-scoped deployment/readback/health checks and rollback. No automatic deploy from planning branch.

## Required test matrix / acceptance

- A and B can access their own Concat render queue and uploaded-video jobs; A cannot list, guess IDs, preview, cancel, download, copy/read links to, or delete B's assets or jobs (HTTP 403/404, no metadata leakage). Same enforcement for an admin user acting normally.
- Existing admin-origin Cloud Render records remain available solely to their rightful owner after migration; no ownerless jobs are globally accessible.
- New sidebar shows Concat Studio → Concat, Concat Uploader for each authenticated user; unauthenticated use denied, previous deep links/bookmarks still work securely.
- Owner A completes verified Concat MP4 → Copy Video Link → own uploader Paste → select own channel + metadata → Add to Queue → manual Start → verified YouTube ID and queue history, including refresh/restart.
- Paste owner B's copied link as A, arbitrary URL and local path: rejected. Link expiry/revocation/deletion and Drive-moved source handled without privilege bypass or silent data loss.
- All four uploader tabs and two Channels subtabs match #766. Sheets cannot import/map another owner's channels/jobs or run unrequested publishing. Private/unlisted/public and scheduled uploads follow shared YouTube restrictions.
- Existing Copyright, Song, Short and other uploaders, media, Drive remote and render worker are unchanged; active queues safe throughout; no regression in protected release/rollback checks.

## Guardrails and non-goals

- Planning branch carries documentation only. No code edits, build, production deploy, main merge, feature activation, cleanup of users/legacy media, token rotations, or release changes.
- Do not change the canonical Drive Bridge, deployed Salad image, Concat renderer, shared OAuth client rules, queue defaults, unrelated studio navigation or protected `main`.
- Keep #766 as the source of truth for Concat Uploader behavior; this document defines **new Studio placement + per-owner security + migration** and does not duplicate uploader implementation.

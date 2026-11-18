# STEP 1 — Concat Studio discovery report

Date — 18 November 2026  
Scope — source discovery only  
Inspected main — `9379c166026794994a64ffaffe4efaf3812c02ed`  
Planning branch before this report — `4635f4bc2a0d8651a8ae630c471e34c4c56fb82a`

## Source baseline and related work

The planning branch contains the existing [Studio plan](CONCAT_STUDIO_USER_ISOLATION_20261118.md). GitHub comparison reports divergence from current main, with the plan document as its changed file. Implementation must start from current main and carry the plan forward. Do not transplant the old branch's application snapshot.

[Issue #766](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/issues/766) remains the uploader requirements reference. The new top-level Studio placement in this plan supersedes its older Experiment placement.

[PR #767](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/767) is merged at the inspected main SHA. Its Copy Link button is completed source work and must be reused. The issue comment reports a successful guarded deployment. This discovery did not independently inspect the running VPS.

[PR #765](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/765) is closed. Current main still contains the other Experiment navigation entries. Do not reapply the abandoned hide-all change.

[PR #764](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/764) remains open, unmerged and explicitly on hold. Current main retains worker capabilities/validation preflight. Studio isolation does not require adopting that PR.

The previous conversation's `concat-uploader-766.patch` is a local candidate, without a verified implementation PR. It includes source-adapter and queue changes, but predates #767 and uses a nested Copyright URL. Review and selectively reuse its code only after rebasing on current main and applying the Studio ownership contract. Its earlier mocked test results do not establish this new Studio's readiness.

## 1. What exists and can be reused

| Area | Current source and behavior | Reuse boundary |
| --- | --- | --- |
| Concat page | [cloud_render.py](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/blob/9379c166026794994a64ffaffe4efaf3812c02ed/apps/scene-builder/part01/cloud_render.py), `register_cloud_render`, `/cloud-render/`, Production/Media/GPU Config | Keep one canonical implementation and existing API URLs |
| Production queue | [cloud_render_production.py](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/blob/9379c166026794994a64ffaffe4efaf3812c02ed/apps/scene-builder/part01/cloud_render_production.py), `ProductionQueue`, SQLite persistence, scan/start/pause/resume/cancel/retry/delete, saved remote IDs and endpoints | Reuse queue and verified delivery, add owner predicates and owner queue state |
| Media | `CloudRender.files`, `media/{job}.mp4`, incoming parts, size/SHA-256/MP4 verification | Preserve IDs, verified hashes and local/Drive states |
| Copy Link | [cloud_render.js](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/blob/9379c166026794994a64ffaffe4efaf3812c02ed/apps/scene-builder/part01/static/cloud_render.js), `copyMediaLink` copies same-origin `/api/cloud-render/media/{id}/file` | Keep this link format and bookmarks. Current route requires admin and serves local ready/failed/queued records only |
| Drive Bridge | `scene_rclone_env`, `execute`, `remote_stat`, `_upload`, `DriveIntake` | Preserve canonical runtime identity/config lookup, immutable copy, size+MD5 verification before local cleanup |
| Sheets | [sheets_bridge.py](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/blob/9379c166026794994a64ffaffe4efaf3812c02ed/apps/copyright-uploader/part01/lum_engine/sheets_bridge.py), `google_sheet_sync.py`, owned Sheet synchronization in `StudioTenants` | Reuse fetch/parser/channel mapping and status pipeline, add optional Concat metadata mapping |
| Channels/OAuth | `channels_web.py`, [oauth_web.py](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/blob/9379c166026794994a64ffaffe4efaf3812c02ed/apps/copyright-uploader/part01/lum_engine/oauth_web.py), [copyright_vault.py](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/blob/9379c166026794994a64ffaffe4efaf3812c02ed/apps/copyright-uploader/part01/lum_engine/copyright_vault.py) | Reuse channel manager, owner client JSON, shared-client fallback, OAuth sessions and distinct channel tokens |
| Upload pipeline | [worker.py](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/blob/9379c166026794994a64ffaffe4efaf3812c02ed/apps/copyright-uploader/part01/lum_engine/worker.py), `YouTubeUploader`, `upload_pipeline/stages/*`, `remote_verify.py` | Reuse resumable media, thumbnail, playlist, verification and cleanup stages |
| Upload persistence | [project_store.py](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/blob/9379c166026794994a64ffaffe4efaf3812c02ed/apps/copyright-uploader/part01/lum_engine/project_store.py), core `db/queue_repository.py`, upload ledger and successful filename history | Extend application scope while retaining IDs, receipts and retry evidence |
| Existing ownership | [studio_tenants.py](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/blob/9379c166026794994a64ffaffe4efaf3812c02ed/apps/copyright-uploader/part01/lum_engine/studio_tenants.py) | Reuse normalized authenticated username and explicit channel/group/Sheet ownership bindings |

Unchanged reusable helpers include ZIP path/structural validation, chunk integrity checks, remote verification, token classification/secret storage and the standard upload stages except where scheduled publish metadata needs a focused extension. Existing modules need scoped integration rather than physical copies.

## 2. Current authorization and security gaps

[Scene app.py](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/blob/9379c166026794994a64ffaffe4efaf3812c02ed/apps/scene-builder/part01/app.py) refreshes the active account and role on requests. Scene sessions provide username/is_admin. Trusted Nginx `auth_request` uses `/internal/workspace-auth` and overwrites uploader identity headers. Existing Studio grants cover song, short, copyright and voice.

| Surface | Current enforcement | Required isolation work |
| --- | --- | --- |
| Cloud page and browser APIs | Scene middleware denies non-admin Cloud Render access. Cloud route handlers also call login+admin. Mutations use Cloud CSRF | Add owner checks before relaxing either guard. Keep authentication and CSRF |
| Render jobs/media | `files` and `production_jobs` have no owner column. Lists and transitions use global IDs | Owner-filter list/read/change/delete/download, and validate output-to-input ownership on callbacks |
| GPU config/queue controls | One global config key/value table holds endpoint, worker/salad credentials, receiver token, auto-upload and production queue state | Separate owner-editable connection settings and queue controls. Keep operational infrastructure secrets restricted. An owner's config change must not redirect another owner's active job |
| Direct worker push | `/api/cloud-render/ingest/{job}/chunk` bypasses browser session middleware and authenticates a shared receiver token | Resolve existing owner-bound render job using server records. Unknown direct jobs need a server-held owner-bound credential/mapping before acceptance. A shared token or filename alone cannot establish ownership |
| Drive input discovery | `production_jobs.drive_id` is globally UNIQUE. `scan` can merge resource keys into an existing job | Scope duplicate identity by owner. Do not mutate another owner's discovered input or leak its existence |
| Existing uploader | Copyright instance enforces owner channel/Sheet/group/project access, including for normal admin operations. Other uploader instances allow some admin bypass | Preserve strict policy for Concat. Do not inherit general LUM admin bypass merely by assigning a new instance name |
| Upload application scope | Projects have owner but no distinct Concat application scope. Owner pause state and worker controls are shared within Copyright | Add app+owner namespace to queue queries, controls, event/history views and admissions |
| Channels/client/token vault | Owner client paths and owner token vaults exist; OAuth sessions carry owner, channel selection is checked | Reuse exact per-channel token selection. Sharing a client configuration never shares another owner's tokens |
| Media lifecycle | Local MP4 is removed after verified Drive delivery. No uploader source retention/reference exists | Resolve verified Drive-backed records and pin a safe input until upload completion/retry. Preserve canonical media |

Cloud Render admin-only controls currently protect global data. Removing those checks alone would expose all existing jobs, paths and credentials.

Uploader scheduling also needs a focused correction for #766. Existing form treats naive schedules as Asia/Karachi and delays application admission. The VPS worker passes `publish_at=None`, and current media stage does not emit YouTube `publishAt`. A future Concat schedule needs validated timezone-to-UTC conversion, future time checks and private+`publishAt` request semantics while preserving Copyright behavior.

Adding a Copyright video currently re-arms `engine_enabled` unless pause-after-current is set. Concat requires its own persisted manual-start default. Existing `process_queue` claims the manual core queue generally, so shared-worker integration needs job/application-scoped claims to prevent another application's jobs from running.

## 3. Required migration and storage changes

Use additive, transactional migrations with backup and idempotent rerun checks.

1. Add normalized owner identity to render inputs/jobs/media. Preserve all existing IDs, hashes, states, spool files and remote paths. Inherit output owner from its input job, including push, pull, retry and crash recovery.
2. Attribute legacy ownerless records using verified operator/account evidence. Quarantine unresolved records from normal user responses. Do not assign everything to the first admin who logs in. Inventory and attribution require later authorized runtime inspection.
3. Replace global Drive-input uniqueness with owner+Drive identity, preserving existing references. Add owner/query indexes.
4. Scope owner-editable GPU settings and persisted queue start/pause state. Retain in-flight job endpoint/credential association across configuration edits, including restart. Keep global worker-capacity limits where required for one shared worker.
5. Add uploader application identity or an equivalently enforced persistent discriminator. Backfill existing rows as Copyright, preserving queue/core IDs and history. Scope owner control rows and visible history by application.
6. Claim duplicate uploads by owner, stable verified source identity and destination channel. A cancelled job with evidence of a possible remote upload must retain its duplicate claim until reconciliation.
7. Add retention references or safe staging pins tied to admitted uploader jobs. Local same-filesystem hardlinks can avoid copying where permissions permit. If fallback retrieval is needed, use canonical Drive Bridge and verify recorded hashes. Never hand arbitrary URLs to a downloader.
8. Reuse existing username identity conventions. Account rename/recreation implications must be explicitly handled if identity becomes mutable. Avoid a separate user database.

No credential export, token regeneration, media deletion or worker/image changes are needed for discovery.

## 4. Exact implementation change map

Paths below are relative to the repository.

| Files/modules | Required change |
| --- | --- |
| `apps/scene-builder/part01/cloud_render.py` | Owner-aware config/media/ingest/query APIs, authorized existing link resolver, Drive-backed handoff and retention |
| `apps/scene-builder/part01/cloud_render_production.py` | Owner migrations, intake deduplication, queue state, worker job ownership and cancellation/retry checks |
| `apps/scene-builder/part01/app.py` | Concat Studio entry/aliases, access policy/session Studio grants, remove only superseded Cloud admin usage gates after isolation tests |
| `apps/scene-builder/part01/templates/base.html` | Server sidebar + Studio rail, preserve old URLs |
| `apps/scene-builder/part01/static/vps1_native_site_shell.js` and `.css` | Top-level Concat Studio, path classification, native/legacy rails and visibility for authenticated owners |
| `apps/scene-builder/part01/templates/cloud_render.html`, `static/cloud_render.js`, `static/cloud_render.css` | Shared Studio rail/context, owner-scoped actions, keep #767 copy behavior and Drive action |
| `apps/scene-builder/part01/precutover_policy.py` | Narrow allowlist for any new mutation aliases, with existing auth/CSRF intact |
| `apps/copyright-uploader/part01/lum_engine/standalone_app.py`, `project_store.py`, `worker.py` | Mount scoped variant, four tabs, admission adapter, separate controls/history, scoped core claims and safe recovery |
| `apps/copyright-uploader/part01/lum_engine/concat_source.py` (candidate new adapter) | Same-host link parsing, server-side owner+completed verification, local/Drive resolution and retention |
| `apps/copyright-uploader/part01/templates/app.html`, `base.html`, `copyright_queue.html`; relevant navigation/picker JS | Reuse controls with Concat context, exact four tabs and two channel subtabs, one source field |
| `apps/copyright-uploader/part01/desktop_core/modules/db/migrations.py`, `queue_repository.py`, `youtube_uploader.py`, `upload_pipeline/stages/media.py` | Only necessary scope, reconciliation and Concat scheduling extensions |
| `production/copyright_studio_release.py`, `production/runtime.py`, `production/gateway.py` | Review routing/packaging integration for future release. Existing uploader ingress is under Copyright permission; new Studio URL must receive its own trusted authorization mapping |

Proposed UI URLs from the plan are `/concat-studio/`, `/concat-studio/concat/` and `/concat-studio/concat-uploader/`. Keep existing `/cloud-render/`, `/api/cloud-render/*` and copied `/file` links as authorized compatibility routes. Browser GET aliases may redirect. Keep mutating API routes functional.

Reusing Copyright's process is possible, but its ingress currently requires the Copyright Studio grant. A nested `/copyright-uploader/concat` alias alone would inherit that permission and does not satisfy standalone Concat Studio access. Review shared ingress and app scoping before choosing the final mount. No new upload scheduler or renderer service is required.

## 5. Tests and readiness

Existing source suites to retain include:

- `tests/test_cloud_render_copy_link.py`, `test_cloud_render_v1.py`, `test_cloud_render_production.py`, `test_cloud_render_live_html.py`, `test_cloud_render_gateway_release.py`, `test_cloud_render_drive_web_release.py`.
- `apps/scene-builder/tests/test_cloud_render_drive.py`.
- `tests/test_copyright_queue_claim_cancel.py`, `test_copyright_owned_sheet_groups.py`, `test_copyright_pc_vault_restore.py`, `test_copyright_member_handover.py`, `test_copyright_studio_v2.py`, `test_copyright_uploader_ui.py`.
- `tests/test_three_studio_uploader_tenants.py`, `test_studio_tenant_http.py`, `test_three_studio_user_isolation.py`, `test_studio_owner_scoped_recovery.py`.
- `production/test_canonical_studio_navigation.py`, `production/test_studio_permissions_release.py`.

Missing acceptance coverage must include two owners plus an admin acting as owner, every Cloud operation and guessed ID, worker callback forgery/replay, legacy quarantine/rerun migration, cross-owner Drive deduplication, config switch during render, app-separated queue claims, manual start after restart, local cleanup racing uploader admission, Drive-backed source verification, expired sessions/ambiguous remote upload, exact scheduled YouTube request, all four tabs/two subtabs, old bookmarks and responsive navigation.

Existing CI lanes include `vps1-cloud-render-v1-ci.yml`, `vps1-cloud-render-drive-bridge.yml`, Experiment controls/access checks and uploader regression lanes. Extend appropriate path filters during implementation.

This step inspected source and repository history. It did not run application tests, browser checks, GPU tests or live uploads. Production data ownership, filesystem access for shared handoff, current active queue inventory and exact installed credentials/config associations remain runtime verification items.

## 6. Step 2 implementation instructions

1. Branch from the inspected current main or a freshly verified successor. Carry this discovery and the existing plan forward. Keep #764 on hold.
2. Implement owner schema, attribution/quarantine migration, owner-scoped Cloud queries and worker callback binding first. Preserve existing guards until negative isolation tests pass.
3. Scope config and queue controls without changing renderer protocol or Drive Bridge. Test owners, admin parity, in-flight configuration and restart behavior.
4. Add Concat Studio shell/access mapping and compatible existing URLs. Reuse #767. Keep unrelated Experiment entries in their present state.
5. Integrate the #766 uploader variant through existing modules, with scoped jobs/claims, manual default, verified local/Drive adapter and correct scheduling. Review the earlier patch selectively.
6. Run migration, isolation, queue/recovery and compatibility suites with mocked publishing, then CI on the implementation branch. Record remaining authenticated browser/live-upload checks separately.

Stop here. This report authorizes no application implementation or production action.

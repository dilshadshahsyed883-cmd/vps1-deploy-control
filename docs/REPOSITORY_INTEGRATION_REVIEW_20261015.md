# VPS_1 GitHub source-of-truth integration review — 2026-10-15

**Scope:** repository integration only. No deployment, live VPS access,
production service changes, branch rewrites or Auto Script activation.
The shared production website is **not yet cut over** to this Git checkout.

## Authoritative review base

- Private repository: `dilshadshahsyed883-cmd/vps1-deploy-control`
- Candidate baseline: `migration/fresh-vps-11groups-20261015`
  at `44658e8fef5c7daa8ff17098e0dff2c486228b09`.
- Existing PR #7 is DRAFT and targets `main`. The review PR containing
  this document targets PR #7's **migration branch**, not `main`.
- 12 existing GitHub Actions workflows completed successfully on this
  baseline commit (GitHub Actions evidence; tests are isolated, not
  proof of production readiness).
- This review adds a named, read-only `validate-git-source-of-truth`
  GitHub Actions check and repository contract tests. See its check
  conclusion on the review PR before considering merge.

## Current branch reconciliation

| Lane | State and disposition |
| --- | --- |
| #7 migration | OPEN/DRAFT, production merge BLOCKED |
| #8 common website shell | MERGED into migration, not main |
| #11 Nginx GitOps foundation | MERGED into migration, not main |
| #12 source/queue/Nginx integration | MERGED into migration, not main |
| #14 guarded activation rollback tests | MERGED into migration |
| #15 final symlink/rollback hardening | MERGED into migration |
| #13 superseded integration | CLOSED unmerged; do not replay |
| #9 source-of-truth draft | OPEN/DRAFT; **7 of 11** original changed file blobs are identical in the migration baseline; other 4 have later integrated/hardened versions. Do not wholesale merge. |
| #10 queue draft | OPEN/DRAFT; **2 of 5** original changed file blobs are identical in the migration baseline; other 3 have later integrated versions. Do not wholesale merge. |
| #6 Auto Script draft | OPEN/DRAFT, separate app-owned deployment toolchain: 6 branch-only paths. Do not merge/activate as part of 10 active apps. Preserve OFFLINE registry. |

The blob comparison above checks exact Git object identities, not just
similar filenames. Divergent branch history alone does **not** show
missing code. Branch owners must review any genuinely new follow-up work
before replacing integrated files. In particular, avoid rewriting
`rebuild/deploy_control.py`, source-of-truth staging, and shared
Nginx controls from older PR heads.

## Source ownership, manifests and UI contract

- `rebuild/groups.json`: exactly 11 tracked groups, **10 active** and
  `auto-script` **OFFLINE**, production/fresh-install/import flags false.
- `rebuild/install-profiles.json`: same registered group IDs, independent
  per-app service definitions and pending runtime contracts.
- `apps/<app>`: independent source namespace for active apps. Kokoro's
  separate source snapshot does not automatically replace Voice Studio.
- `rebuild/snapshots/live-source-manifest.json`: historical sanitized
  Agent source capture covering 10 groups and 413 source files, with
  per-file SHA256; **not a current post-edit release lockfile**.
- `rebuild/snapshots/targeted-source-manifest.json`: 24 source-only
  LUM DB Python modules with per-file SHA256.
- `apps/auto-script/SOURCE_SHA256.json`: four captured Auto Script
  source files with SHA256, retained offline.
- `rebuild/builder.py`: rebuilds immutable per-app source artifacts
  with SHA256 archive and per-file manifest, verifies complete archive
  contents; the new review check repeats this for all ten active groups.
- `rebuild/website_shell.py` + `rebuild/shell/`: one shell/navigation;
  each app retains its own UI, route, backend, queue and lifecycle.
  A shell iframe route is NOT proof that a service is running.
- `infrastructure/registry/apps.json`: intentionally empty pending
  canonical authorized live Nginx/service discovery. Do not guess
  upstream ports or turn on production routes.

## Merge and required-check gate

The integration review PR should merge **only into the DRAFT migration
branch** when its named check and relevant fresh-VPS workflows pass and
there are no stale/shared-control conflicts. PR #7 must remain DRAFT
until the owner separately reviews source reconciliation and all
production safeguards. No automatic production enablement follows
from a passing review check.

The GitHub App connector received **403 Resource not accessible by
integration** on `main` branch-protection reads. Repository rulesets
returned **403** with a private-repository plan upgrade message.
Therefore **branch protection and required checks are NOT verified**.
An administrator must verify/configure effective `main` protection,
prevent bypass/force push, require PR review and required status checks
(including `validate-git-source-of-truth`), and re-read protection
before main merge. Do not claim these controls are enabled.

## Remaining production blockers (owned by the other tabs)

1. Canonical OVH snapshot plus tested restore and backups of any runtime
   data/volumes not covered by the snapshot.
2. Live host discovery and approved Nginx/service ownership, queue and
   worker contracts, secrets and persistent data isolation.
3. Full authenticated app/browser/worker tests, isolated install, SHA
   provenance validation, exact rollback and runtime-drift evidence.
4. Verified 75 GB disk availability and actual shared dependency/model
   footprint against the aspirational 30 GB app target.
5. Main required-check/branch protection enforcement and explicit
   separately authorized production cutover.

**Production action count in this review: zero.** Existing PR branches
remain under their current owners. Do not enable production actions or
set staging/cutover approval variables from this review.

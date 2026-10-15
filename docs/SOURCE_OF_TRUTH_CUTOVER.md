# VPS_1 — GitHub source of truth and guarded release candidates

Canonical code source: private repository dilshadshahsyed883-cmd/vps1-deploy-control.
This lane is based on the unmerged fresh-rebuild PR and does **not** change
production. Existing Drive Bridge and snapshot/cutover safeguards remain in force.

## Contract

- Code, shell, app registry, release definitions, dependencies, CI configuration:
  GitHub commit is authoritative **after separately authorized cutover**.
- Runtime secrets, user uploads, databases, queues, media, model caches and
  ongoing jobs are persistent VPS data, never Git content.
- A merge changing only apps/<registered-app>/ can produce exactly that active
  app's immutable source artifact; Auto Script is OFFLINE.
- Registry, shell, dependency, build-tool and staging-tool changes require a
  separate shared-impact review. Unregistered apps fail closed.
- Artifacts record the full commit SHA and SHA256 checksums. VPS-side staging
  verifies the archive, every staged file and the active stage pointer.
- Each app uses its own serialized staging lock; production rollout is NOT
  enabled by this workflow.
- The common website shell and each application's UI remain independently
  versioned. A shell change cannot be silently deployed as an app patch.

## CI / staging behavior

Pull request: classify and test; build each safe changed application; NEVER
contact VPS. Merges into main: repeat tests/build; keep artifact evidence.
Optional automatic staging requires all these GitHub repository variables to
be exactly yes:

- VPS1_OVH_SNAPSHOT_VERIFIED
- VPS1_POST_SNAPSHOT_STAGING_APPROVED
- VPS1_GITHUB_BRANCH_PROTECTION_CONFIRMED
- VPS1_GITHUB_AUTO_STAGE_APPROVED

Staging remains restricted to the already established non-root
~/vps1-ci-pilot/fresh-staging tree. It does NOT restart systemd, change
Nginx, switch production routes or import database/media. Production rollout
is NOT implemented or authorized in this lane.

## Remaining hard blockers to production automation

1. Verify OVH snapshot and non-snapshot runtime backups/restore.
2. Enable and verify protected main with required CI checks; the GitHub app
   connector may not have permission to modify branch protection settings.
3. Merge source/rebuild PR after reconciling concurrent app updates and check
   source matches approved versions. Resolve known runtime blockers.
4. On an authorized VPS staging installation, validate actual service units,
   dependencies, health endpoints, shell routing/auth, queues and per-app locks.
5. Implement and test production release activation, health rollback and a
   read-only deployed-SHA drift check under the approved execution policy.
6. Only after separate explicit production cutover authorization enable the
   production deployment path. Never infer approval from a successful CI run.

Safe test:
    python3 -m unittest rebuild.tests.test_ci_release rebuild.tests.test_stage_fresh_app -v

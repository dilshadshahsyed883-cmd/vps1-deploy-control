# VPS_1 — GitHub source of truth and guarded release candidates

Canonical candidate source: PUBLIC repository dilshadshahsyed883-cmd/vps1-deploy-control (visibility verified 2026-10-16).
This lane is based on the unmerged fresh-rebuild PR and does **not** change
production. Existing Drive Bridge and snapshot/cutover safeguards remain in force.

## Contract

- Code, shell, app registry, release definitions, dependencies, CI configuration:
  GitHub commit is authoritative **after separately authorized cutover**.
- Runtime secrets, user uploads, databases, queues, media, model caches and
  ongoing jobs are persistent VPS data, never Git content.
- A merge changing only apps/<registered-app>/ can produce exactly that active
  app's immutable source artifact; Auto Script is OFFLINE.
- Registry, shell, dependency, build-tool, workflows, unknown scripts and
  staging-tool changes require shared-impact review. Unregistered apps and
  unknown changed paths fail closed; docs/tests are explicitly exempted.
- Artifacts record the full commit SHA and SHA256 checksums. VPS-side staging
  verifies the archive, every staged file and the active stage pointer.
- Each app uses its own serialized staging lock; production rollout is NOT
  enabled by this workflow.
- The common website shell and each application's UI remain independently
  versioned. A shell change cannot be silently deployed as an app patch.

## CI / staging behavior

Pull request: classify and test; build each safe changed application; NEVER
contact VPS. Merges into main: repeat tests/build; keep artifact evidence.
PHASE 1 HARD HOLD: the automatic main-push `auto-stage` job has an explicit
literal `false &&` condition. Even with all variables enabled it CANNOT SSH,
scp or stage on VPS_1. Candidate build/test remains available without host
access. This hold must only be removed by a separately reviewed Phase 2 change
after snapshot/restore, mutable data backups, Agent 1 Drive Bridge evidence,
protected main, security review and explicit user approval.

Historically optional staging requires these GitHub repository variables to
be exactly yes (currently INSUFFICIENT without removing the hard hold):

- VPS1_OVH_SNAPSHOT_VERIFIED
- VPS1_POST_SNAPSHOT_STAGING_APPROVED
- VPS1_GITHUB_BRANCH_PROTECTION_CONFIRMED
- VPS1_GITHUB_AUTO_STAGE_APPROVED

Staging remains restricted to the already established non-root
~/vps1-ci-pilot/fresh-staging tree. It does NOT restart systemd, change
Nginx, switch production routes or import database/media. Production rollout
is NOT implemented or authorized in this lane.

## Optional staging integrity watch

The staged integrity `audit` job is also statically disabled for Phase 1,
including the scheduled main-branch trigger; unknown old GitHub variables
cannot initiate VPS SSH. Re-enabling requires a reviewed Phase 2 change.

Once separately authorized, it can check one or all ten active groups
read-only each day or on demand.
The additional variable `VPS1_STAGING_DRIFT_WATCH_APPROVED=yes` is required.
It runs the trusted GitHub-checkout validator via SSH stdin without
copying the script to the VPS. It checks the stored release digests,
active commit SHA, unexpected files and symlink escapes; unstaged apps
are not reported as successful. Auto Script remains offline.
Local metadata can itself be tampered with: this is local integrity
monitoring, not independent GitHub artifact attestation or production monitoring.

## PUBLIC exposure and non-production main merge security gate

The repository is currently PUBLIC. No credential/history leak clearance is
claimed from a current-tree filename scan or a passing source CI suite.
Before treating main as the authoritative release source, complete a dedicated
full Git-history and working-tree secret scan, inspect imported backups and
sensitive paths, verify GitHub Actions environment/secret access, revoke or
rotate any exposed credential, and review third-party code/license exposure.
Repository visibility must not be changed automatically. Existing isolated
pilot workflow also supports main-push execution only for pilot-specific paths;
this migration does not change those paths. Do not merge additional pilot
changes without a separate trigger review.

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

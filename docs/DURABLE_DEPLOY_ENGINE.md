# VPS_1 durable deployment executor — integration contract

**Current state (2026-10-15):** GitHub-side code + CI synthetic transaction tests. **NOT deployed to VPS_1.** No GitHub workflow in this feature connects by SSH or changes a live service. Production switches in `rebuild/groups.json` must remain `false` pending separately verified Tab 4 controls. Auto Script remains OFFLINE.

## Responsibilities

- **Tab 1 / integration:** Reconcile the draft source-of-truth PRs and integration branch; make `rebuild/deploy_engine.py`, `rebuild/deploy-contracts.json` and `vps1-deploy-engine-ci.yml` a reviewed cohesive part of source of truth. This PR intentionally targets `integration/vps1-source-queue-nginx-20261015`, not `main`, to preserve in-progress reconciliation and prevent duplicate queue implementations.
- **Tab 2 / executor (this change):** Durable SQLite host-wide FIFO, immutable full SHA intake, app ownership validation, duplicate suppression, stale-claim recovery, retry policy, strict states, verified archive admission and dry-run simulated promotion/rollback. It extends the V2 queue *concept*; existing `IntentQueue` prototype is retained for backwards compatibility. The new database schema can upgrade an older prototype database in place. Migrated orphaned `CLAIMED` rows are quarantined.
- **Tab 3 / infrastructure:** Confirm Nginx, per-app service/worker names, dependencies, migration strategy, health endpoints, read-only verification permissions, release directory ownership and rollback procedures. No unreviewed service names or migration paths are assumed by this module.
- **Tab 4 / activation:** Perform verified OVH snapshot + restore demonstration, external DB/queue/uploads backup validation, canonical VPS/Agent Drive Bridge bootstrap, queue database ownership and persistence, legitimate runner-to-host intent handoff, exact GitHub release artifact SHA256 and trusted commit verification, service/worker preflight, migration policy, authenticated health checks, controlled failure test, rollback exercise and post-rollback verification. Only after separately approved production safeguards can a hardened on-host backend be installed and production workflows be enabled.

## How queue processing works

```text
Approved commit on protected branch
  -> GitHub source-of-truth classifier
  -> affected app builds/tests (in parallel by app)
  -> immutable source package + SHA256 release manifest
  -> [Tab 4 authenticated GitHub provenance + approved Drive Bridge handoff REQUIRED]
  -> strict per-app archive SHA256 and immutable manifest admission
  -> one persistent host-wide SQLite queue
  -> FIFO claim with exclusive lease
  -> verify deployed source/manifest + snapshot/backup and runtime contracts
  -> guarded atomic activation
  -> exact app/worker/queue/health verification
  -> VERIFIED, or automatic rollback + prior-version verification
  -> ROLLED_BACK, FAILED, or NEEDS_ATTENTION
```

`Engine` implements the last state machine. `SimulatedBackend` uses existing `release_transaction.transact` and `fresh_vps` and may only write under an isolated `/tmp` root. **A production host backend is deliberately not installed, activated or implicitly assumed to exist**. Admission through the queue is NOT deployment authorization. A GitHub-hosted runner's SQLite file is ephemeral: do not point multiple runner jobs to independently created local databases and claim host-wide durability. The persistent production SQLite file and the approved single handoff path must be hosted by the VPS execution agent, not GitHub-hosted runners.

## Queue states

| State | Meaning |
| --- | --- |
| `QUEUED` | Newly accepted, deduplicated by exact (app, 40-char Git SHA) |
| `CLAIMED` | Exclusive active deployment lease; no later app can activate |
| `RETRY_WAIT` | Pre-activation transient failure; bounded exponential delay; later FIFO requests cannot bypass |
| `VERIFIED` | Active revision passed backend verification or explicit recovery reconciliation |
| `ROLLED_BACK` | Prior healthy source reinstated and independently verified |
| `FAILED` | Non-retryable failure, or bounded preparation retries exhausted |
| `NEEDS_ATTENTION` | Worker crash, lease expired, uncertain activation, rollback failed; **all subsequent claims frozen** |

On a crash, never infer success from prior job logs. Compare actual active Git SHA to the requested and recorded previous SHA and independently verify health before calling `Engine.reconcile(job_id)`. If ambiguous, leave the queue frozen and alert the operator. Retrying after uncertain side effects risks corrupting queue/database state. A lease expiring during activation never produces VERIFIED automatically.

## CLI for non-production intake state inspection

```bash
python3 -m rebuild.deploy_engine --db /tmp/vps1-test/queue.sqlite3 enqueue --app short-maker --sha <FULL_40_CHAR_COMMIT_SHA>
python3 -m rebuild.deploy_engine --db /tmp/vps1-test/queue.sqlite3 list
python3 -m rebuild.deploy_engine --db /tmp/vps1-test/queue.sqlite3 enqueue-verified --app short-maker --sha <FULL_40_CHAR_COMMIT_SHA> --manifest /tmp/trusted-candidates/short-maker-<SHA12>.tar.gz.manifest.json
python3 -m unittest rebuild.tests.test_deploy_engine -v
python3 -m unittest discover -s rebuild/tests -v
```

The CLI accepts unverified *intents* for simulation; `enqueue-verified` additionally checks exact app/revision/archive digest and pins the artifact SHA256 in the durable record. This integrity check does not authenticate the GitHub run: Tab 4 must obtain the artifact from a trusted protected source and validate provenance before invoking it. Use `simulate-worker --root /tmp/APPROVED_TEST_ROOT --once` only after preparing a synthetic rootfs with an existing known-good release. The simulated worker deliberately mocks service restarts and health checks, so it must never be used to verify live production. There is deliberately no production deploy command. Use a dedicated file on a persistent VPS filesystem after authorized bootstrap; don't store the production queue in `/tmp`, `RUNNER_TEMP`, a Git working tree, or multiple independent runner-local databases. Back up both the queue DB and the deployment state before applying schema changes.

## Per-app runtime contract

`rebuild/deploy-contracts.json` is checked against the source registry and existing install profiles. All ten active applications have explicit dependencies and known service entries. No pending worker is silently assumed installed; all worker lists are initially empty and `runtime_verified=false`. Auto migrations are set to `false` for all apps. No database/queue/user upload paths are deleted, imported or migrated automatically. Each contract requires Tab 3 service mapping and Tab 4 health/backup evidence before it can be promoted to a production-ready state; a contract edit alone never enables live deployment.

## CI and evidence

`.github/workflows/vps1-deploy-engine-ci.yml` executes on both Python 3.11 and 3.12 and checks concurrent submissions, one-claim-at-a-time behavior, duplicate suppression, strict FIFO during backoff, bounded retry, legacy SQLite migration, orphaned leases, authenticated-token-like lease fencing, uncertain crash recovery, healthy/failed activation, atomic rollback, preserved mutable app data, and offline app controls.

GitHub workflow run: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/38758766583 — both matrix jobs successful. **Only CI synthetic safety proven.** CI does not prove SSH reachability, external backups, OVH snapshot restoration, real service restarts, real HTTP auth/smoke, Nginx cutover, queue persistence across host reboots, real migrations, or authorized live rollback.

## Merge and activation gates

1. Tab 1 reviews branch lineage, reconciles the draft PR #6/#7/#9/#10, protects `main`, and records required workflow checks. The integration branch is currently not identical to `main`; do not auto-merge.
2. Tab 3 fills and verifies worker and service deployment contracts under a separate reviewed PR, plus Nginx integration.
3. Tab 4 supplies snapshot restore/backup evidence and tests the durable host executor with the canonical Drive Bridge, safely bootstraps a restricted on-host executor plus trusted artifact handoff, and verifies rollback and active health.
4. Production controls are enabled only under an explicit user-approved cutover decision with passing required checks. No feature PR or CI success grants this authorization.

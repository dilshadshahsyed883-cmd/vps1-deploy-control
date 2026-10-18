# VPS_1 deployment control V2: offline source and queue foundation

Status: PREPARATION ONLY. No live VPS work, no cutover, and no queue executor.
This change sits on the draft migration PR #7, not production main.

## Implemented
- Validated app/source change detection against ten active registry groups.
- Source changes designate only the changed app for a future immutable release;
  dependents enter the test matrix, not automatic deployments.
- Shared or rebuild changes validate all active groups.
- Kokoro standalone snapshot is not Voice Studio's deployable source tree.
  Snapshot edits flag a required source synchronization and suppress stale
  Voice Studio release artifacts; unknown IDs fail closed.
- Auto Script is OFFLINE in this migration and cannot enter release/queue.
- A durable SQLite FIFO of deployment INTENTS with (app, commit) deduplication,
  one host-wide claimed request and strict terminal outcomes.
- A new GitHub CI workflow builds the change plan, tests queue behavior and
  publishes deployment-plan.json as an artifact. No SSH or Tailscale step.

## Not implemented yet
This is NOT a real production execution queue. The prototype is restricted
to /tmp and has no worker or VPS consumer. A queue claim merely marks an
intent in test state: it DOES NOT deploy or restart any app. There is no lease
expiry/recovery, remote mutation, production rollout, or proven auto-rollback.
GitHub Actions concurrency alone is not a lossless FIFO (new pending jobs can
replace older pending jobs). Persistent host-side execution must be verified
and authorized separately using the canonical Drive Bridge process.

GitHub main protection and production-environment permissions also require
administrative verification. The source-only CI tests cannot verify browser,
external OAuth, FFmpeg media processing or actual VPS disk usage.

## Offline usage examples
    git diff --name-only BASE HEAD > /tmp/changed-paths.txt
    python3 -m rebuild.deploy_control plan --paths /tmp/changed-paths.txt
    python3 -m unittest rebuild.tests.test_deploy_control -v
    python3 -m rebuild.deploy_control enqueue --db /tmp/vps1-intents.db --app short-maker --sha FULL40CHARSHA
    python3 -m rebuild.deploy_control claim --db /tmp/vps1-intents.db
    python3 -m rebuild.deploy_control finish --db /tmp/vps1-intents.db --job-id 1 --result verified
    python3 -m rebuild.deploy_control list --db /tmp/vps1-intents.db

Only mark verified when independently proven. Production deployment remains
blocked until the OVH snapshot/restore, per-app service/worker gates,
explicit cutover approval and tested rollback are satisfied.

## Selective source artifacts
The PR artifact workflow now selects only direct changed active app source groups.
Core builder/registry/artifact workflow changes regenerate all ten source artifacts.
Manual workflow dispatch keeps one-app/all-app selection. A non-source
change never authorizes production deployment or stages live services.

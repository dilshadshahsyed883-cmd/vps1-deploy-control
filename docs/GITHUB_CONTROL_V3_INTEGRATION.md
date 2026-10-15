# GitHub Control V3 — integrated source-only candidate

This branch merges the Git-managed work from draft PR #9 and #10 plus Nginx
GitOps PR #11 into the already draft fresh VPS migration PR #7.

GitHub currently has TWO intentionally separate application registries:

1. `rebuild/groups.json` + `rebuild/install-profiles.json`:
   intended app ownership, dependency graph, source/CI packaging and
   prospective isolated fresh-rootfs installation. Ten active app groups.
   Auto Script is OFFLINE and must not be automatically activated.
2. `infrastructure/registry/apps.json`: confirmed **live** Nginx
   routes only. This remains empty until a source-verified read-only
   VPS routing audit identifies real upstream ports, service ownership,
   prefix mode, WebSockets and auth. DO NOT generate these live entries
   from tentative `install-profiles.json` values.

`rebuild/shell/assets/` is only a candidate website shell. A configured
route is not proof of working authentication, nested static assets or
browser-ready UI.

The queue is a durable SQLite **intent** test primitive, not a runner.
A `CLAIMED` intent does not mean a VPS command was run.
Real VPS agent deployment is out of scope until the user has confirmed
OVH snapshot/restore and canonical Drive Bridge prerequisites.

## Reconciled policies

- A normal owned app edit can make a source-only artifact and run CI tests.
- Unknown control-plane edits, registry/builder changes and Nginx routing
  changes demand review before any optional staging promotion.
  Rebuilding all inert artifacts for a core builder change is NOT
  automatic staging, and is never production deployment authorization.
- `apps/kokoro/` is a standalone snapshot. Those edits cannot silently
  deploy stale `apps/voice-studio/` files. Planner and source-of-truth
  classifier both block unsafe promotion pending an explicit owned-source sync.
- Auto Script remains OFFLINE despite parallel draft PR #6.
- Tests under `rebuild/tests/test_control_integration.py` check that
  Nginx route IDs (when eventually populated) are registered active apps,
  their ports match reviewed service profiles and the offline flag stays off.

## Evidence and unresolved operational gates

Combined CI must pass the source suite, real isolated rootfs smoke tests,
Nginx syntax, app artifacts, independent queue tests and release candidate
classifiers. This validates Git code only, NOT production behavior.

Main protection is still not enforced; configuring required status checks
and least-privilege production environments requires GitHub administration.
No production executor/rollback or real model/worker/browser/30 GB VPS
measurement is verified. No live changes are authorized by this PR.

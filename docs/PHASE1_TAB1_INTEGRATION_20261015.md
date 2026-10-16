# Phase 1 Tab 1 — Integrated GitHub control review (2026-10-15)

## Pinned sources and reconciliation
- Branch `integration/phase1-tab1-control-20261015` begins at latest inspected migration `d4d27a66e1c89e1e0d218555a61406a20c4ef9f9` (PR #16 already merged).
- PR #17 imported exactly 14 changed paths at `6562ba017a223665fedeff652ae893c4f3d09e64`. PR #18 imported exactly five paths at `f5d178578ce88527e1d40de5e8ace836fb403a82`.
- Migration's newer `rebuild/deploy_control.py` is preserved; older PR #9/#10 changes must not be replayed. All application sources, source manifest hashes, and offline Auto Script registration are preserved.
- Repository source-contract test is updated to treat only the verified Nginx live registry as authoritative: installing a service does not establish public routing.
- Durable-engine CI now checks migration/integration pull requests (not just older integration target), including Python 3.11 and 3.12 simulations.
- Shell, independent UIs, per-app service/worker templates, snapshot-aware source manifests, immutable Git SHA release admission, host-wide FIFO, SQLite lease fencing/recovery, and 30 GB storage admission remain separate non-production contracts.

## Integrated-head GitHub Actions blocker
First integration commit `9afd2867360cbcd7af138c93c10094425ec7df4a` triggered twelve GitHub Actions workflow runs. All failed within seconds **before any recorded runner steps**, and GitHub's job-log download returned BlobNotFound. Annotations exist but cannot be retrieved with the available connector. These observations are not application unit test failures, and their exact platform/account cause is **not established**. Do not declare CI PASS. Do not merge this branch while required CI checks remain failing.

Representative workflow runs:
- Source contract: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/38762648089
- Infrastructure: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/38762648018
- Clean Ubuntu: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/38762648020

## Main/production gate
- `main` branch's reported `protected` flag was **false** when inspected. Protection endpoint GET returned **403 Resource not accessible by integration**. Repository rulesets GET returned **403 Upgrade to GitHub Pro or make this repository public**. Do not bypass; authorized admin must establish effective protection and required checks on private repo or use an eligible plan.
- All source-of-truth runtime install flags remain false. Auto Script remains OFFLINE. No live ports, auth callback behavior, hardware/storage measurements or model reuse safety asserted.
- Real OVH snapshot/restore, immutable authenticated release provenance, external mutable state backups, exact service + Nginx mapping, health checks and live recovery remain Tab 4 requirements.

## Scope
GitHub repository only. No creation of another repository, production deployment, VPS/SSH invocation, database or queue mutation, Nginx reload, or destructive file action.

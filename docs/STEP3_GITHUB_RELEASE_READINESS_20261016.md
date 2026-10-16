# Step 3 — GitHub verification and release-readiness audit (2026-10-16)

**Scope:** GitHub repository and GitHub Actions only. NO `main` merge, production deploy, VPS access, Agent 1 execution, SSH/Tailscale/OIDC enablement, environment changes, or data mutation.

## Verified baseline (before this documentation PR)

- Repository: `dilshadshahsyed883-cmd/vps1-deploy-control`, **PUBLIC**.
- Protected `main`: `0abfeb9812722ed361b85c3966da5b3cd5d3d26d`.
- Open [migration PR #7](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/7) at `cc3767d42740c238644aef7bd250b615165733fc`, 138 commits ahead and 0 behind `main`.
- [PR #22](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/22) **merged** into migration. Its atomic verified enqueue / post-activation SHA and health guards and ten-app source tests are present.
- [PR #6](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/6) remains **DRAFT**: its separate Auto Script production SSH/OIDC/privileged helper workflow was **not** imported. Auto Script remains OFFLINE.
- GitHub Actions for exact migration SHA `cc3767d42740c238644aef7bd250b615165733fc`: **33 completed checks, 30 SUCCESS, 3 intentionally SKIPPED, 0 FAILED, 0 PENDING**. Includes Python 3.11 and 3.12 durable queue simulations, full isolated Ubuntu installation/rollback, public-data audit and Gitleaks source/history scan. [Pinned checks](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/commit/cc3767d42740c238644aef7bd250b615165733fc/checks).

**Note:** This is a dated, immutable baseline. A later documentation-only commit receives its own GitHub checks, reported on that PR and PR #7. Passing simulations never proves deployment on VPS_1.

## Source-side control verification

1. `main` branch reported **protected=true** and active `Protect main — VPS1` ruleset #25220297 applies to the default branch. It requires a pull request and successful `validate-git-source-of-truth` plus `offline-pilot` checks, with strict branch-up-to-date enforcement. Zero required approving reviews is configured for a solo-developer workflow; there are no ruleset bypass actors. Force-push and branch deletion are prohibited.
2. All **23** tracked workflows were inventoried. The six that contain direct Tailscale/SSH remote calls have explicit always-false job gates: `fresh-vps-source-of-truth.yml` (auto-stage), `vps1-staging-drift.yml` (audit), `fresh-vps-post-snapshot-stage.yml`, `vps1-pilot-deploy.yml`, `vps1-audio-404-probe.yml`, and `vps1-connection-check.yml`.
3. `fresh-vps-cutover.yml` includes a production environment but has a fail-closed guard followed by a deliberate nonzero exit; source flags `production_deploy_enabled` and `fresh_install_enabled` are both `false`. Manual database migrations, legacy imports and offline apps remain disabled.
4. `infrastructure/registry/apps.json` contains zero verified live routes. All ten entries in `rebuild/deploy-contracts.json` have `runtime_verified=false`, `health.enabled=false`, and automatic migrations disabled. Candidate profile ports **cannot** be treated as live ports.
5. Public repository security and source/history scan workflows passed on the pinned baseline. A passed scan covers the checked-out/reachable-ref scope, not inaccessible deleted history, external identities, actual GitHub secret ACLs or Tailscale provider authorization.
6. Historical 2026-10-14 / 2026-10-15 pilot and integration documents contain period-specific statuses, some superseded. The root README has been updated to state PUBLIC repo, current source architecture, and fail-closed restrictions rather than presenting that older pilot status as current.

## Still BLOCKED: provider / runtime gates

- [ ] [Security issue #21](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/issues/21): independently verify GitHub repository/environment secret exposure/access scope, deployment SSH user/sudo rules and trusted identity. The GitHub connector cannot read their effective grants.
- [ ] Verify Tailscale OAuth/OIDC client, `tag:ci`, ACL/grants, and host authorization in the **actual provider**. Do not infer from GitHub workflow text or historical reports.
- [ ] Verify required environment-review restrictions/provider sign-off rather than relying on unverified user-configured UI claims.
- [ ] Complete Agent 1 / canonical Drive Bridge V3 read-only runtime evidence: app services, nine historical listeners / seven profile-port mismatches, Script Work process ownership, real Nginx/auth/WebSockets/static behavior, per-app health, workers, databases, queues, media/storage capacity and rollback contracts.
- [ ] Validate OVH snapshot **restore** and external/non-snapshotted mutable-data backups before any VPS changes.
- [ ] Independently review final PR #7 exact head/base/checks/security and obtain **separate, explicit user authorization** before any `main` merge. Production deployment remains a further independent authorization; a merge is never a cutover instruction.

## Release decision

**GitHub source/CI framework: verified at the pinned baseline. Main-merge-ready: NO, because external privilege/security gate #21 is OPEN. Production-ready: NO, because real VPS deployment and rollback have not been verified.** Keep PR #7 open, Auto Script OFFLINE, remote SSH/OIDC workflows hard-disabled, current `main` protected, and the VPS unchanged.

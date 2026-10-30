# VPS_1 — GitHub deployment control (PUBLIC, production gated)

This repository is **PUBLIC** and contains the GitHub-controlled source, build and release framework for VPS_1. It is **not** evidence of a working production deployment, and **no GitHub-to-VPS production cutover is authorized**.

**Current reviewed candidate (16 October 2026):** [migration PR #7](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/7) targets protected `main` but is **not merged**. [Security gate #21](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/issues/21) remains open. Earlier pilot-specific reports are historical; do not infer current Tailscale, SSH, port or deployment identity state from them.

## What is implemented (GitHub / CI only)

- Source manifests, selective per-app artifacts, immutable Git SHA / SHA256 validation and isolated Ubuntu installation simulations.
- Ten active application groups with independent source/build contracts, a shared candidate website shell, and fail-closed GitOps Nginx routing.
- Durable host-wide SQLite deployment-intent queue with FIFO, idempotence, fencing/recovery and **synthetic** failure/rollback tests. This is not an installed production executor.
- Security scans for public-source hygiene and fetched reachable Git history, plus required offline CI checks.

**Auto Script status (29 October 2026): ONLINE, confirmed by the owner.** The scoped activation was introduced in merged [PR #314](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/314) on 25 October 2026; Experiment Studio navigation was added in merged [PR #315](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/315). The older `offline` flags in the *fresh-rebuild* contract refer only to exclusion from that migration, not the live runtime. Preserve the existing Auto Script scheduler, queues, SQLite and activation during unrelated releases; do not re-enable legacy SSH workflows. See [current status and scope](docs/AUTO_SCRIPT_STATUS_20261029.md). Independent live health proof is not part of this documentation update.

## Authoritative configuration

| Source | Meaning |
| --- | --- |
| `rebuild/groups.json` | Ten active fresh-rebuild targets; Auto Script excluded from fresh rebuild (runtime is ONLINE), with global migration switches disabled |
| `rebuild/deploy-contracts.json` | Per-app runtime ownership, services, workers, migrations and health; currently **not runtime verified** |
| `rebuild/install-profiles.json` | **Proposed** service ports / dependencies, not verified live VPS ports |
| `infrastructure/registry/apps.json` | Confirmed live Nginx route registry; currently **empty** |
| `docs/TAB3_APP_COMPATIBILITY_20261016.md` | Ten-app source compatibility and missing real-runtime evidence |
| `docs/STEP3_GITHUB_RELEASE_READINESS_20261016.md` | Current GitHub security / merge-readiness audit and remaining release gates |

The candidate shared shell and source build artifacts do not establish authenticated public URLs, real workers, persistent databases/queues, WebSockets or rollback on the VPS.

## GitHub safety boundary

`main` has active ruleset **Protect main — VPS1 (#25220297)** requiring a PR, `validate-git-source-of-truth`, and `offline-pilot`, with force-push and deletion blocked. Offline CI passing does **not** clear external deployment permissions.

Phase 1 GitHub-to-VPS SSH/Tailscale staging, pilot, audio-probe and connection workflows are hard-disabled in source. Fresh cutover remains fail-closed, and production/staging must not be enabled solely by modifying a GitHub variable. Refer to [SECURITY.md](SECURITY.md) and [the public-repository security decision](docs/PUBLIC_REPOSITORY_SECURITY_DECISION.md).

Before any main merge or VPS execution, reconcile the specific release gates in [the Step 3 audit](docs/STEP3_GITHUB_RELEASE_READINESS_20261016.md). Keep GitHub repository/environment secret grants, the Tailscale OIDC `tag:ci` ACL and SSH identity scope **UNVERIFIED** unless checked against their actual providers.

## VPS execution rule

Use the canonical `Automation_Master_System/10_MASTER_PROMPTS_INSTRUCTIONS/00_DRIVE_BRIDGE_CURRENT_RULES_V3.txt` workflow with **VPS_1 / Agent 1**. Packages belong only in `Automation_Master_System/VPS_1/01_COMMANDS/Agent 1/PACKAGES/`; Agent 1 reports belong in `Automation_Master_System/VPS_1/02_REPORTS/`. Preserve the working OS, SSH, Tailscale, rclone / Drive Bridge, models, databases, uploads and queues. An OVH snapshot claim is not restore proof; verify the snapshot and external data recovery before separately authorized production changes.

## GitHub-only validation

Use GitHub Actions on the exact candidate SHA as release evidence. For a local development checkout with its test dependencies installed:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s rebuild/tests -v
python3 tools/build_pilot.py
python3 tools/validate_release.py --manifest dist/release.json
bash -n scripts/preflight_readonly.sh
```

These commands are for a **local checkout / CI**, not instructions to execute against the VPS. They do not replace provider access review, authenticated live app smoke tests, independent data backups or rollback verification.

Never commit secrets, tokens, `.env` files, production user media, databases, archives or live agent credentials to this PUBLIC repository.

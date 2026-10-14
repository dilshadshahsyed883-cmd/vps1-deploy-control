# VPS_1 deployment control — safe pilot

Private, app-independent deployment control repository for VPS_1. **No production deploy is enabled.**

## Verified 14 October 2026

- Locked VPS execution agent: Agent 1, Google Drive Bridge V3.
- Read-only VPS preflight passed; Ubuntu, Git/Python/SSH are available; Tailscale is **not installed**.
- There is no confirmed tailnet identity, deployment user, deployment allowlist, or SSH authorization.
- Existing apps, services, ports, databases, Drive runners, and release pointers remain untouched.

## Pilot pipeline

1. Update a branch/PR in this private repo.
2. CI builds a **static, inert sample site**, runs offline manifest/archive checks, tests and syntax checks.
3. Bootstrap host access **only after** a separate verified Agent 1 preflight, a non-root deploy-user plan, pre-existing tailnet credentials, ACLs, and rollback proof.
4. Connect GitHub-hosted CI through short-lived Tailscale OIDC identity to the **pilot-only** VPS endpoint, test rollout + rollback, then request policy cutover.

No production deploy workflow, self-hosted production Actions runner, root SSH deploy, or network listener is included. The sample does not touch an existing app.

## Quick verification

```bash
python3 -m unittest discover -s tests -v
python3 tools/build_pilot.py
python3 tools/validate_release.py --manifest dist/release.json
bash -n scripts/preflight_readonly.sh
```

## Rules

Canonical: `Automation_Master_System/10_MASTER_PROMPTS_INSTRUCTIONS/00_DRIVE_BRIDGE_CURRENT_RULES_V3.txt`.
App-first supplement: `05_APP_FIRST_DELIVERY_RULES_V1_20261014`.
Migration design: `06_VPS1_GITHUB_DEPLOY_AGENT_MIGRATION_PILOT_V1_20261014`.

Read [SECURITY.md](SECURITY.md) before enabling any VPS-side operation. Do not put tokens, .env files, SSH keys or production source bundles in GitHub artifacts.

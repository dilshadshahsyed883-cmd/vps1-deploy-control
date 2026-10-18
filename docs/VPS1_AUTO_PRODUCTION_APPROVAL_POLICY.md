# VPS_1 production — owner-account automatic deployment policy

## Goal

Stop repeated manual **Review deployments → Approve and deploy** requests while preserving protected production GitHub → Tailscale → VPS_1 deploy controls.

## Owner-only GitHub Actions gate

The two production release workflows on protected `main` require
`github.actor == 'dilshadshahsyed883-cmd'`, along with their existing explicit
release marker / manual dispatch gate. Production secrets continue to be read
only inside jobs bound to `environment: production`, with restricted Tailscale
OIDC and pinned SSH transport. The VPS root helper only permits fixed operations,
allowlisted apps, and current protected-main SHA; per-app rollback remains in place.

This GitHub identity check is **not** a guarantee that the account cannot be
compromised. Protect the owner's account with passkeys/2FA and preserve required
branch checks. Unauthorized or unexpected actors cannot start these production jobs.

## One-time GitHub owner/admin setting — OWNER COMPLETED / VERIFIED 19 OCT 2026 (PKT)

At https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/settings/environments :

1. Owner opened **production** and disabled **Required reviewers** in GitHub Environment Settings; owner-confirmed.
2. A separate owner-only, production-Environment-bound no-VPS workflow independently verified the deployment gate is no longer waiting for approval.
3. Keep **Deployment branches and tags** restricted to the protected **main** branch.
4. Preserve all three existing Environment Secrets **TS_OAUTH_CLIENT_ID**, **TS_AUDIENCE**, and **VPS1_SSH_PRIVATE_KEY**. Do not move them into repository files or expose their values.
5. Keep branch-protection required CI, review-and-merge source policy, Tailscale and SSH restrictions and deployment health/rollback checks.

**Important:** GitHub's native *Required reviewers* rule does not provide
owner-triggered automatic approval; while enabled, runs wait even when the
GitHub actor is the owner. Disabling it is a one-time **GitHub Settings**
change by the repository administrator; changing a workflow YAML cannot
remove that environment protection rule. We do **not** add an automatic
approver using a reusable personal access token.

## Actual independent verification — PASS

- Owner confirmed changing GitHub Environment `production` Required reviewers settings.
- [PR #74](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/74) added a narrowly scoped protected-main owner-only Environment-bound approval proof workflow, 3/3 PR checks PASS.
- [GitHub Actions run #39169185919](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/39169185919), job `117798488891`, completed **SUCCESS**; pending deployments **empty**. Recorded exact log markers: `APPROVAL_FREE_PRODUCTION_ENVIRONMENT=PASS`, `VPS_OPERATIONS=NONE`, `SECRET_ACCESS=NONE`, `APPLICATION_DEPLOYMENT=NONE`.
- Proof was bound to `environment: production` and owner `github.actor` on protected main, but used no Tailscale, SSH, sudo, credentials, Nginx or VPS host connection. A successful run with no reviewer approval confirms this Environment did not require a manual reviewer at verification time.
- Direct production release workflows still require existing explicit push markers or an authorized manual dispatch. Owner-only actor gating was added via PR #73; this change does not mean every main commit can deploy. The root-owned SSH helper and production rollback still enforce independent operational controls.
- Future real production releases must still pass per-app trusted TLS, auth 401/200, JSON health, browser HTML, service readiness and app-scoped rollback conditions before new live status may be recorded.

**Current checked source SHA at approval proof:** `1ae1dd4fdc609437685265ebba47708d8b51d150`. Prior five-route release success: `39167302988`. Owner functional UAT remains PENDING, Auto Script OFFLINE.

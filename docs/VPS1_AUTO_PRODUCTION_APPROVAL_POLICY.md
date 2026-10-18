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

## One-time GitHub owner/admin setting (cannot be changed by this repository commit)

At https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/settings/environments :

1. Open **production**.
2. Under **Deployment protection rules**, turn **Required reviewers** off (or remove all required reviewers), then save changes.
3. Keep **Deployment branches and tags** restricted to the protected **main** branch.
4. Preserve all three existing Environment Secrets **TS_OAUTH_CLIENT_ID**, **TS_AUDIENCE**, and **VPS1_SSH_PRIVATE_KEY**. Do not move them into repository files or expose their values.
5. Keep branch-protection required CI, review-and-merge source policy, Tailscale and SSH restrictions and deployment health/rollback checks.

**Important:** GitHub's native *Required reviewers* rule does not provide
owner-triggered automatic approval; while enabled, runs wait even when the
GitHub actor is the owner. Disabling it is a one-time **GitHub Settings**
change by the repository administrator; changing a workflow YAML cannot
remove that environment protection rule. We do **not** add an automatic
approver using a reusable personal access token.

## Verification

After the owner changes the setting, the **next explicitly authorized**
production release on `main`, triggered by `dilshadshahsyed883-cmd`,
should start without a **Waiting for review** step. Do not trigger a real
deployment just to prove the toggle changed. A scheduled or on-demand
read-only host readback can verify branch restrictions and production OIDC
if needed. All actual app route/API and login checks must still PASS before
recording a live release.

**VPS_1 status when written:** existing five-route release and record
`c57f7797f1b813ad1025a17eae6603b9dbeead8b` VERIFIED; owner functional
UAT PENDING; Auto Script OFFLINE.

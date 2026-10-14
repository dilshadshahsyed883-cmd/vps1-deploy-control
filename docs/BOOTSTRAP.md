# Unblock one-time secure bootstrap (Agent 1 locked)

## Existing observation

Drive report: `AGENT_VPS1_AGENT1_GITHUB_DEPLOY_READONLY_PREFLIGHT_V1_20261014.txt`
`PREFLIGHT_RESULT=PASS`, `EXIT_CODE=0`, `TAILSCALE_CLI=ABSENT`, `TAILSCALE_DAEMON=ABSENT`, `SSH_SERVICE=active`. Source, privileges and app baseline were intentionally not scanned.

## Before installation

1. Decide whether to use a tailnet. Tailscale is **not** presently installed or authorized on the VPS. Creating a tailnet and a GitHub OIDC federated identity requires a tailnet administrator; this repo has no credentials.
2. Approve the proposed `tag:ci` ↔ `tag:vps1-pilot` single endpoint ACL; use the minimum service port, not blanket SSH access.
3. Capture current SSH/network routes and deploy-user permission boundary under Agent 1 with a read-only job. Do not overwrite SSH configuration, firewall, DNS or active services.
4. Prepare an install script on a non-production path with exact installed package source/version, rollback/uninstall instructions and a stop switch; verify idempotence before running.
5. Enroll the host in Tailscale using a one-time auth mechanism only if credentials are provided through a secure channel on VPS. Never embed auth keys in Drive job packages.
6. Confirm CI OIDC identity from the intended protected GitHub environment/ref and validate tailnet ACL with a single pilot-only read-only endpoint.
7. Deploy only a synthetic static sample into isolated staging, validate UI from a browser, test forced-failure rollback, and confirm original apps untouched.
8. Only then propose final controlled policy cutover from Drive Bridge-only to the approved new interface. Drive Bridge remains fallback.

## GitHub action integration (NOT enabled yet)

Following the official Tailscale GitHub Action v4 workflow, federated identity requires:

- `TS_OAUTH_CLIENT_ID`: the federation client ID, protected secret.
- `TS_AUDIENCE`: the federation audience, protected secret.
- `permissions: id-token: write` in the narrowly scoped deployment workflow only.
- Tailscale tagged ephemeral CI node, e.g. `tags: tag:ci`.

Do not create an executable deployment workflow until host access, branch protection, secret trust, environment approval and rollback have passed a guarded pilot.

Official guidance: https://tailscale.com/docs/integrations/github/github-action

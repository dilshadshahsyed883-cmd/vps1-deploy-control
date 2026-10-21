# Auto Script — isolated deployment scope

Production URL: https://app.scenebuilder.online/auto-script

Existing host application (historical audit, **not yet reverified**): `/opt/scene-builder-web`. Auto Script lives inside the shared Scene Builder service; it is **not** an independent service to replace wholesale.

## Git-managed source (pending current live sanitized snapshot)

The exact expected source files (and only those four files) belong in this directory:

- `auto_script.py`
- `templates/auto_script.html`
- `static/auto_script.js`
- `static/auto_script.css`

**DO NOT add** `.env`, credentials, `app.py` from the shared host, SQLite databases, queues, user content, generated media, runtime assets, configuration secrets, cache or logs.

Once the current Agent 1 Drive Bridge source archive has passed SHA256 verification, bring the four owned source files here, review diffs and verify the host's current integration. The release builder fails closed while any source is absent. Existing application/router bootstrap code is outside this app-owned update boundary.

## Production bootstrap gates

GitHub repository: `dilshadshahsyed883-cmd/vps1-deploy-control`. The dedicated workflow uses existing Tailscale OIDC/SSH and a distinct `auto-script-production` concurrency group. Set repository variable `AUTOSCRIPT_PRODUCTION_READY=yes` **only after**:

1. Agent 1 verifies current source and shared service, protected state directories, runtime owner, production health endpoint, and actual deploy capability.
2. Root-owned, allowlisted `/usr/local/sbin/auto-script-apply-release` is installed through authorized Drive Bridge bootstrap with one exact service ID (no unrestricted shell privileges).
3. A source-identical first release, health checks, automatic rollback and a controlled failed deploy have all been exercised safely.
4. The existing CI deploy principal has **only** the specific helper permission; GitHub ref/environment/credentials meet the production trust boundary.

CI checks / build run without this switch; production deploy is intentionally blocked. This repository branch does **not** modify production. Other app folders and workflows remain independent.

## Required verification evidence

CI test run, release SHA256, host copy SHA256, backup path, service health, auth/route checks, queue+DB regression, controlled failure rollback, browser UI testing, exact GitHub run/commit SHA and VPS Agent 1 DONE report. Do not label production PASS from a 303 redirect alone.

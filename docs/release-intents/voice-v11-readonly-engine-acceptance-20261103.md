# Voice V1.1 real live engine readiness acceptance — guarded read-only execution

Date: 03 Nov 2026 PKT. Scope: implementation of the **unchanged**, owner-approved frozen Voice V1/V1.1 acceptance.

## Verified previous production baseline
- Merged PR #676, successful protected Voice-only deploy #41570036288, SHA b7b3cac064bfe7988d47f97bbb5d7e44b74ccea2.
- Live port 8143 healthy, four Voice workers active, real Kokoro CPU synthesis passed, signed Scene SSO/anonymous denial verified.
- Existing Nginx config untouched; backups and rollback receipt recorded. GPU provider explicitly not configured.
- Merged PR #679 includes narrow read-only acceptance with **28/28 PR CI PASS** and no change to frozen UI/permissions/defaults.

## Exact approved operation
After protected CI PASS on this docs-only PR, merge with both exact markers:
`[vps1-production-release:voice-studio] [vps1-voice-readonly-acceptance]`.
The existing protected-main Voice-only GitHub/Tailscale deploy lane runs an *audit*, not an install:
1. Require exact protected-main SHA, Voice-only app selector and host serialization.
2. Verify existing managed Voice release pointer/record, localhost health and 5 active units (web + four workers).
3. Execute `nginx -t` only, with no Nginx edit/reload.
4. Obtain real Scene admin signed session and verify native Voice HTTPS health/UI plus anonymous denial and protected SSO internal route.
5. Verify signed GET status for Kokoro, Edge and Clone; require 401 for anonymous requests. Read Kokoro and Edge catalogue existence, never list user-owned job IDs or data.
6. Return non-secret booleans including Edge readiness, GPU configured/online and catalogue existence. If not ready, report it accurately; do not auto-fix credentials or workers.
7. Runtime and gateway branches MUST NOT start/restart services, install or switch source, mutate SQLite jobs, create synthesis jobs, upload Drive media, change user grants or expose tokens.
8. On failed authorization or release identity proof, fail closed with production preserved. No bypass or unapproved plan changes. Auto Script remains OFFLINE.

## Separately pending after read-only result
- Real admin and assigned non-admin two-account browser UAT of visibility and cross-owner access. Never impersonate another person with the admin's cookie.
- Edge real generation and verified Drive delivery: **NOT TESTED** by this read-only diagnostic.
- External OmniVoice GPU provider setup/real generation: separate admin-managed credentials and provider readiness, not enabled by this diagnostic.

Evidence:
- Source/CI: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/679
- Baseline release: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/41570036288

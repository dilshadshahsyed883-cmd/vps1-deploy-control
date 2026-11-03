# Voice Studio V1.1 — guarded gateway SSO repair release intent (3 Nov 2026)

## Proven issue and reviewed source
- Protected Voice-only run [41541509135](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/41541509135) reached live 8143 health, real Kokoro CPU synthesis PASS, and all four Voice systemd workers ACTIVE.
- It failed on the historical loopback Basic Auth HTML probe (HTTP 401) after Voice's native page was protected by the separate Scene-signed SSO session. Legacy gateway transaction rolled back; do **not** mistake Basic UI 401 for authorization failure under Scene SSO.
- [PR #674](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/674) merged at `b346b1e05181bd94f83388c848cf92af1b7d6dc4` after **11/11 offline checks PASS**, preserving concurrent Song Audio gateway protections. No UI feature or frozen plan changes.

## Production action
- One **Voice Studio only** protected-main production install, via approved Tailscale SSH identity, serialized host lock and immutable exact-main source.
- Enforce all-owner idle check (including the shared legacy Audio Downloader until confirmed split), database backups, media/Drive originals, unchanged Scene SSO and authorization, direct anonymous denial on Kokoro/Edge/Clone, Kokoro real CPU sample, all four workers ACTIVE, and rollback readiness.
- Legacy gateway Basic UI 401 is **not** accepted as success by itself. The new guarded gate requires an existing owned Scene SSO Nginx snippet, a real Scene admin session, valid authenticated HTTPS Voice health and native UI, public anonymous denial, hidden internal SSO endpoint, and direct anonymous denial on three Voice APIs.
- Any missing route, 401 under a supposedly valid Scene session, unhealthy unit, unexpected production drift, active work, or failed check must **fail closed**; keep previous release/data and report exact non-secret blocker. Never bypass CI, auth or queue safety.
- Scope: Voice Studio only. No Song Audio cutover, Scene redeploy, Short Studio mutation, user data deletion or Drive cleanup. Auto Script remains **OFFLINE**.

## Post-release acceptance
1. Verify protected GitHub Actions run reports Voice service and 4 workers ACTIVE, 8143 health, Kokoro CPU synthesis PASS, and `SCENE_SSO_VERIFIED` gateway proof.
2. Prove signed Scene SSO denial of anonymous requests, active Voice sidebar and all 3 engine views.
3. Perform real admin and non-admin two-account acceptance, showing identical permitted controls and strict per-owner separation; status remains `UAT_PENDING` until those two real user sessions are exercised.
4. Record exact deployed commit, release transaction, CI run, rollback record and results in the Master Progress document.

# VPS_1 Short Maker Kokoro UI — guarded root-idle fallback

Date: 2026-11-02. Latest protected source `f7c6ac908cfdf91baa1dc05350a4f8326dc0e3ce`.

## Decision
- Code is already merged from PR #658. This is **release intent only**, no app changes.
- The additional drain-readiness workflow run [41488002122](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/41488002122) uses unauthenticated `127.0.0.1:8146/api/jobs`, but Short Maker's endpoint requires trusted Scene SSO and applies per-user filtering. That check cannot certify **all-owner** idleness; do not bypass identity, forge headers or treat it as success.
- Use the existing successful `VPS1 Short Studio guarded release` with `[vps1-short-studio-release]` and `[vps1-short-maker-only]` on the protected-main merge commit; no drain marker on this commit.
- The workflow's `apps=(short-maker)` branch installs **only Short Maker** with production-scoped SSH identity, verified pinned fingerprint/host key and `vps1-production-deploy install REV short-maker`.
- `production/runtime.py` performs `require_short_maker_idle(state)` for any active Short Maker before stopping a service: checks **every** persisted `jobs/*/state.json`; unreadable state or any nonterminal job blocks release. This is a stronger all-user authoritative check than the broken per-user API probe.
- Production lock, app-scoped replacement, health checks and rollback remain intact; no other app is installed or restarted.
- Preserve Auto Script OFFLINE, `SHORT_MAKER_DAILY_PROFILES_ENABLED=0`, `SHORT_MAKER_COOPERATIVE_LOCK_ENABLED=0`, and `SHORT_MAKER_UNUSED_FOLDER_DELETE_ENABLED=0`. No frozen functionality changes.
- Release must be considered unverified unless the GitHub production workflow actually reports PASS and authenticated route inspection follows. The earlier broken readiness run is **not** proof of idle or deployment.
- The stale readiness workflow must be separately repaired to use authoritative all-owner evidence before any future use; never remove the root install idle guard.

## Evidence
- PR #658 merged: `ac75f7b4bf1beeb287556d1f4d0e0e3953c4c3bc` (CSS + tests, 23 green checks).
- PR #662: production environment secret scope fix; original readiness probe itself remains unsuitable for all-user idle certification.
- Existing same-family protected release run 41479741406 succeeded with production environment and pinned SSH.
- This fallback triggers a **single** Short Maker-only install on the exact release SHA.

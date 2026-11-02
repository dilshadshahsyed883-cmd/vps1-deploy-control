# VPS_1 Short Maker Kokoro Profiles/Folders — guarded production release intent

Date: 2026-11-02 (PKT)
Approved UI-only change: PR #658, protected main merge `ac75f7b4bf1beeb287556d1f4d0e0e3953c4c3bc`.

## Release scope
- Install **only short-maker**, using the existing `VPS1 Short Maker drain-aware guarded release` workflow.
- The guarded workflow's sole release marker is `[vps1-short-maker-drain-release]`. Do **not** also trigger `[vps1-short-studio-release]`, which would cause a duplicate deploy path.
- The release is CSS-only for `#shortProfilesPanel` and `#foldersPanel`, with a source CI regression test; no service/worker/API/queue/scheduler changes.
- Preserve the canonical 214px sidebar, 1400px max Video Studio content, 26px top and 34px gutter and responsive 1440/1024/768/390/360 gate. Shared shell unaffected.
- Preserve Short Maker and Short Uploader production data, media, Drive originals, credentials, user isolation and queue state.
- Leave `SHORT_MAKER_DAILY_PROFILES_ENABLED`, `SHORT_MAKER_COOPERATIVE_LOCK_ENABLED` and `SHORT_MAKER_UNUSED_FOLDER_DELETE_ENABLED` **OFF** (Phase 1).
- Auto Script remains **OFFLINE**.

## Safety and acceptance
1. Run full required PR and security CI on the exact release-intent HEAD; do not bypass main protection.
2. Merge only when protected checks pass and head is current.
3. On protected `main` push, activate only the dedicated `[vps1-short-maker-drain-release]` guarded release.
4. Existing Tailscale/SSH pinning, active-job drain and production serialized deploy prevent interruption. If idle state is unknown or jobs active, no install.
5. Installer must provide health verification and rollback readiness; do not alter other services.
6. After guarded install, verify protected `/short-maker/` route and authenticated Profiles/Folders visuals at 1440px plus mobile; keep browser verification explicitly pending until observed.
7. Do not infer successful deployment merely from PR merge or CI. Record production run URL, deployed SHA, outcomes and rollback status when available.

Functional frozen plan unchanged. This document is an auditable release intent, not proof of production deployment.

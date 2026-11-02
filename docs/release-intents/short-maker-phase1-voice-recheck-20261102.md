# VPS_1 Short Maker — protected Phase-1 live re-verification (02 Nov 2026)

Frozen plan: docs-approved PR #635, already merged. No change to Short Maker, uploader, Voice, Song, tenant ownership, Drive originals, existing Nginx routes, automation flags, or Auto Script OFFLINE.

## Verified evidence
- PR #635 code merge 98b7ade85ba881d3566b8dbcf00a5695728ac550: protected check gates green. Short Maker 8146 service recorded PASS_SERVICE_HEALTH during its guarded install, but the old global gateway promotion subsequently FAILED because voice-studio 8143 was inactive.
- Voice recovery protected run 41474246433 later recorded voice_8143=HEALTHY, voice_scene_sso=PASS, anonymous=DENIED, authenticated Short Maker gateway=PASS and seven legacy routes=PASS. A later independent Song 8152 cutover failed; this release rechecks current gateway requirements independently.
- Previous drain-aware wait workflow 41470083386 did not establish all-owner quiescence; its unauthenticated /api/jobs endpoint is not treated as an authoritative multi-user idle gate. Its job was skipped before deploy.
- Use only the already-reviewed **single** Short Studio release lane `[vps1-short-studio-release] [vps1-short-maker-only]`. This avoids triggering the separate drain-aware workflow in parallel. The production host's `require_short_maker_idle` checks ALL persisted jobs, not just a caller's view, before touching the active service.

## Production release acceptance
1. SHA matches current protected main; serialized Tailscale, existing SSH fingerprint pin, and native production environment required.
2. Install/reverify **only** `short-maker`; preserve other services and state, SQLite backup, immutable source pointer, HTTP health and rollback.
3. Preserve existing voice-studio live and authenticated gateway checks. Never suppress/rewrite an inactive public service, never disable SSO or shorten idle checks.
4. Leave `SHORT_MAKER_DAILY_PROFILES_ENABLED=0`, `SHORT_MAKER_COOPERATIVE_LOCK_ENABLED=0`, and `SHORT_MAKER_UNUSED_FOLDER_DELETE_ENABLED=0` in Phase 1.
5. Independently verify next four-service shared mount, all-owner drain, and unused-folder deletion before later activation. Admin/non-admin live acceptance only after safe deployment. Auto Script remains OFFLINE.

This is release-intent documentation only and keeps frozen feature scope intact. Failure must be reported as failure; no claim of production success without actual action logs.

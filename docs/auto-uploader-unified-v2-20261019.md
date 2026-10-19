# VPS_1 Auto Uploader — Unified Mode Coordinator (19 Oct 2026)

**Status: IMPLEMENTATION CANDIDATE — NOT LIVE MULTI-MODE PUBLISHING.**
Source: protected GitHub main; locked VPS_1 and Agent 1 for this chat.
Existing Owner UI: https://app.scenebuilder.online/local-upload-manager

## Reused production foundation

- Engine 1: existing Auto Engine V6 scheduler, queues, worker, Google Sheets and OAuth integration.
- Engine 2 / Shorts: separate read-only mode inventory, not installed as independent publishing services.
- Short Stream: independent FFmpeg/RTMP streaming service, never put into standard upload queue.
- Four-mode read-only registry: apps/local-upload-manager/part01/lum_engine/four_modes.py (PR #55).
- Existing upload records, OAuth secrets, Google Sheets, proxy settings and schedules are HOST-OWNED; no source/secret import into GitHub.
- Source repo is public as checked 19 Oct 2026; do not commit any tokens, media, private user data or database files.
- Auto Script stays OFFLINE.

## New work in this candidate

- A shared SQLite cross-mode channel+normalized-filename ownership ledger with BEGIN IMMEDIATE reservations.
- Historical receipts must be confirmed for all three uploading modes before claims can be reserved.
- Duplicate/previously completed/ambiguous uploads are blocked, including simultaneous claims.
- Safe failure can be retried by a different mode; remotely uncertain failures must stay blocked until reconciliation.
- No app database/queue migration, scheduling, upload, livestream or credential operation occurs on import or status read.
- Mode-specific coordinator health is exposed through the existing authenticated heavy-mode status response.
- Explicit env LUM_MULTI_MODE_CLAIMS_ARMED=1 required for any write; default is disabled.
- Not yet wired to legacy production uploader workers: doing so requires per-worker admission before each new remote upload, recording completion/ambiguity and disabling unsafe parallel legacy schedulers.

## Next deployment milestones (must not be reported as done)

1. One-shot host-only read-only inventory of actual Engine 1/Engine 2/Shorts roots, jobs, historical receipts, tokens and schedulers. Do not rely on pre-reinstall records.
2. Provision isolated mode 2 and Shorts runtimes/services and independent data roots; stage config without starting their uploading workers.
3. Import verified historical receipts into shared ledger; reconcile ambiguous jobs and existing publishers. Keep snapshots/rollback.
4. Integrate reserve/settle inside each uploader worker's *real* YouTube remote-publish pipeline, with crash recovery and immutable channel/video identity.
5. Activate workers one by one with automated non-publishing queue tests, login/HTTPS/health checks, then controlled publisher tests only when authorized.
6. Keep current four-mode original UI, route through Scene Builder login, verify stable tabs and navigation.

## Completion evidence required

GitHub CI must show test_uploader_mode_coordinator and existing test_uploader_four_modes PASS. Production go-live requires separate protected-main owner-only Tailscale transaction, remote service proofs, authenticated HTTPS, checks of duplicate ownership, and rollback result. Owner performs manual UAT afterward. PR PASS alone is not VPS PASS.

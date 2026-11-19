# Concat Studio Agent 5 — guarded integration status (2026-11-18)

Repository: `dilshadshahsyed883-cmd/vps1-deploy-control`
Candidate: `integration/concat-studio-final-20261118`
Draft PR: #771 (target `main`); not merged or deployed.

## Integrated
- Agent 1 discovery plan, Agent 2 navigation PR #769 (Concat and Concat Uploader), Agent 3 uploader reuse PR #770 (Sheets, channels/tokens, upload engine, manual queue), Agent 4 audit PR #768 upgraded with owner API fixtures.
- Existing Copy Video Link from merged PR #767; Thin Relay PR #764 remains on hold.
- Cloud Render's browser requests take owner exclusively from the authenticated Scene session. Production jobs, Media, config/credentials, queue and owner downloads are filtered by owner; admin has no cross-owner bypass. Worker output must correspond to an existing owner-bound Production remote ID.
- Idempotent owner-aware SQLite schema migration: legacy rows get empty owner and remain quarantined, IDs and delivery metadata preserved, Drive-ID uniqueness becomes (owner, Drive ID). Ownership is never guessed from current/admin account.
- Local or canonical Drive-backed MP4 staging verifies owner, MP4 header, size and SHA-256; only stored verified Drive path is used. Copy Link remains available after Drive cleanup.
- Standalone Concat Uploader is configured as its own loopback service on port 8021 with a private data root. Nginx source-only route relies on trusted Scene auth_request identity, replacing any user-supplied owner headers; reviewed rollback emits exact removal of that route. No Nginx change is live.
- Alice/Bob/Admin HTTP fixtures, SQLite migrations, queue and MP4 handoff tests, and a dedicated GitHub Actions workflow have been added. All are source/offline and require final green CI verification.

## Release blockers
1. Final SHA's GitHub CI and all regression/security suites must pass; queued/incomplete means not verified.
2. The guarded release must provision dedicated private uploader service, secret/env injection, correct Unix identity and canonical Scene rclone/Media read permissions, without exposing its loopback port. This code has not been installed on production.
3. Real Salad worker, Drive cleanup/handoff and YouTube resumable upload/scheduling acceptance have not been run in authorized external test accounts.
4. Before any live migration, review production database backup, ownerless record counts and independently verified attribution evidence. Never truncate, guess owners or automatically backfill.
5. Verify global GPU slot/owner concurrency limits against running services; prevent cross-owner worker config changes.

Release authorization requires a subsequent separate guarded deploy approval. Until then `main`, Nginx, VPS services, Docker image, Windows EXE, current OAuth, unrelated studios and production data remain unchanged.

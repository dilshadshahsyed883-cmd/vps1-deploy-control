# Song Studio / Voice Studio — safe backend separation (02 Nov 2026)

Status: **STAGED / NOT CUT OVER / NO PRODUCTION DEPLOYMENT**.

## Immutable functionality
- Song Studio owns **Audio Downloader → Song Maker → Song Uploader**. Preserve canonical `/audio-downloader/`, all owner-scoped jobs, queues, saved preferences, thumbnails, existing Song Maker handoff and Google Drive originals.
- Voice Studio owns **OmniVoice, Kokoro and Edge**. Preserve existing V1/V1.1 frozen UI, owner-scoped profiles/jobs, worker credentials and queues.
- Both retain the real Scene SSO and per-request studio grants; do not trust browser `X-Auth-User`, `X-Is-Admin` or local storage.
- Auto Script stays OFFLINE; no migration or deletion of SQLite/media/OAuth/Drive data.

## Safe staging contract
1. Legacy Voice still serves downloader and holds its worker by default. `SONG_AUDIO_BACKEND_SPLIT` defaults OFF. No live behavior changes unless an explicit guarded deployment sets it to `1`.
2. A standalone `song_audio_app:app` accepts **Song** grants only, exports no Voice synthesis APIs, and refuses to start without `SONG_AUDIO_CUTOVER_ACTIVE=1` and an explicit absolute canonical `AUDIO_DOWNLOADER_ROOT`.
3. The existing Audio Downloader worker now takes a single OS file lock inside its durable data directory **before any queue recovery**, held for its process lifetime. A second server fails to start rather than duplicating downloads/uploads or Song Maker handoffs.
4. The Song-only server now owns independent `apps/song-studio/part01/` source copies of the reviewed UI template, downloader API, and Scene session verifier, with a uniquely named Song API module (no import from Voice Studio). Scene now supports an exact, protected `SONG_AUDIO_SPLIT_UPSTREAM=http://127.0.0.1:8152` switch for **Song** APIs and HTML only. Unset is legacy-compatible; any other configured URL is rejected rather than proxied. This switch is not activated in production. A separate service process and guarded route change are still required to finish the isolation; copy/migrate data is forbidden.
5. No protected CI, Gitleaks rules, branch rules, authorization or workflow release conditions are weakened.

## Guarded cutover checklist — fail closed
- **Never run a legacy and new worker simultaneously.** First upgrade/verify the legacy Voice worker's lock, identify the real protected service env `AUDIO_DOWNLOADER_ROOT`, audit all-owner queued/running/retrying jobs and pending/sending Song Maker handoffs, and require zero in-flight jobs.
- Confirm unused loopback port and provision the independent Song Audio Python/runtime/systemd service under the existing protected Tailscale + deployment lock. New service uses the existing canonical Audio root, not a new copy or empty DB.
- Switch Scene's Song-only HTML/API proxy to the new service, and disable the legacy Voice Audio router/worker under the explicit split flag in **one transaction**; preserve canonical public URLs and authenticated/owner-scoped media responses.
- Health-check Song-only and Voice-only separately; test admin, Song-only, Voice-only and two distinct non-admin accounts. Verify Voice cannot list downloader jobs, Song cannot list Voice jobs, and neither can access another user's media.
- Keep a rollback journal and SQLite backups. On any failure, revert routing, stop the new Song worker, release its lock, restore the old Voice service and verify both paths **before** declaring rollback complete. Do not delete durable data.
- Run required full-history Gitleaks, Voice/Song CI, tenant isolation, deployment CI and other protected-main checks, all with genuine PASS. No bypass, skip, override, historical-finding exception, or production deployment from this draft branch.
- CI alone never proves the VPS cutover. Record exact protected merge SHA, actual installer conclusion, and post-deploy two-account acceptance after a guarded release.

## Acceptance state
This branch adds Song Studio-owned source modules, the isolated entrypoint, an exclusive worker lock, a default-off legacy Voice split switch, an exact Scene loopback proxy selector, plus offline isolation/SSO tests. It **does not** currently register a production unit, alter Scene routing, move the live queue or declare either studio separated in production. Remaining wiring is deliberately withheld until the guarded cutover transaction can be verified end-to-end.

**Important:** The legacy Voice service still has its own Audio Downloader API and its old worker until an approved release. Do not run the standalone Song service against live data until the complete guarded cutover transaction is validated.

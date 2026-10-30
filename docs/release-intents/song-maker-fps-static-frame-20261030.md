# VPS_1 Song Maker — guarded FPS optimization release intent

Date: 30 October 2026 | VPS_1 / Agent 1

## Owner authorization and source baseline
- Owner requested to proceed with production installation of the Song Maker FPS fix after PR #417 merged, all 14 PR checks passed.
- Fix merged into protected `main` as `93ae314d48230cfbc77917916d5012fd3c670d9a`.
- This file is a release selector only; it MUST NOT alter any running app, database, credential, queue, or media data as a source change.
- Trigger ONLY the dedicated **VPS1 Song Maker guarded release** workflow with both exact commit markers:
  `[vps1-song-maker-update-release]` `[vps1-song-maker-only]`.
  Never use the broader Song Studio selector or generic multi-app deploy.

## Scope
- Source change already reviewed in #417: a single static cover image is decoded once, looped as a decoded video frame at 25 FPS; encoder is bounded at 4 threads instead of 2.
- Preserve the existing 1080p, 720p and 9:16 output options, selected H.264 CRF quality, 192 kbps AAC audio, full fractional audio duration, jobs and Drive safety semantics.
- No changes to Audio Downloader, Song Uploader, Auto Caption Studio, Scene Builder, Short Studio, voice engine, or Auto Script.

## Safety gates
- Never stop or interrupt a running Song Maker render, upload or queued task to accelerate this release. If the idle-admission fence detects any active or queued job, reject without cutover or queue manipulation.
- Maintain the host-wide serialized GitHub production lane with fixed Tailscale/SSH identity, protected current-main SHA guard and exact app `song-maker` selector.
- Require the installed privileged helper's byte-level compatibility, existing app ownership, immutable release staging, pre-deploy real media tests, SQLite backup, bounded service stop/restart, authentication and health smoke checks, and scoped rollback on any failure.
- If provider issues, helper mismatch, DB state, configuration drift, authentication, or active workers block deployment, report BLOCKED and leave production unchanged. Never bypass with another app's installer.
- Do not generate or publish test media without the owner's explicit approval and an isolated authorized test pair.

## Evidence and acceptance
- Record the dedicated GitHub Actions run URL and exact deployed SHA, successful Song Maker-only service/HTTPS health and unchanged neighboring apps.
- Measure FPS and `speed=x` from the **next new actual production render** with unchanged resolution and audio profile. A local benchmark (~1.9x) is not production proof; 45 FPS must not be promised.
- Real Google Drive output verification remains separately pending until authorized end-to-end evidence exists. A successful release check is not a real Drive receipt.

## Second owner-requested production attempt — 30 October 2026
- First guarded run [41004084887](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/41004084887), attempt 1, correctly blocked on active or queued Song Maker work. Its GitHub "rerun failed jobs" attempt 2 was skipped by the push-only one-attempt guard; it did not attempt another VPS cutover.
- Owner explicitly requested to **try again now**. This documentation-only release-trigger commit is a fresh current-protected-main GitHub push with both selectors `[vps1-song-maker-update-release]` and `[vps1-song-maker-only]`.
- App source is unchanged from optimized PR #417. Do not interrupt existing renders, uploads, or queued jobs; idle fence must reject again if jobs remain. Rejecting busy work is a safe result, not a reason to bypass the protection or rewrite queue states.
- Verify the new GitHub Actions run ID, actual scoped worker/HTTPS result, and precise reason for success or block. Update [issue #422](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/issues/422).

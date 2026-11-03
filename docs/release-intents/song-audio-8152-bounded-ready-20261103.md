# Song Audio independent 8152 — guarded retry after bounded-readiness fix

## Evidence and approved scope

- Failed cutover `41475387692` passed all-owner downloader and Song Maker handoff idle checks, but the immediate port 8152 HTTP readiness probe saw ECONNREFUSED.
- Read-only protected diagnostic `41480699794` reported `ROLLED_BACK`, legacy Voice 8143 healthy, Scene route still legacy, no dedicated Song 8152 unit active and no detected Python-crash signature.
- Source-only PR #657 landed in protected `main` at `0c02e7043f1fa9cad28aa3cd5f9101858cc0a442`. Its _health_song waits at most 55 seconds (hard limit 60), accepts only transient connection failures/503 with the exact unit still active, and fails closed on all invalid worker identity, route or SSO checks. 17/17 latest PR-head checks passed.
- This release intent adds no source changes, no new permissions, and no alteration to the frozen Song/Voice plan.

## Exact protected deployment selectors

On a protected-main merge by the authorized actor, the **merge commit message** contains exactly the two release selectors `[vps1-song-studio-release]` and `[vps1-song-audio-separate]`; no other app or Song selectors. Use only the existing Tailscale pinned-host serialized Song/Voice/Scene transaction `production.song_audio_cutover.install`, not a generic install or manual systemctl cutover.

## Mandatory fail-closed gates

Before touching units, require a healthy original Voice service and canonical persistent Audio Downloader SQLite/schema, all-owner active Voice and Song jobs idle, Song Maker handoffs drained, free port 8152, existing release provenance, immutable manifests and durable root-owned backups. Preserve original database, media, Drive originals, profiles, OAuth credentials and historical jobs (including inert orphan rows). Verify no competing legacy worker before starting the dedicated Song process.

After staging, restart Voice in synthesis-only split mode, start **one** Song worker on loopback 8152 and use bounded readiness to prove real active worker, exact app identity and anonymous SSO denial. Flip Scene routing **last**, then independently verify authenticated canonical Song page and anonymous denial. Keep Voice synthesis endpoints on 8143 and all admin/non-admin owner isolation. No fake sessions, security-scan bypass, queue row changes, manual force or unverified cleanup.

Any failed gate, unit exit, invalid identity, unavailable storage, genuine active job or failed health check must stop the deployment and use the existing non-destructive rollback. Report the actual guarded-run receipt; do not claim live activation without it. Keep Auto Script OFFLINE and repository visibility unchanged.

## Acceptance and next evidence

- Protected-main release workflow succeeds; receipt `song_audio_cutover=PASS`, `worker=ONE`, route `SONG_8152`, original database `PRESERVED`.
- Voice 8143 healthy synthesis-only; Song 8152 sole downloader worker; Scene routes Song to 8152 with authenticated SSO and anonymous denial.
- Real admin and non-admin account tests confirm isolated queues and identical allowed operational controls, recorded separately after activation.

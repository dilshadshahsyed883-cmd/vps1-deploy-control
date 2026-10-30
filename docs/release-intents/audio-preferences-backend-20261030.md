# VPS_1 — Audio Downloader saved preferences backend release (2026-10-30)

## Reviewed source

- Source feature: merged PR #451, merge SHA `fa7594e47969753b71859936248c9e2fc9d9dc70`.
- Source code: `apps/voice-studio/part01/audio_downloader_api.py` (`audio_preferences` owner-scoped SQLite table and authenticated `GET/PUT /tts/api/audio-downloader/preferences`).
- UI was separately released with production receipt `PASS_SONG_STUDIO_VOICE_UI`. That receipt **does not verify** backend API deployment.
- Required branch checks, Audio Downloader contract tests, and Chromium UI tests passed on #451.

## Release objective

Apply the already-reviewed Audio Downloader backend module to the active Voice Studio release through the established guarded *filename-only* lane. The name of the lane is historical; it installs the current, validated Audio module from source. This release-intent document introduces no new runtime behavior.

Guard conditions and non-goals:
- Existing protected-main push workflow `vps1-audio-filename-release.yml` with the two required release tags and the exact current main SHA.
- Use existing Tailscale identity, host deployment serialization, Audio queue idle fence, SQLite backup, immutable new release, health checks, rollback.
- Keep proxy settings, login/session, Song Maker integration, saved files, queue/jobs and other Voice engines unchanged. No schema destructive migration.
- Preserve Auto Script **OFFLINE**.
- If a guarded smoke or health check fails, rollback and report failure; never claim production preferences persistence verified from a UI-only receipt.

## Acceptance evidence

Require deploy receipt `PASS_AUDIO_FILENAME_ONLY` (including backup/health and unchanged worker evidence). The new authenticated preferences routes must support owner-scoped GET/PUT and persist the six explicit download choices across reload/restart; anonymous access must be denied. Neither queued jobs nor historical jobs may be modified by preferences updates. No YouTube publishing is authorized.

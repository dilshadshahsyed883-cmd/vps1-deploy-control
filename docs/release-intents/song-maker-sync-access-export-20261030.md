# VPS_1 Agent 1 — Song Maker Sync Access and Personal Export Settings

Owner reported disabled **Sync Now** on Song Maker mobile UI and requested
export settings in the Settings tab, with rendering/queue batch controls.

Main branch already has an independently merged Song Maker queue/Settings/FPS
implementation; this focused patch addresses the **remaining Drive sync admin
gate** without overwriting that implementation.

- Scene session SSO must approve the user before Song Maker API requests.
- Any active user authorized by existing Song Maker workspace SSO may request
  Sync Now. A non-admin is hard-pinned to the existing named default folder
  `Song Studio/Audio downloader`, output `Song Studio/song maker`, and
  the configured application remote's code-approved default; this does not
  provide access to arbitrary folder IDs, remote selection or credentials.
- Only admins can change the shared Drive remote, source/output folder IDs and
  automatic scan interval. Automatic scan defaults to 300 seconds, configurable
  only by admins; manual scanning is requested immediately.
- Scan snapshot, matching and queued jobs remain strictly scoped to the
  authenticated Song Maker owner. Existing source-version idempotency remains.
- Every user may save a personal resolution/fit preset, applying only to newly
  queued manual and Drive-matched projects. Existing queued outputs untouched.
- Output tab stays removed; existing Queue preview/download, verified Drive
  links, FFmpeg performance reporting and selected batch controls are preserved.
- No real Drive uploads, credential changes, queue activation, or production
  mutations occur in branch CI.

Tests include non-admin SSO authorization, admin-only Drive configuration,
per-owner default and invalid export, Drive-pair export, and 5-viewport browser
smoke. Production must pass protected-main source/CI, existing Song Maker
idle-only Tailscale deployment release, health and rollback gate before live claim.

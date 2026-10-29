# VPS_1 / Agent 1 — Song Maker Sync, Settings and Live Queue — 2026-10-30

## Owner request
Enable Sync Now on the user's Song Maker page; remove the redundant Output workspace;
add Settings for export options; show real render FPS, speed, elapsed time and ETA
in the Queue using Auto Caption-style progress with batch controls.

## Implementation contract
- The authenticated Song Maker user may request a Drive sync. An ordinary user
  is restricted to the code-approved Song Studio default remote and input/output
  folders; administrator-only Drive remote/folder configuration remains protected.
- Sync requests are persisted in owner-specific SQLite settings and picked up by
  the existing independent sync worker; matching / source-version dedup preserved.
- Export presets are stored per owner, are validated, affect only newly admitted
  manual and Drive-matched jobs, and never rewrite already queued jobs.
- Keep Preview, Download and Drive link within Queue; no Output tab.
- Report real FFmpeg `-progress` FPS/speed when present, plus actual elapsed and
  speed-derived ETA. No fabricated metrics.
- Pause only prevents future claims. It does not terminate active render/upload,
  clear data, or pause other users. Batch retry, cancel pending and clear completed
  operate only on the requesting user's jobs.
- Preserve independent Song Maker state, source ID dedup, archive-to-used after
  verified render, Drive checksum verification, original manual input protection,
  existing SSO, and host serialization.

## Verification and release
PR CI: real media/API tests, GitHub security checks and browser at
360, 390, 768, 1024, 1440px. No real Drive or production uploads in CI.
Production install remains guarded by existing protected-main,
Tailscale-only Song Maker release transaction, health checks, idle fence and rollback.
A source commit, PR or synthetic Drive fixture is not proof of live deployment.

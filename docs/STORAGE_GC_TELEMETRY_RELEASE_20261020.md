# Storage GC telemetry release intent — 2026-10-20

- Source: PR #180 (merged, 34 successful checks, 2 skipped).
- Stage 1 only: existing guarded `storage-gc-install` via GitHub/Tailscale.
- Installer adds a root-owned sanitized read-only status producer and backfills existing audit (when readable).
- No deletion is requested by this intent; the existing 12-hour systemd timer is preserved.
- Stage 2 will separately deploy the read-only Storage Manager/Scene UI after this release passes.
- Application data, Drive media, OAuth, queues, rollback and Auto Script OFFLINE are protected.

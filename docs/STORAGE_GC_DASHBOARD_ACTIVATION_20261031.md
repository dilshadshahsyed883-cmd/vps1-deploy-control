# Storage Manager dashboard activation — 2026-10-31

This release request follows PR #513 (storage cleanup implementation),
PR #518 (fixed locked deployment status step), and PR #517 (isolated backend).

Only after Storage Manager backend release health/readback passes, merge with
`[vps1-storage-dashboard-release]` to install the Scene Builder shell
through the protected, serialized Tailscale workflow.

Readback must confirm admin-only Run Safe Cleanup with a confirmation
step and CSRF; 12-hour automated GC remains enabled, 30-minute root-owned
status refresh reports verified scan timestamps, and skipped reasons are
displayed without paths or personal data.

No arbitrary deletion, full-production-clean, database/queue removal,
rollback change, user-media deletion or Auto Script activation is authorized.

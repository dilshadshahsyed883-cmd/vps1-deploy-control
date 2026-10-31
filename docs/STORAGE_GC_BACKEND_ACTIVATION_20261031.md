# Storage GC — scoped backend activation (2026-10-31)

This change is an **auditable release trigger**, not a cleanup operation.

After PR #513's guarded GC installer passes production readback, the protected
`[vps1-storage-backend-retirement-release]` workflow installs only the
isolated Storage Manager candidate from protected main via Tailscale.

Acceptance: the candidate health and authenticated metrics pass; its
`/v1/snapshot` remains read-only; the new 30-minute root-owned telemetry
and manual GC path can be observed without granting the web service deletion
or root privileges. Media, queues, databases, rollback snapshots, Drive upload
records, current releases and Auto Script remain unchanged.

The Scene Builder dashboard is a **separate subsequent release**, triggered
only after this backend release is successful.
